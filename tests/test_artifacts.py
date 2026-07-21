"""Artifact store (PRD §11): key generation + local/GCS blob write.

Needs the stack installed (config/pydantic) but NO database. ``get_settings`` is
patched per-test so the store reads a temp ``local_dir`` / fake creds instead of
the process-wide settings. The GCS backend is exercised with the ``google.cloud``
SDK mocked — GCP is never contacted.

NB: this module is *only* a key + blob store — there is no digest/HTML/escaping
logic to cover. Tests reflect what the source actually does.
"""

from __future__ import annotations

import re
import sys
import types
from types import SimpleNamespace

from switchboard.artifacts import ArtifactStore, _key


def _fake_settings(local_dir, *, backend="local", bucket="switchboard-artifacts",
                   project_id="proj-123"):
    return SimpleNamespace(
        artifacts=SimpleNamespace(backend=backend, local_dir=str(local_dir), gcs_bucket=bucket),
        creds=SimpleNamespace(google_sa=lambda: SimpleNamespace(project_id=project_id)),
    )


def _store(monkeypatch, settings):
    monkeypatch.setattr("switchboard.artifacts.get_settings", lambda: settings)
    return ArtifactStore()


# -- key generation -----------------------------------------------------------

def test_key_format():
    key = _key("hotcars", "report", "html")
    assert re.match(r"^hotcars/report/\d{8}-\d{6}\.html$", key), key


def test_key_components_and_extension():
    key = _key("carbuzz", "distribution_draft", "json")
    brand, kind, fname = key.split("/")
    assert brand == "carbuzz"
    assert kind == "distribution_draft"
    assert fname.endswith(".json")


def test_key_preserves_odd_inputs():
    # No validation/escaping in _key — inputs pass through verbatim.
    key = _key("brand x", "k.i.n.d", "tar.gz")
    assert key.startswith("brand x/k.i.n.d/")
    assert key.endswith(".tar.gz")


# -- local backend ------------------------------------------------------------

def test_put_text_local_pointer_and_file(monkeypatch, tmp_path):
    store = _store(monkeypatch, _fake_settings(tmp_path))
    ptr = store.put_text(brand="hotcars", kind="report", ext="html",
                         text="<h1>hi</h1>", content_type="text/html")
    assert ptr["backend"] == "local"
    assert ptr["content_type"] == "text/html"
    assert ptr["bytes"] == len(b"<h1>hi</h1>")
    assert ptr["key"].startswith("hotcars/report/")
    assert ptr["uri"].startswith("file:")
    assert (tmp_path / ptr["key"]).read_bytes() == b"<h1>hi</h1>"


def test_put_text_default_content_type(monkeypatch, tmp_path):
    store = _store(monkeypatch, _fake_settings(tmp_path))
    ptr = store.put_text(brand="hotcars", kind="report", ext="txt", text="plain")
    assert ptr["content_type"] == "text/plain"


def test_put_text_utf8_byte_count(monkeypatch, tmp_path):
    store = _store(monkeypatch, _fake_settings(tmp_path))
    text = "héllo—✓"  # multibyte chars: byte length != char length
    ptr = store.put_text(brand="topspeed", kind="report", ext="txt", text=text)
    assert ptr["bytes"] == len(text.encode("utf-8"))
    assert ptr["bytes"] != len(text)
    assert (tmp_path / ptr["key"]).read_bytes().decode("utf-8") == text


def test_put_text_empty_input(monkeypatch, tmp_path):
    store = _store(monkeypatch, _fake_settings(tmp_path))
    ptr = store.put_text(brand="hotcars", kind="report", ext="txt", text="")
    assert ptr["bytes"] == 0
    assert (tmp_path / ptr["key"]).read_bytes() == b""


def test_put_bytes_local(monkeypatch, tmp_path):
    store = _store(monkeypatch, _fake_settings(tmp_path))
    data = b"\x00\x01\x02binary\xff"
    ptr = store.put_bytes(brand="carbuzz", kind="image", ext="bin", data=data)
    assert ptr["backend"] == "local"
    assert ptr["content_type"] == "application/octet-stream"  # default
    assert ptr["bytes"] == len(data)
    assert (tmp_path / ptr["key"]).read_bytes() == data


def test_put_creates_nested_parent_dirs(monkeypatch, tmp_path):
    store = _store(monkeypatch, _fake_settings(tmp_path))
    ptr = store.put_text(brand="hotcars", kind="report", ext="txt", text="x")
    written = tmp_path / ptr["key"]
    assert written.exists()
    assert written.parent.is_dir()  # brand/kind subdirs were created


def test_backend_read_from_settings(monkeypatch, tmp_path):
    store = _store(monkeypatch, _fake_settings(tmp_path, backend="gcs"))
    assert store.backend == "gcs"


# -- gcs backend --------------------------------------------------------------

def test_gcs_falls_back_to_local_without_sdk(monkeypatch, tmp_path):
    # Force the `from google.cloud import storage` import to fail deterministically.
    monkeypatch.setitem(sys.modules, "google.cloud.storage", None)
    store = _store(monkeypatch, _fake_settings(tmp_path, backend="gcs"))
    ptr = store.put_text(brand="hotcars", kind="report", ext="txt", text="data")
    assert ptr["backend"] == "local"  # fell back, no exception
    assert (tmp_path / ptr["key"]).read_bytes() == b"data"


def test_gcs_success_with_mocked_client(monkeypatch, tmp_path):
    captured: dict = {}

    class FakeBlob:
        def __init__(self, key):
            captured["blob_key"] = key

        def upload_from_string(self, data, content_type=None):
            captured["data"] = data
            captured["content_type"] = content_type

    class FakeBucket:
        def __init__(self, name):
            captured["bucket"] = name

        def blob(self, key):
            return FakeBlob(key)

    class FakeClient:
        def __init__(self, project=None):
            captured["project"] = project

        def bucket(self, name):
            return FakeBucket(name)

    google_mod = types.ModuleType("google")
    cloud_mod = types.ModuleType("google.cloud")
    storage_mod = types.ModuleType("google.cloud.storage")
    storage_mod.Client = FakeClient
    cloud_mod.storage = storage_mod
    google_mod.cloud = cloud_mod
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_mod)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage_mod)

    store = _store(monkeypatch, _fake_settings(tmp_path, backend="gcs",
                                              bucket="my-bucket", project_id="proj-9"))
    ptr = store.put_bytes(brand="hotcars", kind="report", ext="pdf",
                         data=b"PDFDATA", content_type="application/pdf")

    assert ptr["backend"] == "gcs"
    assert ptr["uri"] == f"gs://my-bucket/{ptr['key']}"
    assert ptr["content_type"] == "application/pdf"
    assert ptr["bytes"] == len(b"PDFDATA")
    # client wired from settings; data streamed through the mock, nothing local.
    assert captured["project"] == "proj-9"
    assert captured["bucket"] == "my-bucket"
    assert captured["blob_key"] == ptr["key"]
    assert captured["data"] == b"PDFDATA"
    assert captured["content_type"] == "application/pdf"
    assert not list(tmp_path.rglob("*.pdf"))


if __name__ == "__main__":
    import inspect

    for name, fn in sorted(globals().items()):
        if (name.startswith("test_") and callable(fn)
                and not inspect.iscoroutinefunction(fn)
                and not inspect.signature(fn).parameters):
            fn()
            print(f"PASS {name}")
