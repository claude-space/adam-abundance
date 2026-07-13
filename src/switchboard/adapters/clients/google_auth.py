"""Build read-scoped Google credentials from the credentials layer.

Accepts either inline service-account JSON (``GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON``)
or a key-file path (``GOOGLE_APPLICATION_CREDENTIALS``). Never uses a logged-in
user's token — resource access is always service-account based (PRD §9.1).
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from ...credentials import GoogleSA
from ..base import AdapterUnavailable

# Scopes gate which API surface the token may call; read-only-ness is enforced
# by the service account's IAM role (BigQuery Data Viewer / Sheets viewer), not
# the scope (PRD §8 least-privilege). NOTE: running even a read-only BigQuery
# query submits a job, which the narrow `bigquery.readonly` scope forbids
# (ACCESS_TOKEN_SCOPE_INSUFFICIENT) — so we use the `bigquery` scope and rely on
# IAM to keep it read-only.
BIGQUERY_SCOPES = ("https://www.googleapis.com/auth/bigquery",)
SHEETS_SCOPES = ("https://www.googleapis.com/auth/spreadsheets.readonly",)


def build_credentials(sa: GoogleSA, scopes: Sequence[str]) -> Any:
    try:
        from google.oauth2 import service_account  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise AdapterUnavailable("google-auth not installed (pip install .[data])") from exc

    if sa.inline_json:
        try:
            info = json.loads(sa.inline_json)
        except ValueError as exc:
            raise AdapterUnavailable("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON is not valid JSON") from exc
        return service_account.Credentials.from_service_account_info(info, scopes=list(scopes))
    if sa.path:
        return service_account.Credentials.from_service_account_file(sa.path, scopes=list(scopes))
    raise AdapterUnavailable("No Google service-account credential configured")
