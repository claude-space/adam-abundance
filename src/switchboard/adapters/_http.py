"""Tiny shared HTTP-JSON helper for reading/triggering existing-system endpoints,
with bounded retry + backoff on transient failures (PRD Phase 6 hardening)."""

from __future__ import annotations

import asyncio
from typing import Any

from ..logging_ import get_logger
from .base import AdapterUnavailable

log = get_logger("adapter.http")

_MAX_ATTEMPTS = 3
_RETRY_STATUS = {429, 500, 502, 503, 504}


async def _request(method: str, base: str, path: str, *, json: Any | None = None,
                   headers: dict[str, str] | None = None, params: dict[str, Any] | None = None,
                   timeout: float = 30.0) -> Any:
    try:
        import httpx  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise AdapterUnavailable("httpx not installed") from exc

    url = f"{base.rstrip('/')}{path}"
    last_exc: Exception | None = None
    # follow_redirects: ShellAgent-hosted agents 308 API paths to a trailing slash.
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.request(method, url, json=json, headers=headers or {}, params=params)
                if resp.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS - 1:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                resp.raise_for_status()
                try:
                    return resp.json()
                except Exception:  # noqa: BLE001 — non-JSON response
                    return {"status_code": resp.status_code, "text": resp.text[:500]}
            except httpx.HTTPStatusError:
                raise
            except httpx.HTTPError as exc:  # transport/timeout — retry
                last_exc = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                raise
    if last_exc:  # pragma: no cover
        raise last_exc


async def get_json(base: str, path: str, headers: dict[str, str] | None = None,
                   params: dict[str, Any] | None = None) -> Any:
    return await _request("GET", base, path, headers=headers, params=params, timeout=20.0)


async def post_json(base: str, path: str, *, json: Any | None = None,
                    headers: dict[str, str] | None = None, params: dict[str, Any] | None = None,
                    timeout: float = 60.0) -> Any:
    return await _request("POST", base, path, json=json, headers=headers, params=params, timeout=timeout)
