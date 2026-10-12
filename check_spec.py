"""Check that SPEC.md and server.py agree on the spec version.

Run: python check_spec.py
Exits 1 if the front matter, the meld-spec block, and server.SPEC_VERSION
do not all carry the same spec_version, or if the version is not semver.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def versions(spec_text: str, server_text: str) -> dict:
    found = {}
    fm = re.match(r"^---\n(.*?)\n---\n", spec_text, re.S)
    if fm:
        m = re.search(r"^spec_version:\s*(\S+)\s*$", fm.group(1), re.M)
        if m:
            found["front matter"] = m.group(1)
    block = re.search(r"```meld-spec\n(.*?)```", spec_text, re.S)
    if block:
        m = re.search(r"^spec_version:\s*(\S+)\s*$", block.group(1), re.M)
        if m:
            found["meld-spec block"] = m.group(1)
    m = re.search(r'^SPEC_VERSION\s*=\s*"([^"]+)"', server_text, re.M)
    if m:
        found["server.py"] = m.group(1)
    return found


def problems(spec_text: str, server_text: str) -> list:
    found = versions(spec_text, server_text)
    errs = []
    for where in ("front matter", "meld-spec block", "server.py"):
        if where not in found:
            errs.append(f"spec_version missing from {where}")
    for where, v in found.items():
        if not SEMVER.match(v):
            errs.append(f"{where}: spec_version {v!r} is not semver")
    if len(set(found.values())) > 1:
        errs.append("spec_version mismatch: " + ", ".join(f"{k}={v}" for k, v in found.items()))
    if "## Changelog" not in spec_text:
        errs.append("SPEC.md has no Changelog section")
    elif found.get("front matter") and f"- {found['front matter']} " not in spec_text.split("## Changelog", 1)[1]:
        errs.append("Changelog has no entry for the current spec_version")
    return errs


def main() -> int:
    errs = problems((ROOT / "SPEC.md").read_text(), (ROOT / "server.py").read_text())
    for e in errs:
        print("check_spec:", e, file=sys.stderr)
    if not errs:
        print("check_spec: ok,", versions((ROOT / "SPEC.md").read_text(), (ROOT / "server.py").read_text())["server.py"])
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
