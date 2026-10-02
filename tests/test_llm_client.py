"""Tests for OllamaClient's retry/backoff behaviour and the factory wiring.

These never hit a live model — requests.post is monkeypatched and time.sleep is
neutralised so the backoff math is exercised without real waits.
"""

import pytest
import requests

import src.llm.client as client_mod
from src.llm.client import OllamaClient, get_llm_client


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)


def test_returns_response_on_first_success(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return _FakeResponse({"response": "  hi  "})

    monkeypatch.setattr(client_mod.requests, "post", fake_post)
    client = OllamaClient(max_retries=2)
    assert client.generate("p") == "hi"
    assert len(calls) == 1  # no retries needed


def test_retries_then_succeeds(monkeypatch):
    attempts = {"n": 0}

    def flaky_post(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise requests.ConnectionError("ollama down")
        return _FakeResponse({"response": "ok"})

    monkeypatch.setattr(client_mod.requests, "post", flaky_post)
    client = OllamaClient(max_retries=2, retry_backoff=0)
    assert client.generate("p") == "ok"
    assert attempts["n"] == 3  # 1 initial + 2 retries


def test_raises_after_exhausting_retries(monkeypatch):
    attempts = {"n": 0}

    def always_fail(*args, **kwargs):
        attempts["n"] += 1
        raise requests.ConnectionError("still down")

    monkeypatch.setattr(client_mod.requests, "post", always_fail)
    client = OllamaClient(max_retries=2, retry_backoff=0)
    with pytest.raises(requests.ConnectionError):
        client.generate("p")
    assert attempts["n"] == 3  # gave up after 1 + 2


def test_factory_threads_retry_config():
    client = get_llm_client(
        {"llm": {"backend": "ollama", "max_retries": 5, "retry_backoff": 2.5}}
    )
    assert isinstance(client, OllamaClient)
    assert client.max_retries == 5
    assert client.retry_backoff == 2.5
