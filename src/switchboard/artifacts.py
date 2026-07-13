"""Artifact store (PRD §11): structured record + pointer in the DB, bytes in blob
storage. Agents never touch the filesystem directly — they go through here.

Two backends: ``local`` (a directory, for dev) and ``gcs`` (Google Cloud
Storage, since the portfolio is on GCP). Returns a pointer dict that gets stored
on the ``report`` / ``distribution_draft`` memory entry's payload.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import get_settings
from .logging_ import get_logger

log = get_logger("artifacts")


def _key(brand: str, kind: str, ext: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{brand}/{kind}/{ts}.{ext}"


class ArtifactStore:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.backend = self.settings.artifacts.backend

    def put_text(self, *, brand: str, kind: str, ext: str, text: str,
                 content_type: str = "text/plain") -> dict[str, Any]:
        return self._put(brand, kind, ext, text.encode("utf-8"), content_type)

    def put_bytes(self, *, brand: str, kind: str, ext: str, data: bytes,
                  content_type: str = "application/octet-stream") -> dict[str, Any]:
        return self._put(brand, kind, ext, data, content_type)

    def _put(self, brand: str, kind: str, ext: str, data: bytes, content_type: str) -> dict[str, Any]:
        key = _key(brand, kind, ext)
        if self.backend == "gcs":
            return self._put_gcs(key, data, content_type)
        return self._put_local(key, data, content_type)

    def _put_local(self, key: str, data: bytes, content_type: str) -> dict[str, Any]:
        root = Path(self.settings.artifacts.local_dir)
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        log.info("artifact written: %s (%d bytes)", path, len(data))
        return {"backend": "local", "key": key, "uri": path.resolve().as_uri(),
                "content_type": content_type, "bytes": len(data)}

    def _put_gcs(self, key: str, data: bytes, content_type: str) -> dict[str, Any]:
        try:
            from google.cloud import storage  # type: ignore
        except ImportError:
            log.warning("google-cloud-storage not installed; falling back to local artifact store")
            return self._put_local(key, data, content_type)
        client = storage.Client(project=self.settings.creds.google_sa().project_id)
        bucket = client.bucket(self.settings.artifacts.gcs_bucket)
        blob = bucket.blob(key)
        blob.upload_from_string(data, content_type=content_type)
        uri = f"gs://{self.settings.artifacts.gcs_bucket}/{key}"
        log.info("artifact uploaded: %s (%d bytes)", uri, len(data))
        return {"backend": "gcs", "key": key, "uri": uri, "content_type": content_type, "bytes": len(data)}
