# DOM-First Scrambled Capture Design

**Goal**

Replace the current comix.to scrambled-image flow with a DOM-first reader capture path that depends on stable reader output rather than private renderer function signatures.

**Problem**

The current scrambled-image implementation starts from a page image URL, then imports the site's private `secure-*.js` module and calls its renderer directly. This is brittle because comix.to changes that private renderer interface without notice. Each signature change breaks downloads even when the reader still renders the final page correctly in the browser.

The current downloader also lacks enough context to drive the actual reader UI. `ChapterPage` only carries the image URL plus dimensions, so the browser layer cannot navigate to the correct reader page and extract the final DOM-rendered result.

**Recommended Approach**

Introduce reader-aware page metadata and make DOM-first reader capture the primary scrambled-image path.

For each scrambled page, the adapter should return:

- the existing image URL
- the chapter reader URL
- the page index inside the chapter
- existing width and height hints

The downloader should keep treating plain images and scrambled images uniformly at the high level, but when a page is marked scrambled it should pass the full page metadata to the browser instead of only passing the image URL. The browser should then:

1. open or reuse the reader page for that chapter
2. navigate the reader state to the target page index
3. wait for the rendered DOM output for that page to stabilize
4. extract pixels from the visible reader canvas or image
5. fall back to element screenshot when direct canvas export is blocked

The existing renderer shim should remain available only as a fallback path when DOM-first capture cannot find a stable rendered result.

**Architecture**

1. **Reader-aware metadata**

Extend `ChapterPage` with explicit reader context:

- `reader_url: str | None`
- `page_index: int | None`

This keeps the download boundary simple: a `ChapterPage` remains the single unit of page work, but it now carries enough information to support both raw image fetches and reader-driven capture.

`ChapterImages` remains the adapter output type. The comix.to adapter becomes responsible for filling the new fields on scrambled pages while leaving plain-image pages unchanged.

2. **DOM-first browser capture**

Split comix-to browser capture into explicit phases:

- reader page preparation
- reader page selection
- DOM render discovery
- byte extraction
- fallback renderer invocation

The primary path should not import or call the private renderer directly. Instead it should locate the active reader canvas or image element in the DOM after the site finishes rendering the requested page.

Extraction order:

- If the active element is a canvas, try `toBlob()` first.
- If `toBlob()` fails because the canvas is tainted or unavailable, use Playwright element screenshot.
- If the active element is an image rather than a canvas, use the image element or a small wrapper container screenshot.

This makes the browser boundary depend on the reader's final presentation layer rather than its internal decode API.

3. **Fallback compatibility**

Retain the current imported-renderer shim as a fallback only. Use it when:

- reader DOM capture cannot find the active rendered page
- the reader page never settles on the requested index
- the DOM structure changes in a way that hides the final element but the legacy renderer still works

This fallback should live behind a clearly named helper so the primary DOM-first path and the compatibility path remain independently testable.

4. **Reader-page reuse**

DOM-first capture becomes much more expensive if every page opens a fresh reader page. The browser layer should therefore reuse the acquired Playwright page for all scrambled pages within a chapter download attempt whenever possible.

At minimum, one acquired pooled page should be able to:

- navigate once to a chapter reader URL
- move between page indices within that same chapter
- extract multiple page results sequentially

We do not need a full long-lived per-chapter cache in this refactor. Helper boundaries should still keep intra-chapter page reuse explicit and testable.

**Data Flow**

1. `ComixToAdapter.get_chapter_images()` parses chapter payloads and builds `ChapterPage` values.
2. Scrambled pages receive `reader_url` and `page_index`.
3. `Downloader._fetch_image_bytes()` passes the full `ChapterPage` into the scrambled-image browser call.
4. `CdpBrowser.get_scrambled_image_bytes()` keeps the public browser API stable but forwards the richer page metadata to the registered comix.to renderer.
5. The comix.to browser helper performs DOM-first reader capture.
6. If DOM-first capture fails for supported fallback reasons, the helper uses the existing renderer shim.

**Error Handling**

Failures should distinguish between:

- missing reader context
- reader navigation failure
- rendered DOM element not found
- direct canvas export blocked
- fallback renderer failure

These do not all need new exception classes immediately, but the error text should make diagnosis much easier than the current generic renderer failure path.

Screenshot fallback is not an error; it is an expected compatibility path when canvas export is blocked.

**Testing Strategy**

1. Extend parsing tests so scrambled `ChapterPage` records include `reader_url` and `page_index`.
2. Add browser-helper tests that assert DOM-first logic is attempted before the renderer shim.
3. Add tests for:
   - canvas export success
   - canvas export failure with screenshot fallback
   - missing DOM element with legacy-renderer fallback
   - missing reader context producing a clear failure
4. Keep the existing scrambled downloader integration test and update it only where the richer page metadata changes the call shape.
5. Do not expand `doctor` in this refactor; keep that as separate follow-up work.

**Scope Boundaries**

This refactor should not:

- redesign the generic browser page pool
- add cross-session reader caching
- remove the legacy renderer shim in the same change
- redesign non-scrambled downloads

The goal is to move the primary path to reader DOM capture while keeping a survivable fallback.

**Success Criteria**

- Scrambled pages no longer rely on private renderer signatures in the primary path.
- The browser can capture a scrambled page using reader DOM output with only page metadata and reader context.
- The fallback renderer still exists for unexpected reader DOM drift.
- Existing downloader and browser tests remain green with new coverage around DOM-first capture.
