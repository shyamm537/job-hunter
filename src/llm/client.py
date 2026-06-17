"""LLM abstraction layer.

The rest of the codebase calls `get_llm_client(config).generate(prompt)` and
never imports a specific backend directly. Swapping Ollama for Llama.cpp,
a hosted OpenAI-compatible API, or Hugging Face transformers is a
config.yaml change, not a code change.
"""

from abc import ABC, abstractmethod

import requests


class LLMClient(ABC):
    @abstractmethod
    def generate(self, prompt: str) -> str:
        raise NotImplementedError


class OllamaClient(LLMClient):
    """Talks to a local Ollama server (default backend)."""

    def __init__(self, model: str = "llama3", host: str = "http://localhost:11434"):
        self.model = model
        self.host = host

    def generate(self, prompt: str) -> str:
        response = requests.post(
            f"{self.host}/api/generate",
            json={"model": self.model, "prompt": prompt, "stream": False},
            timeout=120,
        )
        response.raise_for_status()
        return response.json().get("response", "").strip()


def get_llm_client(config: dict) -> LLMClient:
    """Factory that picks a backend based on config.yaml's `llm.backend`."""
    llm_config = config.get("llm", {})
    backend = llm_config.get("backend", "ollama")

    if backend == "ollama":
        return OllamaClient(
            model=llm_config.get("model", "llama3"),
            host=llm_config.get("host", "http://localhost:11434"),
        )

    raise ValueError(
        f"Unsupported LLM backend: {backend!r}. "
        "Only 'ollama' is implemented today — add a new LLMClient subclass "
        "and a branch here to support others."
    )
