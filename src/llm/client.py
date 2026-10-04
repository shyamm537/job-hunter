"""LLM abstraction layer.

The rest of the codebase calls `get_llm_client(config).generate(prompt)` and
never imports a specific backend directly. Swapping Ollama for Llama.cpp,
a hosted OpenAI-compatible API, or Hugging Face transformers is a
config.yaml change, not a code change.
"""

import logging
import time
from abc import ABC, abstractmethod

import requests

log = logging.getLogger("jobhunter.llm")


class LLMClient(ABC):
    @abstractmethod
    def generate(self, prompt: str) -> str:
        raise NotImplementedError


class OllamaClient(LLMClient):
    """Talks to a local Ollama server (default backend).

    Transient failures (server briefly down, a dropped connection, a 5xx)
    are retried with exponential backoff. Once the retries are exhausted the
    original exception propagates — the caller (src/llm/cli.py) decides what to
    do with a job that can't be generated.

    `timeout` is how many seconds to wait for a generation (None = no limit).
    Connecting gets its own short limit, so an unreachable server still fails
    fast. A read timeout is raised at once, not retried: the model was busy,
    and asking again would only repeat the wait.
    """

    CONNECT_TIMEOUT = 10

    def __init__(
        self,
        model: str = "llama3",
        host: str = "http://localhost:11434",
        max_retries: int = 2,
        retry_backoff: float = 1.0,
        timeout: float | None = 600,
    ):
        self.model = model
        self.host = host
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.timeout = timeout

    def generate(self, prompt: str) -> str:
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                response = requests.post(
                    f"{self.host}/api/generate",
                    json={"model": self.model, "prompt": prompt, "stream": False},
                    timeout=(self.CONNECT_TIMEOUT, self.timeout),
                )
                response.raise_for_status()
                return response.json().get("response", "").strip()
            except requests.ReadTimeout:
                raise
            except requests.RequestException as exc:
                if attempt == attempts - 1:
                    raise
                wait = self.retry_backoff * (2**attempt)
                log.warning(
                    "LLM call failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    attempts,
                    exc,
                    wait,
                )
                if wait:
                    time.sleep(wait)
        # Unreachable: the loop either returns or re-raises on the last attempt.
        raise RuntimeError("generate() exhausted retries without returning")


def get_llm_client(config: dict) -> LLMClient:
    """Factory that picks a backend based on config.yaml's `llm.backend`."""
    llm_config = config.get("llm", {})
    backend = llm_config.get("backend", "ollama")

    if backend == "ollama":
        return OllamaClient(
            model=llm_config.get("model", "llama3"),
            host=llm_config.get("host", "http://localhost:11434"),
            max_retries=llm_config.get("max_retries", 2),
            retry_backoff=llm_config.get("retry_backoff", 1.0),
            # 0 in config means no limit, which requests spells None.
            timeout=llm_config.get("timeout", 600) or None,
        )

    raise ValueError(
        f"Unsupported LLM backend: {backend!r}. "
        "Only 'ollama' is implemented today — add a new LLMClient subclass "
        "and a branch here to support others."
    )
