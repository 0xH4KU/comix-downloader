# Contributing

## Scope

This project is maintained as a sequence of small, fully-verified slices. Contributors should prefer changes that are easy to review, easy to rollback, and easy to validate in isolation.

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

Current high-risk module baselines:

- `src/comix_dl/cli/__init__.py`: `100%`
- `src/comix_dl/cli/flows.py`: `89%`
- `src/comix_dl/cdp_browser.py`: `78%`
- `src/comix_dl/converters.py`: `70%`

Regression expectation:

- Changes in high-risk modules such as browser/session, conversion, and CLI orchestration should come with focused tests instead of relying on the global floor

## Regression Test Policy

Any behavior change should include tests that prove the intended outcome.

Required cases:

- Bug fixes must add or extend a regression test that fails before the fix
- Browser/session changes must cover lock handling, retries, timeouts, or page-pool behavior as applicable
- Download/resume changes must cover partial state, recovery, and completion boundaries
- Converter changes must cover large-input and failure-path behavior when relevant
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

- update `TODO.md` checkboxes only when the acceptance condition is actually met
- bump the patch version in `pyproject.toml`, `src/comix_dl/__init__.py`, and the README version badge
- update the relevant docs in the same slice
- commit immediately after validation passes

## Documentation Expectations

Keep these files aligned with reality:

- `README.md` for user-facing behavior and commands
- `ARCHITECTURE.md` for current structure and known debt
- `DEVELOPMENT.md` for local setup and quality commands
- `TODO.md` for accepted and remaining work

Do not document target architecture as if it already exists.
