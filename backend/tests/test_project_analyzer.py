"""Project Analyzer: secure ZIP ingest -> manifest (unit + API)."""
from __future__ import annotations

import io
import secrets
import zipfile

import pytest
from fastapi.testclient import TestClient

import main as main_mod
import project_analyzer as pa

client = TestClient(main_mod.app)


class _PermissiveLimiter:
    def ip_allowed(self, ip): return True
    def hit_ip(self, ip): return None
    def register_allowed(self, ip): return True
    def record_registration(self, ip): return None
    def login_allowed(self, ip, email): return True
    def record_login_failure(self, ip, email): return None
    def clear_login_failures(self, ip, email): return None
    def purchase_allowed(self, uid): return True
    def record_purchase(self, uid): return None


@pytest.fixture(autouse=True)
def _permissive_limiter(monkeypatch):
    monkeypatch.setattr(main_mod, "limiter", _PermissiveLimiter())


def _zx(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in members.items():
            z.writestr(name, content)
    return buf.getvalue()


def _register():
    email = f"analyzer-{secrets.token_hex(4)}@fluxswarm.test"
    r = client.post("/api/auth/register",
                    json={"email": email, "name": "A",
                          "password": "s3cure-Pass-123", "tos_accept": True})
    assert r.status_code == 200, r.text
    return r.json()["token"]


# --- unit: analyze_zip ------------------------------------------------


def test_analyze_zip_basic_manifest():
    data = _zx({
        "src/app.py": "def f():\n    return 1\n",
        "src/util.js": "const x = 1;\n",
        "README.md": "# hi\n",
        "requirements.txt": "fastapi\n",
    })
    m = pa.analyze_zip(data)
    assert m["ok"] is True
    assert m["files_count"] == 4
    assert m["total_lines"] == 5
    assert "python" in {l["lang"] for l in m["languages"]}
    assert "requirements.txt" in [f["path"] for f in m["files"]]
    assert "python (pip)" in m["frameworks"]
    assert m["top_level_entries"] == ["README.md", "requirements.txt", "src"]


def test_analyze_zip_rejects_non_zip():
    with pytest.raises(pa.ProjectAnalyzerError):
        pa.analyze_zip(b"not a zip archive at all")


def test_analyze_zip_rejects_traversal():
    data = _zx({"../../etc/passwd": "root:x"})
    with pytest.raises(pa.ProjectAnalyzerError, match="unsafe member path"):
        pa.analyze_zip(data)


def test_analyze_zip_rejects_absolute_and_backslash():
    for name in ("/etc/passwd", "C:/Windows/win.ini", "a\\..\\b"):
        with pytest.raises(pa.ProjectAnalyzerError):
            pa.analyze_zip(_zx({name: "x"}))


def test_analyze_zip_rejects_duplicate_case_folded():
    data = _zx({"a.txt": "1", "A.TXT": "2"})
    with pytest.raises(pa.ProjectAnalyzerError, match="duplicate member path"):
        pa.analyze_zip(data)


def test_analyze_zip_rejects_oversize_member(monkeypatch):
    monkeypatch.setattr(pa, "MAX_MEMBER_BYTES", 10)
    data = _zx({"big.txt": "x" * 20})
    with pytest.raises(pa.ProjectAnalyzerError, match="member too large"):
        pa.analyze_zip(data)


def test_analyze_zip_rejects_oversize_archive(monkeypatch):
    monkeypatch.setattr(pa, "MAX_ARCHIVE_BYTES", 50)
    data = _zx({"ok.txt": "x" * 200})
    with pytest.raises(pa.ProjectAnalyzerError, match="archive too large"):
        pa.analyze_zip(data)


# --- API --------------------------------------------------------------


def test_api_analyze_requires_auth():
    r = client.post("/api/project/analyze")
    assert r.status_code == 401


def test_api_analyze_ok_and_counts():
    token = _register()
    data = _zx({"app.py": "print(1)\n", "static/site.css": "body{}\n"})
    r = client.post("/api/project/analyze", headers={"Authorization": f"Bearer {token}"},
                    files={"file": ("proj.zip", data, "application/zip")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["files_count"] == 2
    assert body["truncated"] is False


def test_api_analyze_rejects_hostile_zip():
    token = _register()
    data = _zx({"../escape.txt": "x"})
    r = client.post("/api/project/analyze", headers={"Authorization": f"Bearer {token}"},
                    files={"file": ("evil.zip", data, "application/zip")})
    assert r.status_code == 400
    assert "unsafe member path" in r.json()["detail"]


def test_api_analyze_rejects_empty_form():
    token = _register()
    r = client.post("/api/project/analyze", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400
    assert "No project file uploaded" in r.json()["detail"]