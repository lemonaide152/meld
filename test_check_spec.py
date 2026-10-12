from pathlib import Path

import check_spec
from fastapi.testclient import TestClient

import server

ROOT = Path(__file__).resolve().parent


def test_repo_versions_agree():
    assert check_spec.main() == 0


def test_mismatch_fails():
    spec = (ROOT / "SPEC.md").read_text()
    srv = (ROOT / "server.py").read_text().replace('SPEC_VERSION = "2.0.0"', 'SPEC_VERSION = "2.0.1"')
    assert any("mismatch" in e for e in check_spec.problems(spec, srv))


def test_missing_block_version_fails():
    spec = (ROOT / "SPEC.md").read_text().replace("spec_version: 2.0.0\ntimestamp_format", "timestamp_format")
    assert any("meld-spec block" in e for e in check_spec.problems(spec, (ROOT / "server.py").read_text()))


def test_health_reports_spec_version():
    body = TestClient(server.app).get("/health").json()
    assert body == {"ok": True, "spec_version": "2.0.0", "conformance": "partial"}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
