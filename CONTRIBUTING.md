# Contributing

## Development setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pytest -q
```

The automated suite is offline and must not require Ollama, Qdrant Server, Docker,
or access to the source website. Tests that exercise live services belong in an
explicit diagnostic script and must not run in CI.

## Pull requests

- Keep changes focused and explain the user-visible behavior.
- Add or update tests for routing, ingestion, retrieval, and evidence-gate changes.
- Do not commit `.env`, crawled pages, databases, vectors, model files, exports, or
  personal conversation data.
- Preserve source URLs and citation metadata through every retrieval transformation.
- Prefer an explicit abstention over relaxing a safety or evidence threshold silently.
- Run `python -m pip check` and `python -m pytest -q` before opening a pull request.

## Dependency changes

`requirements.in` documents direct dependencies. `requirements.lock.txt` is the
reviewed Python 3.12 environment used by CI. Update both intentionally and describe
why the dependency is needed.
