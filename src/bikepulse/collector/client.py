from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx


class GbfsClientError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class FetchResult:
    url: str
    payload: dict[str, Any]
    content: bytes
    content_sha256: str
    latency_ms: int
    attempts: int
    status_code: int


class GbfsClient:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_retries: int,
        backoff_seconds: float,
        user_agent: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._client = httpx.Client(
            timeout=timeout_seconds,
            headers={"User-Agent": user_agent},
            transport=transport,
        )

    def fetch_json(self, url: str) -> FetchResult:
        attempts = 0
        last_error: GbfsClientError | None = None
        while attempts <= self._max_retries:
            attempts += 1
            started = time.perf_counter()
            try:
                response = self._client.get(url)
                latency_ms = int((time.perf_counter() - started) * 1000)
                if response.status_code >= 500:
                    raise GbfsClientError(
                        "GBFS server error", retryable=True, status_code=response.status_code
                    )
                if response.status_code >= 400:
                    raise GbfsClientError(
                        "GBFS client error", retryable=False, status_code=response.status_code
                    )
                content = response.content
                payload = json.loads(content)
                return FetchResult(
                    url=url,
                    payload=payload,
                    content=content,
                    content_sha256=hashlib.sha256(content).hexdigest(),
                    latency_ms=latency_ms,
                    attempts=attempts,
                    status_code=response.status_code,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = GbfsClientError(str(exc), retryable=True)
            except json.JSONDecodeError as exc:
                raise GbfsClientError("GBFS response was not valid JSON", retryable=False) from exc
            except GbfsClientError as exc:
                last_error = exc
                if not exc.retryable:
                    raise
            if attempts <= self._max_retries:
                time.sleep(self._backoff_seconds * attempts)
        assert last_error is not None
        raise last_error

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GbfsClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
