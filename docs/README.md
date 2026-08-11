# Documentation

This is placeholder documentation scaffolded against the current state of the codebase — the storage layer, one scraper, the LLM wrapper, and the Streamlit dashboard. Sections marked **(TODO)** describe intent or open questions, not working code. As the project grows, treat these as living documents to update alongside the code they describe, not as a spec written in advance of it.

| Doc | Covers |
|---|---|
| [`architecture.md`](./architecture.md) | Layered design, the three core patterns (Strategy ingestion, DB-backed queue, LLM abstraction), end-to-end data flow |
| [`data-model.md`](./data-model.md) | `JobPost` schema field-by-field, dedup strategy, known looseness, future table candidates |
| [`scrapers.md`](./scrapers.md) | `BaseScraper` contract, the `SeekScraper` worked example, scraping scope/ethics, how to add a new source |
| [`llm-providers.md`](./llm-providers.md) | `LLMClient` contract, `OllamaClient`, prompt templates, how to add a new backend |
| [`configuration.md`](./configuration.md) | Every `config.yaml` field, what's actually wired up vs. documented-only |
| [`roadmap.md`](./roadmap.md) | Detailed status per step, open questions not yet resolved |
| [`contributing.md`](./contributing.md) | Local setup, lint/test commands, conventions — draft, untested by an outside contributor |

For getting started (install, run, the project pitch), see the root [`README.md`](../README.md).
