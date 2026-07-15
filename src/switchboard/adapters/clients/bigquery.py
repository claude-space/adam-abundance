"""Async-friendly BigQuery client wrapper.

Wraps the (synchronous) ``google-cloud-bigquery`` SDK behind ``asyncio.to_thread``
and reports ``total_bytes_processed`` so the governor can charge ``bq_bytes``.
A ``dry_run`` estimate is available to price a query *before* running it (used by
the orchestrator's cost estimates).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ...credentials import GoogleSA
from ...logging_ import get_logger
from ..base import AdapterUnavailable
from .google_auth import BIGQUERY_SCOPES, build_credentials

log = get_logger("client.bigquery")


@dataclass
class BQResult:
    rows: list[dict[str, Any]]
    bytes_processed: int = 0
    fields: list[str] = field(default_factory=list)


class BigQueryClient:
    def __init__(self, sa: GoogleSA) -> None:
        self._sa = sa
        self._client = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from google.cloud import bigquery  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AdapterUnavailable("google-cloud-bigquery not installed (pip install .[data])") from exc
        creds = build_credentials(self._sa, BIGQUERY_SCOPES)
        project = self._sa.project_id
        if not project:
            raise AdapterUnavailable("No BigQuery project id configured")
        self._client = bigquery.Client(project=project, credentials=creds)
        return self._client

    def _query_sync(self, sql: str, params: dict[str, Any] | None, dry_run: bool) -> BQResult:
        from google.cloud import bigquery  # type: ignore

        def _bq_type(v: Any) -> str:
            if isinstance(v, bool):     # bool before int — bool is an int subclass
                return "BOOL"
            if isinstance(v, int):
                return "INT64"
            if isinstance(v, float):
                return "FLOAT64"
            return "STRING"

        client = self._get_client()
        qp = []
        for name, value in (params or {}).items():
            if isinstance(value, (list, tuple)):
                # Array parameter (e.g. `WHERE col IN UNNEST(@names)`); element
                # type is inferred from the first item, defaulting to STRING.
                seq = list(value)
                qp.append(bigquery.ArrayQueryParameter(
                    name, _bq_type(seq[0]) if seq else "STRING", seq))
            else:
                qp.append(bigquery.ScalarQueryParameter(name, _bq_type(value), value))
        job_config = bigquery.QueryJobConfig(
            query_parameters=qp, dry_run=dry_run, use_query_cache=not dry_run
        )
        job = client.query(sql, job_config=job_config)
        if dry_run:
            return BQResult(rows=[], bytes_processed=int(job.total_bytes_processed or 0))
        rows = [dict(r.items()) for r in job.result()]
        fields = list(rows[0].keys()) if rows else []
        return BQResult(rows=rows, bytes_processed=int(job.total_bytes_processed or 0), fields=fields)

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> BQResult:
        return await asyncio.to_thread(self._query_sync, sql, params, False)

    async def estimate_bytes(self, sql: str, params: dict[str, Any] | None = None) -> int:
        result = await asyncio.to_thread(self._query_sync, sql, params, True)
        return result.bytes_processed
