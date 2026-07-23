"""Plagiarism / originality checks (content QA, §13.16) via two providers, both
persisted on a content job's ``preview_meta['plagiarism']`` and surfaced in the
artifact-review "Plagiarism" signal:

  * **Copyscape** — synchronous text search. POST the text, get matches back.
    Needs ``COPYSCAPE_USERNAME`` + ``COPYSCAPE_API_KEY`` (the API authenticates
    with the account username AND the key).
  * **Copyleaks** — asynchronous. Log in (``COPYLEAKS_EMAIL`` + ``COPYLEAKS_API_KEY``),
    submit a scan, and the completed result arrives at a public webhook — so it
    needs ``SWITCHBOARD_PUBLIC_URL`` and only works where the app is publicly
    reachable (prod), not localhost.

Pure parsers/formatters are unit-testable; the network calls never raise (they
return an ``error``/``not_configured`` result) so a QA check can't break a page.
"""

from __future__ import annotations

import base64
import re
import secrets
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree as ET

from .logging_ import get_logger

log = get_logger("plagiarism")

_COPYSCAPE_URL = "https://www.copyscape.com/api/"
_COPYLEAKS_ID = "https://id.copyleaks.com"
_COPYLEAKS_API = "https://api.copyleaks.com"
_MAX_CHARS = 12000  # cap text sent out (cost + payload)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_int(v: Any) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return 0


# -- configuration -----------------------------------------------------------

def copyscape_configured(creds: Any) -> bool:
    return bool(creds.resolve("COPYSCAPE_USERNAME", secret=False) and creds.resolve("COPYSCAPE_API_KEY"))


def copyleaks_configured(creds: Any) -> bool:
    return bool(creds.resolve("COPYLEAKS_EMAIL", secret=False) and creds.resolve("COPYLEAKS_API_KEY"))


def missing_config(creds: Any) -> list[str]:
    """Identifiers that still need adding, given a key is present (for a helpful
    'you added the key but not the username' message)."""
    out: list[str] = []
    if creds.resolve("COPYSCAPE_API_KEY") and not creds.resolve("COPYSCAPE_USERNAME", secret=False):
        out.append("COPYSCAPE_USERNAME")
    if creds.resolve("COPYLEAKS_API_KEY") and not creds.resolve("COPYLEAKS_EMAIL", secret=False):
        out.append("COPYLEAKS_EMAIL")
    if copyleaks_configured(creds) and not creds.resolve("SWITCHBOARD_PUBLIC_URL", secret=False):
        out.append("SWITCHBOARD_PUBLIC_URL")
    return out


# -- Copyscape (synchronous) -------------------------------------------------

def parse_copyscape_xml(xml_text: str) -> dict[str, Any]:
    """Copyscape csearch XML → {status, score, count, matches[], error}. ``score``
    is the highest ``percentmatched`` across returned results (0 = nothing found).
    Uses descendant search so it's robust to the exact root element name."""
    try:
        root = ET.fromstring(xml_text or "")
    except ET.ParseError as exc:
        return {"status": "error", "provider": "copyscape", "error": f"bad XML: {exc}"}
    err = root.findtext(".//error")
    if err and err.strip():
        return {"status": "error", "provider": "copyscape", "error": err.strip()}
    matches: list[dict[str, Any]] = []
    for r in root.findall(".//result"):
        matches.append({
            "url": (r.findtext("url") or "").strip(),
            "title": (r.findtext("title") or "").strip(),
            "pct": _to_int(r.findtext("percentmatched")),
            "words": _to_int(r.findtext("wordsmatched")),
        })
    matches.sort(key=lambda m: m.get("pct") or 0, reverse=True)
    return {
        "status": "done", "provider": "copyscape",
        "score": matches[0]["pct"] if matches else 0,
        "count": _to_int(root.findtext(".//count")),
        "matches": matches[:5], "checked_at": _now(),
    }


async def run_copyscape(creds: Any, text: str) -> dict[str, Any]:
    """Synchronous Copyscape internet text search. Never raises."""
    user = creds.resolve("COPYSCAPE_USERNAME", secret=False)
    key = creds.resolve("COPYSCAPE_API_KEY")
    if not (user and key):
        return {"status": "not_configured", "provider": "copyscape",
                "error": "needs COPYSCAPE_USERNAME + COPYSCAPE_API_KEY"}
    body = (text or "").strip()[:_MAX_CHARS]
    if len(body) < 40:
        return {"status": "error", "provider": "copyscape", "error": "text too short to check"}
    try:
        import httpx  # type: ignore
    except ImportError:
        return {"status": "error", "provider": "copyscape", "error": "httpx not installed"}
    comparisons = _to_int(creds.resolve("COPYSCAPE_COMPARISONS", secret=False)) or 3
    data = {"u": user, "k": key, "o": "csearch", "e": "UTF-8", "c": str(comparisons), "t": body}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(_COPYSCAPE_URL, data=data)
            resp.raise_for_status()
            return parse_copyscape_xml(resp.text)
    except Exception as exc:  # noqa: BLE001
        log.info("[plagiarism] copyscape failed: %s", exc)
        return {"status": "error", "provider": "copyscape", "error": str(exc)[:200]}


