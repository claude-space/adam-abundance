"""Hero-image candidate sources for the artifact preview (§6.6).

Three sources, merged into one small candidate list the reviewer picks from:
- **Unsplash** and **Pexels** — licensed stock, searched by the trend's topic.
  Free tiers; each needs an API key (UNSPLASH_ACCESS_KEY / PEXELS_API_KEY) and
  returns [] when its key is absent, so the picker degrades gracefully.
- **S3 media library** — the brand's own imagery under a configured prefix
  (IMAGE_LIBRARY_PREFIX in S3_BUCKET_NAME), served via IMAGE_CDN_BASE or a
  presigned URL. Requires boto3 + the prefix; returns [] otherwise.

Every candidate carries attribution (credit / credit_url) — Unsplash and Pexels
both require crediting the photographer.
"""
from __future__ import annotations

from typing import Any

from ..logging_ import get_logger

log = get_logger("adapters.images")

_UA = {"User-Agent": "Switchboard/1.0 (+editorial preview)"}


async def _unsplash(query: str, key: str, limit: int) -> list[dict[str, Any]]:
    import httpx  # type: ignore
    async with httpx.AsyncClient(timeout=15.0, headers={**_UA, "Authorization": f"Client-ID {key}"}) as c:
        r = await c.get("https://api.unsplash.com/search/photos",
                        params={"query": query, "per_page": limit, "orientation": "landscape"})
        r.raise_for_status()
        out = []
        for p in (r.json().get("results") or [])[:limit]:
            urls = p.get("urls") or {}
            user = p.get("user") or {}
            out.append({
                "id": f"unsplash:{p.get('id')}",
                "source": "unsplash",
                "thumb_url": urls.get("small") or urls.get("thumb"),
                "full_url": urls.get("regular") or urls.get("full"),
                "credit": f"{user.get('name', 'Unknown')} / Unsplash",
                "credit_url": (user.get("links") or {}).get("html"),
                "width": p.get("width"), "height": p.get("height"),
            })
        return [x for x in out if x["thumb_url"] and x["full_url"]]


async def _pexels(query: str, key: str, limit: int) -> list[dict[str, Any]]:
    import httpx  # type: ignore
    async with httpx.AsyncClient(timeout=15.0, headers={**_UA, "Authorization": key}) as c:
        r = await c.get("https://api.pexels.com/v1/search",
                        params={"query": query, "per_page": limit, "orientation": "landscape"})
        r.raise_for_status()
        out = []
        for p in (r.json().get("photos") or [])[:limit]:
            src = p.get("src") or {}
            out.append({
                "id": f"pexels:{p.get('id')}",
                "source": "pexels",
                "thumb_url": src.get("medium") or src.get("small"),
                "full_url": src.get("large") or src.get("original"),
                "credit": f"{p.get('photographer', 'Unknown')} / Pexels",
                "credit_url": p.get("photographer_url") or p.get("url"),
                "width": p.get("width"), "height": p.get("height"),
            })
        return [x for x in out if x["thumb_url"] and x["full_url"]]


def _s3_media(creds: Any, limit: int) -> list[dict[str, Any]]:
    """Brand imagery from the S3 media library under IMAGE_LIBRARY_PREFIX. Served
    via IMAGE_CDN_BASE if set (public CDN), else a presigned GET URL. Best-effort:
    [] when boto3 is missing or the prefix/bucket isn't configured."""
    prefix = creds.resolve("IMAGE_LIBRARY_PREFIX", secret=False)
    bucket = creds.resolve("S3_BUCKET_NAME", secret=False)
    if not (prefix and bucket):
        return []
    try:
        import boto3  # type: ignore
    except ImportError:
        log.info("[images] S3 media library configured but boto3 not installed")
        return []
    cdn = (creds.resolve("IMAGE_CDN_BASE", secret=False) or "").rstrip("/")
    try:
        s3 = boto3.client(
            "s3",
            aws_access_key_id=creds.resolve("S3_ACCESS_KEY_ID"),
            aws_secret_access_key=creds.resolve("S3_SECRET_ACCESS_KEY"))
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=max(limit * 2, 10))
        out: list[dict[str, Any]] = []
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if not key.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".avif")):
                continue
            if cdn:
                url = f"{cdn}/{key}"
            else:
                url = s3.generate_presigned_url("get_object",
                                                Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600)
            out.append({"id": f"s3:{key}", "source": "media-library",
                        "thumb_url": url, "full_url": url,
                        "credit": "Valnet media library", "credit_url": None})
            if len(out) >= limit:
                break
        return out
    except Exception as exc:  # noqa: BLE001 — best-effort source
        log.info("[images] S3 media listing failed: %s", exc)
        return []


async def image_candidates(creds: Any, query: str, *, per_source: int = 6) -> dict[str, Any]:
    """Merge candidates from the media library + Unsplash + Pexels for ``query``.
    Each source is independent and soft-fails to [] (missing key, network, etc.),
    so the picker shows whatever is available. Returns configured-source flags so
    the UI can explain an empty result."""
    query = (query or "").strip() or "automotive"
    candidates: list[dict[str, Any]] = []
    sources: dict[str, str] = {}

    candidates += _s3_media(creds, per_source)
    sources["media-library"] = "on" if creds.resolve("IMAGE_LIBRARY_PREFIX", secret=False) else "unconfigured"

    unsplash_key = creds.resolve("UNSPLASH_ACCESS_KEY")
    if unsplash_key:
        try:
            candidates += await _unsplash(query, unsplash_key, per_source)
            sources["unsplash"] = "on"
        except Exception as exc:  # noqa: BLE001
            log.info("[images] unsplash search failed: %s", exc)
            sources["unsplash"] = "error"
    else:
        sources["unsplash"] = "unconfigured"

    pexels_key = creds.resolve("PEXELS_API_KEY")
    if pexels_key:
        try:
            candidates += await _pexels(query, pexels_key, per_source)
            sources["pexels"] = "on"
        except Exception as exc:  # noqa: BLE001
            log.info("[images] pexels search failed: %s", exc)
            sources["pexels"] = "error"
    else:
        sources["pexels"] = "unconfigured"

    return {"query": query, "candidates": candidates, "sources": sources}
