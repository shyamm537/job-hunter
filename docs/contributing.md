# Contributing

> Placeholder. This is a single-contributor portfolio project as of this writing — these are stub conventions, not a tested process. Treat everything here as a draft until someone other than the original author actually opens a PR.

## Local setup

See the root `README.md` "Getting started" section — `pip install -r requirements.txt`, copy `config.yaml.example` to `config.yaml`, run `make scrape` / `make process` / `make app`.

## Before opening a PR

```bash
make lint   # ruff check src tests
make test   # pytest tests/
```

Both also run in CI (`.github/workflows/ci.yml`) on push/PR to `main`. A PR with failing lint or tests won't merge clean, but there's currently no branch protection enforcing that — it's just convention.

## Code conventions (as currently followed, not formally written down elsewhere)

- New scrapers subclass `BaseScraper` and live in `src/ingestion/` — see `docs/scrapers.md`.
- New LLM backends subclass `LLMClient` and live in `src/llm/client.py` — see `docs/llm-providers.md`.
- Database access goes through `src/storage/database.py`; nothing outside that module should construct a `Session` directly.
- No print-based logging beyond what already exists in the CLI scripts — there's no structured logging setup yet (see "(TODO)" below).

## (TODO) Things not decided yet

- Issue/PR templates
- Versioning / changelog policy
- Whether this accepts external contributions at all, or stays a personal project with the docs written defensively for that case
- A `CODE_OF_CONDUCT.md`
- Structured logging (currently everything is `print()` in the CLI scripts — fine for a single user, not fine if this grows)
