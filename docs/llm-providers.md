# LLM Providers

## The contract

`src/llm/client.py`:

```python
class LLMClient(ABC):
    @abstractmethod
    def generate(self, prompt: str) -> str:
        ...
```

One method, one job: take a fully-formatted prompt string, return a string. No streaming, no chat history, no system/user message separation — `prompts.py` builds one complete prompt per call.

## The only implementation: `OllamaClient`

Talks to a local Ollama server over HTTP (`POST /api/generate`, `stream: false`). Configured via `config.yaml`:

```yaml
llm:
  backend: ollama
  model: llama3
  host: http://localhost:11434
```

`get_llm_client(config)` reads `llm.backend` and constructs the matching client. Right now `ollama` is the only valid value — anything else raises `ValueError`.

## How prompts get built

`src/llm/prompts.py` has two templates, `COVER_LETTER_TEMPLATE` and `COLD_EMAIL_TEMPLATE`, both plain `.format()` strings taking `title`, `company`, `description`, `resume_summary`. `src/llm/cli.py` is the only caller — it formats both templates per pending job and calls `client.generate()` twice.

## (TODO) Adding a second backend

Not built. The factory function is intentionally a single `if/elif` chain so this is a small change when it happens:

1. New subclass of `LLMClient`, e.g. `OpenAIClient`, implementing `generate()`
2. New `elif backend == "openai":` branch in `get_llm_client()`
3. Whatever new config fields that backend needs (e.g. `llm.api_key`) — note: an API key in `config.yaml` means `config.yaml` definitely should not be committed (it's already gitignored, but worth restating since this would make it actively sensitive rather than just personal)

Candidates mentioned in the original project scope: Hugging Face `transformers`, Llama.cpp directly, OpenAI-compatible APIs. None have started.

## (TODO) Error handling

`OllamaClient.generate()` calls `response.raise_for_status()` — if Ollama isn't running, or the model isn't pulled, this raises an unhandled exception that kills `make process` mid-batch. No retry, no partial-progress recovery. If job N fails, jobs N+1 onward in that run don't get processed, but jobs 1 through N-1 already committed are safe (each job commits independently in the loop in `src/llm/cli.py`).