# -- Copyleaks (asynchronous, webhook) ---------------------------------------

def new_scan_id(job_id: int) -> str:
    """A scan id that embeds the job id (so the webhook correlates without a
    lookup table) plus an unguessable suffix (the webhook is public)."""
    return f"sw{int(job_id)}x{secrets.token_hex(8)}"


def job_id_from_scan(scan_id: str) -> int | None:
    m = re.match(r"^sw(\d+)x[0-9a-f]+$", scan_id or "")
    return int(m.group(1)) if m else None


async def copyleaks_login(creds: Any) -> str | None:
    email = creds.resolve("COPYLEAKS_EMAIL", secret=False)
    key = creds.resolve("COPYLEAKS_API_KEY")
    if not (email and key):
        return None
    try:
        import httpx  # type: ignore
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{_COPYLEAKS_ID}/v3/account/login/api",
                                     json={"email": email, "key": key})
            resp.raise_for_status()
            return resp.json().get("access_token")
    except Exception as exc:  # noqa: BLE001
        log.info("[plagiarism] copyleaks login failed: %s", exc)
        return None


async def submit_copyleaks(creds: Any, *, scan_id: str, text: str, webhook_url: str) -> dict[str, Any]:
    """Log in + submit a scan whose completed result Copyleaks POSTs to
    ``webhook_url``. Returns a pending/error result. Never raises."""
    body = (text or "").strip()[:_MAX_CHARS]
    if len(body) < 40:
        return {"status": "error", "provider": "copyleaks", "error": "text too short to check"}
    token = await copyleaks_login(creds)
    if not token:
        return {"status": "error", "provider": "copyleaks", "error": "login failed / not configured"}
    sandbox = bool(str(creds.resolve("COPYLEAKS_SANDBOX", secret=False) or "").strip().lower()
                   in ("1", "true", "yes", "on"))
    payload = {
        "base64": base64.b64encode(body.encode("utf-8")).decode("ascii"),
        "filename": f"{scan_id}.txt",
        "properties": {"webhooks": {"status": webhook_url}, "sandbox": sandbox},
    }
    try:
        import httpx  # type: ignore
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.put(f"{_COPYLEAKS_API}/v3/scans/submit/file/{scan_id}",
                                    headers={"Authorization": f"Bearer {token}"}, json=payload)
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.info("[plagiarism] copyleaks submit failed: %s", exc)
        return {"status": "error", "provider": "copyleaks", "error": str(exc)[:200]}
    return {"status": "pending", "provider": "copyleaks", "scan_id": scan_id, "submitted_at": _now()}


def parse_copyleaks_result(payload: dict) -> dict[str, Any]:
    """Copyleaks 'completed' webhook payload → {status, score, matches[]}.
    ``aggregatedScore`` is normalised to a 0-100 percent."""
    results = (payload or {}).get("results") or {}
    raw = (results.get("score") or {}).get("aggregatedScore")
    if isinstance(raw, (int, float)):
        pct = round(raw * 100) if raw <= 1 else round(raw)
    else:
        pct = None
    matches: list[dict[str, Any]] = []
    for src in (results.get("internet") or [])[:5]:
        if isinstance(src, dict):
            matches.append({"url": src.get("url"), "title": src.get("title"),
                            "words": _to_int(src.get("matchedWords"))})
    return {"status": "done", "provider": "copyleaks", "score": pct,
            "matches": matches, "checked_at": _now()}


# -- signal formatting -------------------------------------------------------

_LABEL = {"copyscape": "Copyscape", "copyleaks": "Copyleaks"}


def signal_text(plag: dict | None) -> str | None:
    """Compact one-line summary for the artifact 'Plagiarism' signal, e.g.
    'Copyscape: clear · Copyleaks: scanning…'. None → SPA shows 'n/a'."""
    if not plag:
        return None
    parts: list[str] = []
    for prov in ("copyscape", "copyleaks"):
        p = plag.get(prov)
        if not isinstance(p, dict):
            continue
        label = _LABEL[prov]
        st = p.get("status")
        if st == "done":
            sc = p.get("score")
            if sc is None:
                parts.append(f"{label}: checked")
            elif sc <= 0:
                parts.append(f"{label}: clear")
            else:
                parts.append(f"{label}: {sc}% match")
        elif st == "pending":
            parts.append(f"{label}: scanning…")
        elif st == "error":
            parts.append(f"{label}: error")
    return " · ".join(parts) if parts else None
