"""Read-only Google Sheets client (values API). Used for writer quotas/baselines
(Analytics) and the paid-media RAW_DATA log (Paid-Media). Switchboard only READS
— quota write-backs stay in writers-dashboard; RAW_DATA writes stay in mp-spend
(PRD §6.5, §8)."""

from __future__ import annotations

import asyncio
from typing import Any

from ...credentials import GoogleSA
from ..base import AdapterUnavailable
from .google_auth import SHEETS_SCOPES, build_credentials


class SheetsClient:
    def __init__(self, sa: GoogleSA) -> None:
        self._sa = sa
        self._service = None

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service
        try:
            from googleapiclient.discovery import build  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AdapterUnavailable("google-api-python-client not installed (pip install .[data])") from exc
        creds = build_credentials(self._sa, SHEETS_SCOPES)
        self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return self._service

    def _read_sync(self, spreadsheet_id: str, range_a1: str) -> list[list[Any]]:
        service = self._get_service()
        resp = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=range_a1)
            .execute()
        )
        return resp.get("values", [])

    async def read(self, spreadsheet_id: str, range_a1: str) -> list[list[Any]]:
        return await asyncio.to_thread(self._read_sync, spreadsheet_id, range_a1)

    async def read_records(self, spreadsheet_id: str, tab: str, *, max_col: str = "Z") -> list[dict[str, Any]]:
        """Read a tab as header-keyed dict rows (row 1 = headers)."""
        values = await self.read(spreadsheet_id, f"'{tab}'!A1:{max_col}")
        if not values:
            return []
        headers = [str(h).strip() for h in values[0]]
        records = []
        for row in values[1:]:
            padded = list(row) + [None] * (len(headers) - len(row))
            records.append(dict(zip(headers, padded)))
        return records
