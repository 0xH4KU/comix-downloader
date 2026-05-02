# Contributing

## Scope

This project is maintained as a sequence of small, fully-verified slices. Contributors should prefer changes that are easy to review, easy to rollback, and easy to validate in isolation.

If you are not improving comix-dl itself but **forking it for a different site**, see `FORKING.md` instead — that document covers the framework / reference-adapter split and the file-by-file checklist for swapping the comix.to adapter for another site.

## First PR in 30 minutes

The fastest path from clone to merged change:

1. **Pick a small target.** Good first PRs touch one module, add a focused test, and don't change the public CLI surface. The "Known Debt" section of `ARCHITECTURE.md` lists candidates that already have a clear owner; the `todo.md` "follow-up" entries are also self-contained.
2. **Set up a local venv.** Steps in *Development Environment* below — about 3 minutes once Chrome is installed.
3. **Reproduce the existing behaviour.** Run the relevant tests (e.g. `pytest tests/test_<module>.py -v`) before changing anything so you have a baseline.
4. **Write the test first.** For bug fixes the test should fail against `main`; for refactors it should already pass and stay green afterwards.
5. **Make the code change.** Keep the diff under ~150 lines if you can. If the work won't fit, split it: add the test in one PR, change behaviour in the next.
6. **Run the full local quality gate** (next section). Don't push until ruff, mypy, and pytest are all clean.
7. **Open the PR with a tight description.** What changed, why, what was tested. The reviewer should not need to dig into commits to understand the boundary.

If you're not sure whether a change qualifies as "small", open a draft PR or an issue first — easier to redirect early than to rework after review.

## Development Environment

```bash
git clone https://github.com/0xH4KU/comix-downloader.git
cd comix-downloader
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

Runtime expectations:

- Python 3.11+
- Google Chrome installed locally
- Desktop environment available for first-run Cloudflare clearance

## Local Quality Gate

Before opening a PR, run the same checks the project expects locally:

```bash
.venv/bin/ruff check .
.venv/bin/mypy src/comix_dl --no-error-summary
.venv/bin/python scripts/check_docs_consistency.py
.venv/bin/pytest --cov=comix_dl --cov-report=term-missing --cov-report=xml --cov-fail-under=70 -q
```

Current enforced coverage gate:

- Total coverage must stay at or above `70%`

Current high-risk module baselines (post wave-1 site-adapter refactor):

- `src/comix_dl/core/cli/__init__.py`
- `src/comix_dl/core/cli/flows.py`
- `src/comix_dl/core/engines/cdp_browser.py`
- `src/comix_dl/core/engines/browser_session.py`
- `src/comix_dl/core/engines/chrome_process.py`
- `src/comix_dl/core/converters.py`
- `src/comix_dl/sites/comix_to.py`

Regression expectation:

- Changes in high-risk modules such as engine, conversion, and CLI orchestration should come with focused tests instead of relying on the global floor.

## Regression Test Policy

Any behavior change should include tests that prove the intended outcome.

Required cases:

- Bug fixes must add or extend a regression test that fails before the fix
- Engine / browser session changes must cover lock handling, retries, timeouts, or page-pool behavior as applicable
- Download/resume changes must cover partial state, recovery, and completion boundaries
- Converter changes must cover large-input and failure-path behavior when relevant
- Site adapter (`sites/comix_to.py`) changes must cover the affected protocol method (search / get_series / get_chapter_images / deduplicate / on_engine_ready) with a fixture-driven test
- Documentation-only changes do not need tests, but they still must keep version/docs consistency checks green

## Pull Request Rules

PRs should be small and scoped. Avoid mixing architectural refactors, behavior changes, and unrelated cleanup in one review.

Each PR should:

- explain the user-visible or maintenance problem being solved
- describe the chosen boundary of the change
- list the validation commands that were run
- update affected documentation when behavior, commands, or architecture notes change

## Release Slice Rules

If a change is released as a versioned slice, do not batch finished work with unrelated pending work.

For each completed slice:

- update `todo.md` checkboxes only when the acceptance condition is actually met
- bump the patch version in `pyproject.toml`, `src/comix_dl/__init__.py`, and the README version badge
- update the relevant docs in the same slice
- commit immediately after validation passes

## Documentation Expectations

Keep these files aligned with reality:

- `README.md` for user-facing behavior and commands
- `ARCHITECTURE.md` for current structure and known debt
- `DEVELOPMENT.md` for local setup and quality commands
- `MIGRATION.md` for behavioural / API shifts maintainers need to absorb
- `FORKING.md` for forks replacing the reference site adapter
- `todo.md` for accepted and remaining work

Do not document target architecture as if it already exists.

## Where things live (post-wave-1 layout)

```
src/comix_dl/
  core/        ← framework: site-agnostic. avoid coupling to comix.to here.
    application/
    cli/
    engines/
    ...
  sites/       ← site adapters. comix_to.py is the reference adapter.
    base.py    ← the SiteAdapter / Engine protocols
    comix_to.py
    _template.py  ← copy this when adding a new site
```

If your change touches `core/`, prefer designing it so that swapping `sites/comix_to.py` for a different adapter still works — even if you're not currently doing that swap. The framework is meant to outlive any single reference site.
