from __future__ import annotations

import httpx
import pytest

from bikepulse.collector.client import GbfsClient, GbfsClientError


def test_retryable_server_error_is_retried() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "retry"})
        return httpx.Response(200, json={"ok": True})

    client = GbfsClient(
        timeout_seconds=1,
        max_retries=1,
        backoff_seconds=0,
        user_agent="BikePulse tests",
        transport=httpx.MockTransport(handler),
    )

    result = client.fetch_json("https://example.test/status.json")

    assert calls == 2
    assert result.payload == {"ok": True}
    assert result.attempts == 2
    assert len(result.content_sha256) == 64


def test_non_retryable_client_error_fails_once() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={"error": "missing"})

    client = GbfsClient(
        timeout_seconds=1,
        max_retries=2,
        backoff_seconds=0,
        user_agent="BikePulse tests",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(GbfsClientError) as exc:
        client.fetch_json("https://example.test/status.json")

    assert calls == 1
    assert exc.value.retryable is False
