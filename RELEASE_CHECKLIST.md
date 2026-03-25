# Release Checklist

## Versioned Slice Rules

Use this checklist for every released slice. Do not batch finished work with unrelated pending work.

1. Finish one bounded slice and confirm its acceptance condition is actually met.
2. Update `TODO.md` immediately for any item that is truly complete. Do not pre-check work in progress.
3. Bump the patch version in:
   `pyproject.toml`
   `src/comix_dl/__init__.py`
   `README.md` version badge
4. Update any affected docs in the same slice:
   `README.md`
   `ARCHITECTURE.md`
   `DEVELOPMENT.md`
   `CONTRIBUTING.md`
   `MIGRATION.md` when maintainer assumptions changed
5. Run the full validation gate:
   `.venv/bin/ruff check .`
   `.venv/bin/mypy src/comix_dl --no-error-summary`
   `.venv/bin/python scripts/check_docs_consistency.py`
   `.venv/bin/pytest --cov=comix_dl --cov-report=term-missing --cov-fail-under=70 -q`
6. Commit immediately with a slice-scoped message.

## Final Branch Closeout

Before declaring the branch finished:

- `TODO.md` has no unchecked release items left
- `git status --short` is clean
- docs/version consistency check is green on the final version
- the latest verified coverage/report matches the enforced CI gate
- the last commit boundary is still small enough to review or roll back cleanly
