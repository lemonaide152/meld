"""Pull-request preview Workers and D1 databases for meld.

Names are always ``meld-prev-<guid>``. Production Worker ``meld`` and D1
``meld-prod`` are refused before any Cloudflare call. Stripe live keys and
X402 secrets are not written into the preview config and are removed from
the environment used to run Wrangler.

Teardown is a CI job (see ops/PREVIEW-DEPLOYS.md). This module is the only
supported way to create or delete those preview resources.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

PROD_WORKER = "meld"
PROD_D1_NAME = "meld-prod"
PROD_D1_ID = "a550ad8f-9359-4666-ac10-c440ad461464"
PROD_HOSTS = frozenset({
    "meld.mergeinc.workers.dev",
    "meld.sh",
    "www.meld.sh",
    "meld.workers.dev",
})

NAME_PREFIX = "meld-prev-"
GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
COMMENT_GUID_RE = re.compile(
    r"<!--\s*meld-preview-guid:\s*"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s*-->",
    re.IGNORECASE,
)
PREVIEW_MARKER = "<!-- meld-preview -->"
ACCOUNT_RE = re.compile(r"^[a-f0-9]{32}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
WORKERS_DEV_URL_RE = re.compile(r"https://[a-z0-9.-]+\.workers\.dev\b")

WRANGLER_VERSION = "4.135.0"
CF_API = "https://api.cloudflare.com/client/v4"
DEPLOY_DIR = Path(__file__).resolve().parent

STAGING_SKIP = {
    "wrangler.toml",
    "wrangler.preview.toml",
    "preview_ops.py",
    "test_preview_ops.py",
    "node_modules",
    ".wrangler",
    "python_modules",
    "__pycache__",
    ".env",
    ".dev.vars",
    ".env.local",
}

HELP = """\
Usage: preview_ops.py <command>

  resolve-guid       Print the pull request's preview GUID (mint one if needed)
  comment            Upsert the preview PR comment for PREVIEW_GUID
  deploy             Create or update the isolated preview Worker and D1
  cleanup            Delete the preview Worker and D1 for PREVIEW_GUID
  cleanup-pr         Delete whatever preview GUIDs the PR comment records
  cleanup-promote    Delete previews for pull requests merged in COMMIT_SHA

Production Worker meld, D1 meld-prod, and live Stripe / X402 secrets are
never targets. Cleanup is performed by GitHub Actions, not by hand.
"""


class CloudflareError(RuntimeError):
    def __init__(self, status: int, payload):
        super().__init__(f"cloudflare api http {status}")
        self.status = status
        self.payload = payload


def validate_guid(guid: str) -> str:
    cleaned = (guid or "").strip().lower()
    if not GUID_RE.match(cleaned):
        raise SystemExit("preview guid must be a UUID")
    return cleaned


def resource_names(guid: str) -> tuple[str, str]:
    """Worker name and D1 name. Both carry the same preview GUID."""
    cleaned = validate_guid(guid)
    name = f"{NAME_PREFIX}{cleaned}"
    guard_preview_name(name)
    return name, name


def guard_preview_name(name: str) -> None:
    if name in {PROD_WORKER, PROD_D1_NAME} or PROD_D1_NAME in name:
        raise SystemExit(f"refusing production resource name {name}")
    if not name.startswith(NAME_PREFIX):
        raise SystemExit(f"refusing non-preview resource name {name}")
    validate_guid(name[len(NAME_PREFIX):])
    if len(name) > 63:
        raise SystemExit(f"preview name exceeds 63 characters: {name}")


def guard_database_id(database_id: str) -> str:
    cleaned = (database_id or "").strip().lower()
    if not GUID_RE.match(cleaned):
        raise SystemExit("D1 database id must be a UUID")
    if cleaned == PROD_D1_ID:
        raise SystemExit("refusing production D1 meld-prod")
    return cleaned


def render_config(guid: str, database_id: str) -> str:
    worker, d1 = resource_names(guid)
    database_id = guard_database_id(database_id)
    text = (
        "# Generated for one pull-request preview. Never deploy this as production.\n"
        f'name = "{worker}"\n'
        'main = "worker.py"\n'
        'compatibility_date = "2026-09-18"\n'
        'compatibility_flags = ["python_workers"]\n'
        "\n"
        "workers_dev = true\n"
        "\n"
        "[[d1_databases]]\n"
        'binding = "DB"\n'
        f'database_name = "{d1}"\n'
        f'database_id = "{database_id}"\n'
        "\n"
        "[vars]\n"
        'MELD_PREVIEW = "1"\n'
    )
    assert_config(text, worker=worker, database_id=database_id)
    return text


def assert_config(text: str, worker: str | None = None, database_id: str | None = None) -> None:
    lowered = text.lower()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in {f'name = "{PROD_WORKER}"', f"name = '{PROD_WORKER}'"}:
            raise SystemExit("preview config names the production Worker meld")
    if PROD_D1_NAME.lower() in lowered:
        raise SystemExit("preview config references production D1 meld-prod")
    if PROD_D1_ID.lower() in lowered:
        raise SystemExit("preview config references the production D1 id")
    for snippet in (
        "stripe_secret_key",
        "stripe_webhook_secret",
        "stripe_price_",
        "x402",
        "sk_live_",
        "whsec_",
    ):
        if snippet in lowered:
            raise SystemExit(f"preview config contains forbidden material ({snippet})")
    if "workers_dev = false" in lowered or "workers_dev=false" in lowered:
        raise SystemExit("preview config disables workers.dev")
    if "[[routes]]" in lowered or re.search(r"(?m)^\s*route\s*=", lowered):
        raise SystemExit("preview config must not set routes")
    if worker is not None and f'name = "{worker}"' not in text:
        raise SystemExit("preview config worker name mismatch")
    if database_id is not None and f'database_id = "{database_id}"' not in text:
        raise SystemExit("preview config database id mismatch")


def scrubbed_env(base: dict | None = None) -> dict:
    """Drop payment secrets so Wrangler cannot publish them onto a preview."""
    env = dict(os.environ if base is None else base)
    for key in list(env):
        upper = key.upper()
        if upper.startswith("STRIPE") or "X402" in upper:
            del env[key]
    return env


def wrangler_env() -> dict:
    env = scrubbed_env()
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "GH_PAT", "NPM_TOKEN"):
        env.pop(key, None)
    if "CLOUDFLARE_API_TOKEN" not in env or "CLOUDFLARE_ACCOUNT_ID" not in env:
        raise SystemExit("Cloudflare credentials missing from the Wrangler environment")
    return env


def sql_statements(sql: str) -> list[str]:
    """Split schema.sql on semicolons after stripping ``--`` comments."""
    kept_lines = []
    for line in sql.splitlines():
        if "--" in line:
            line = line[: line.index("--")]
        kept_lines.append(line)
    parts = [part.strip() for part in "\n".join(kept_lines).split(";")]
    return [part for part in parts if part]


def comment_body(guid: str, url: str | None) -> str:
    worker, d1 = resource_names(guid)
    if url:
        assert_preview_url(url, worker)
        link = url
    else:
        link = "Deploy in progress. Meld CI will replace this line with the preview URL."
    return (
        "### meld preview\n"
        "\n"
        f"{link}\n"
        "\n"
        f"- Worker `{worker}`\n"
        f"- D1 `{d1}`\n"
        "\n"
        "This preview uses its own Worker and its own D1 database. "
        "It does not use production Worker `meld`, production D1 `meld-prod`, "
        "or live Stripe / X402 secrets.\n"
        "\n"
        "**Cleanup is automatic.** Meld CI (the meld agent / GitHub Actions) "
        "deletes this Worker and this D1 when the pull request closes, merged "
        "or not, and deletes them again on the merge promote path. "
        "You do not clean them up yourself.\n"
        "\n"
        f"{PREVIEW_MARKER}\n"
        f"<!-- meld-preview-guid: {validate_guid(guid)} -->\n"
    )


def cleaned_body(guids: list[str]) -> str:
    lines = [
        "### meld preview removed",
        "",
        "Meld CI deleted the preview resources for this pull request. "
        "Nothing is left for you to remove.",
        "",
    ]
    for guid in guids:
        worker, d1 = resource_names(guid)
        lines.append(f"- Deleted Worker `{worker}` and D1 `{d1}`")
    lines.append("")
    lines.append("<!-- meld-preview-cleaned -->")
    return "\n".join(lines) + "\n"


def assert_preview_url(url: str, worker: str) -> str:
    guard_preview_name(worker)
    if not url.startswith("https://"):
        raise SystemExit("preview URL must use https")
    host = url[len("https://"):].split("/")[0].lower()
    if host in PROD_HOSTS or host.startswith("meld."):
        raise SystemExit(f"refusing production URL host {host}")
    prefix = worker + "."
    if not host.startswith(prefix) or not host.endswith(".workers.dev"):
        raise SystemExit(f"preview URL does not match {worker}")
    rest = host[len(prefix):]
    labels = rest.split(".")
    # <account-subdomain>.workers.dev
    if labels[-2:] != ["workers", "dev"] or len(labels) != 3:
        raise SystemExit(f"unexpected preview host {host}")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", labels[0]):
        raise SystemExit(f"unexpected workers.dev subdomain in {host}")
    return f"https://{host}"


def preview_url(worker: str, subdomain: str) -> str:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", subdomain):
        raise SystemExit("workers.dev subdomain is not a single DNS label")
    return assert_preview_url(f"https://{worker}.{subdomain}.workers.dev", worker)


def urls_from_deploy_log(log: str, worker: str) -> list[str]:
    guard_preview_name(worker)
    for host in PROD_HOSTS:
        if f"://{host}" in log.lower():
            raise SystemExit(f"deploy log contained production host {host}")
    found = []
    for match in WORKERS_DEV_URL_RE.finditer(log.lower()):
        try:
            url = assert_preview_url(match.group(0), worker)
        except SystemExit:
            continue
        if url not in found:
            found.append(url)
    return found


def is_ci_comment(comment: dict) -> bool:
    user = comment.get("user") or {}
    if user.get("type") == "Bot":
        return True
    return str(user.get("login") or "").endswith("[bot]")


def guids_from_comments(comments: list[dict]) -> list[str]:
    found: list[str] = []
    for comment in comments:
        if not is_ci_comment(comment):
            continue
        body = comment.get("body") or ""
        for match in COMMENT_GUID_RE.finditer(body):
            guid = validate_guid(match.group(1))
            if guid not in found:
                found.append(guid)
    return found


def production_wrangler_config() -> Path:
    return (DEPLOY_DIR / "wrangler.toml").resolve()


def is_production_wrangler_config(path: Path) -> bool:
    """True only for the repo's production Worker config, not a staging copy."""
    try:
        candidate = path.expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        return candidate.resolve() == production_wrangler_config()
    except OSError:
        return False


def deploy_command(config: Path, worker: str) -> list[str]:
    """Pinned Wrangler deploy of a preview config named wrangler.toml.

    ``pywrangler sync`` only looks for ``wrangler.toml`` or ``wrangler.jsonc``.
    The file in the staging directory is generated preview config. It is not
    ``deploy/wrangler.toml``.
    """
    guard_preview_name(worker)
    if config.name != "wrangler.toml":
        raise SystemExit(
            "preview Wrangler config must be named wrangler.toml so pywrangler sync can find it"
        )
    if not config.is_file():
        raise SystemExit("missing preview Wrangler config")
    if is_production_wrangler_config(config):
        raise SystemExit("refusing to deploy the production wrangler.toml")
    assert_config(config.read_text(encoding="utf-8"), worker=worker)
    args = [
        "npx",
        "--yes",
        f"wrangler@{WRANGLER_VERSION}",
        "deploy",
        "--name",
        worker,
        "--config",
        str(config),
        "--no-experimental-provision",
        "--no-experimental-auto-create",
        "--no-autoconfig",
    ]
    assert_wrangler_argv(args, worker)
    return args


def assert_wrangler_argv(args: list[str], worker: str) -> None:
    if "deploy" not in args:
        raise SystemExit("expected wrangler deploy of a separate preview Worker")
    # `wrangler preview` attaches a preview to the existing Worker (`meld`).
    # Deploy a separate script instead. The package spec `wrangler@<version>`
    # is not the subcommand.
    if "--preview" in args or "preview" in args:
        raise SystemExit("refusing wrangler preview; it targets the production Worker")
    for arg in args:
        if arg in {PROD_WORKER, PROD_D1_NAME, PROD_D1_ID}:
            raise SystemExit("wrangler arguments reference production")
        lowered = arg.lower()
        if "meld.mergeinc" in lowered:
            raise SystemExit("wrangler arguments reference production config")
        if lowered.endswith("wrangler.toml") and is_production_wrangler_config(Path(arg)):
            raise SystemExit("wrangler arguments reference production config")
    if worker not in args:
        raise SystemExit("wrangler arguments missing the preview Worker name")
    if "--config" not in args:
        raise SystemExit("wrangler deploy requires an explicit preview config")


def _ignore_staging(_directory: str, names: list[str]) -> set[str]:
    skipped = set()
    for name in names:
        if name in STAGING_SKIP or name.startswith(".venv") or name.startswith(".env"):
            skipped.add(name)
    return skipped


def stage_sources() -> Path:
    """Copy the Worker source without the production Wrangler config."""
    staging = Path(tempfile.mkdtemp(prefix="meld-preview-"))
    for child in DEPLOY_DIR.iterdir():
        if child.name in _ignore_staging(str(DEPLOY_DIR), [child.name]):
            continue
        dest = staging / child.name
        if child.is_dir():
            shutil.copytree(child, dest, ignore=_ignore_staging)
        else:
            shutil.copy2(child, dest)
    if (staging / "wrangler.toml").exists() or (staging / ".dev.vars").exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit("staging included production Wrangler config or secrets")
    if not (staging / "worker.py").is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit("staging is missing worker.py")
    return staging


def require_cf() -> tuple[str, str]:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip().lower()
    if not token or not account:
        raise SystemExit(
            "CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID must be GitHub Actions "
            "secrets. See ops/PREVIEW-DEPLOYS.md."
        )
    if not ACCOUNT_RE.match(account):
        raise SystemExit("CLOUDFLARE_ACCOUNT_ID must be a 32-character hex account id")
    return token, account


def _cf_error_text(payload) -> str:
    try:
        return json.dumps(payload).lower()
    except TypeError:
        return str(payload).lower()


def is_not_found(exc: CloudflareError) -> bool:
    if exc.status == 404:
        return True
    text = _cf_error_text(exc.payload)
    return "not found" in text or "does not exist" in text or '"code": 10007' in text


def is_already_exists(exc: CloudflareError) -> bool:
    text = _cf_error_text(exc.payload)
    return "already exists" in text or "duplicate" in text


def is_benign_schema_error(exc: CloudflareError) -> bool:
    text = _cf_error_text(exc.payload)
    return "already exists" in text or "duplicate column" in text


def cf_json(method: str, path: str, body: dict | None = None):
    token, account = require_cf()
    path_only = path.split("?", 1)[0]
    if not path_only.startswith(f"/accounts/{account}/"):
        raise SystemExit("refusing Cloudflare request outside the configured account")
    if PROD_D1_ID in path_only:
        raise SystemExit("refusing Cloudflare request for production D1 meld-prod")
    if path_only.rstrip("/").endswith("/workers/scripts/meld"):
        raise SystemExit("refusing Cloudflare request for production Worker meld")
    if path_only.rstrip("/").endswith(f"/d1/database/{PROD_D1_NAME}"):
        raise SystemExit("refusing Cloudflare request for production D1 meld-prod")
    url = CF_API + path
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "meld-preview-ops",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode()
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:800]
        try:
            payload = json.loads(detail) if detail else {}
        except json.JSONDecodeError:
            payload = {"raw": detail}
        raise CloudflareError(exc.code, payload) from None
    payload = json.loads(raw) if raw else {}
    if isinstance(payload, dict) and payload.get("success") is False:
        raise CloudflareError(status, payload)
    return payload


def _result_rows(payload) -> list:
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict):
        if isinstance(result.get("results"), list):
            return result["results"]
        return [result]
    rows = []
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and item.get("success") is False:
                raise CloudflareError(200, item)
            if isinstance(item, dict) and isinstance(item.get("results"), list):
                rows.extend(item["results"])
            elif isinstance(item, dict):
                rows.append(item)
    return rows


def list_databases(name: str) -> list[dict]:
    guard_preview_name(name)
    _, account = require_cf()
    matches = []
    for page in range(1, 11):
        query = urllib.parse.urlencode({"name": name, "page": str(page), "per_page": "100"})
        payload = cf_json("GET", f"/accounts/{account}/d1/database?{query}")
        rows = _result_rows(payload)
        if not rows:
            break
        for row in rows:
            if row.get("name") == name:
                matches.append(row)
        if len(rows) < 100:
            break
    return matches


def database_id_of(row: dict, expected_name: str) -> str:
    if row.get("name") not in (None, expected_name):
        raise SystemExit("D1 response name did not match the preview")
    db_id = row.get("uuid") or row.get("database_id") or ""
    return guard_database_id(str(db_id))


def ensure_d1(name: str) -> str:
    guard_preview_name(name)
    _, account = require_cf()
    existing = list_databases(name)
    if len(existing) > 1:
        raise SystemExit(f"multiple D1 databases are named {name}")
    if len(existing) == 1:
        return database_id_of(existing[0], name)
    try:
        payload = cf_json(
            "POST",
            f"/accounts/{account}/d1/database",
            {"name": name},
        )
    except CloudflareError as exc:
        if not is_already_exists(exc):
            raise
        existing = list_databases(name)
        if len(existing) != 1:
            raise SystemExit(f"could not find a single D1 named {name}") from None
        return database_id_of(existing[0], name)
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise SystemExit("D1 create response did not include a database id")
    return database_id_of(result, name)


def apply_schema(database_id: str) -> None:
    database_id = guard_database_id(database_id)
    _, account = require_cf()
    schema_path = DEPLOY_DIR / "schema.sql"
    statements = sql_statements(schema_path.read_text(encoding="utf-8"))
    if not statements:
        raise SystemExit("schema.sql produced no statements")
    for statement in statements:
        try:
            payload = cf_json(
                "POST",
                f"/accounts/{account}/d1/database/{database_id}/query",
                {"sql": statement},
            )
            result = payload.get("result") if isinstance(payload, dict) else None
            if isinstance(result, list):
                for item in result:
                    if isinstance(item, dict) and item.get("success") is False:
                        raise CloudflareError(200, item)
        except CloudflareError as exc:
            if is_benign_schema_error(exc):
                continue
            sys.stderr.write(
                f"preview schema statement failed: http {exc.status} "
                f"{_cf_error_text(exc.payload)[:300]}\n"
            )
            raise SystemExit("applying preview schema failed") from None


def delete_worker(name: str, attempts: int = 5, delay: float = 3) -> None:
    guard_preview_name(name)
    _, account = require_cf()
    quoted = urllib.parse.quote(name)
    path = f"/accounts/{account}/workers/scripts/{quoted}?force=true"
    _delete_with_retry(path, attempts=attempts, delay=delay)


def delete_d1(name: str, attempts: int = 5, delay: float = 3) -> None:
    guard_preview_name(name)
    _, account = require_cf()
    rows = list_databases(name)
    if len(rows) > 1:
        raise SystemExit(f"multiple D1 databases are named {name}")
    if not rows:
        return
    db_id = database_id_of(rows[0], name)
    path = f"/accounts/{account}/d1/database/{db_id}"
    _delete_with_retry(path, attempts=attempts, delay=delay)


def _delete_with_retry(path: str, attempts: int, delay: float) -> None:
    last: CloudflareError | None = None
    for attempt in range(attempts):
        try:
            cf_json("DELETE", path)
            return
        except CloudflareError as exc:
            if is_not_found(exc):
                return
            last = exc
            if attempt + 1 == attempts:
                break
            time.sleep(delay)
    raise SystemExit("deleting a preview resource failed") from last


def workers_subdomain() -> str:
    _, account = require_cf()
    payload = cf_json("GET", f"/accounts/{account}/workers/subdomain")
    result = payload.get("result") if isinstance(payload, dict) else None
    sub = result.get("subdomain") if isinstance(result, dict) else None
    if not isinstance(sub, str):
        raise SystemExit("could not read the workers.dev subdomain")
    return sub.strip().lower()


def vendor_preview_deps(staging: Path) -> None:
    """Run ``pywrangler sync`` in staging and keep only ``python_modules``.

    Production deploys with ``uv run pywrangler deploy``, which vendors fastapi
    into ``python_modules/`` before upload. Preview does the same sync, then
    removes ``.venv`` and ``.venv-workers`` so those environments are not
    uploaded. The staging ``wrangler.toml`` must already be the preview config.
    """
    staging = staging.resolve()
    if staging == DEPLOY_DIR.resolve():
        raise SystemExit("refusing to vendor dependencies in the production deploy directory")
    config = staging / "wrangler.toml"
    if not config.is_file() or is_production_wrangler_config(config):
        raise SystemExit("preview wrangler.toml is missing")
    assert_config(config.read_text(encoding="utf-8"))
    if not (staging / "pyproject.toml").is_file() or not (staging / "pylock.toml").is_file():
        raise SystemExit("staging is missing pyproject.toml or pylock.toml")
    env = scrubbed_env()
    for key in ("VIRTUAL_ENV", "CONDA_PREFIX", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "PYTHONHOME"):
        env.pop(key, None)
    try:
        proc = subprocess.run(
            ["uv", "run", "--group", "dev", "pywrangler", "sync"],
            cwd=staging,
            env=env,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise SystemExit(
            "uv is required to vendor preview dependencies. "
            "The preview workflow installs it with astral-sh/setup-uv."
        ) from None
    if proc.returncode != 0:
        raise SystemExit(f"pywrangler sync failed with exit {proc.returncode}")
    for name in (".venv", ".venv-workers"):
        path = staging / name
        if path.exists():
            shutil.rmtree(path)
    fastapi = staging / "python_modules" / "fastapi"
    if not fastapi.is_dir():
        raise SystemExit("pywrangler sync did not vendor python_modules/fastapi")
    assert_config(config.read_text(encoding="utf-8"))


def run_wrangler(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        args,
        cwd=cwd,
        env=wrangler_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise SystemExit(f"wrangler failed with exit {proc.returncode}")
    return f"{proc.stdout or ''}\n{proc.stderr or ''}"


def deploy(guid: str) -> dict:
    guid = validate_guid(guid)
    worker, d1 = resource_names(guid)
    database_id = ensure_d1(d1)
    staging = stage_sources()
    try:
        config = staging / "wrangler.toml"
        config.write_text(render_config(guid, database_id), encoding="utf-8")
        apply_schema(database_id)
        vendor_preview_deps(staging)
        log = run_wrangler(deploy_command(config, worker), staging)
        logged = urls_from_deploy_log(log, worker)
        try:
            constructed = preview_url(worker, workers_subdomain())
        except (SystemExit, CloudflareError):
            constructed = ""
        if logged and constructed and logged[0] != constructed:
            url = logged[0]
        elif constructed:
            url = constructed
        elif logged:
            url = logged[0]
        else:
            raise SystemExit("deploy finished without a workers.dev preview URL")
        url = assert_preview_url(url, worker)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    result = {
        "guid": guid,
        "worker": worker,
        "d1": d1,
        "database_id": database_id,
        "url": url,
    }
    print(f"preview worker {worker}")
    print(f"preview d1 {d1}")
    print(f"preview url {url}")
    return result


def cleanup_guid(guid: str, attempts: int = 5, delay: float = 3) -> None:
    worker, d1 = resource_names(guid)
    # Delete the Worker first so the URL stops serving, then drop its database.
    delete_worker(worker, attempts=attempts, delay=delay)
    delete_d1(d1, attempts=attempts, delay=delay)
    print(f"deleted preview worker {worker} and d1 {d1}")


def set_output(name: str, value: str) -> None:
    if "\n" in value:
        raise SystemExit(f"refusing to write multiline output {name}")
    print(f"{name}={value}")
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def require_repo() -> str:
    repo = os.environ.get("REPO", "")
    if not REPO_RE.match(repo):
        raise SystemExit("REPO must be owner/name")
    return repo


def require_pr() -> int:
    raw = os.environ.get("PR_NUMBER", "")
    if not raw.isdigit() or int(raw) <= 0:
        raise SystemExit("PR_NUMBER must be a positive integer")
    return int(raw)


def require_sha() -> str:
    sha = os.environ.get("COMMIT_SHA", "").strip().lower()
    if not SHA_RE.match(sha):
        raise SystemExit("COMMIT_SHA must be a 40-character hex sha")
    return sha


def gh_request(method: str, path: str, body: dict | None = None):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN is required for pull request comments")
    url = "https://api.github.com" + path
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "meld-preview-ops",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise SystemExit(f"GitHub API {method} {path} failed: {exc.code} {detail}") from None
    return json.loads(raw) if raw else None


def list_comments(repo: str, pr: int) -> list[dict]:
    comments: list[dict] = []
    for page in range(1, 21):
        batch = gh_request(
            "GET",
            f"/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}",
        )
        if not batch:
            break
        comments.extend(batch)
        if len(batch) < 100:
            break
    return comments


def upsert_preview_comment(repo: str, pr: int, guid: str, url: str | None) -> None:
    body = comment_body(guid, url)
    existing = None
    for comment in list_comments(repo, pr):
        if is_ci_comment(comment) and PREVIEW_MARKER in (comment.get("body") or ""):
            existing = comment
            break
    if existing:
        gh_request(
            "PATCH",
            f"/repos/{repo}/issues/comments/{existing['id']}",
            {"body": body},
        )
        return
    gh_request("POST", f"/repos/{repo}/issues/{pr}/comments", {"body": body})


def upsert_cleaned_comment(repo: str, pr: int, guids: list[str]) -> None:
    body = cleaned_body(guids)
    existing = None
    for comment in list_comments(repo, pr):
        if is_ci_comment(comment) and "<!-- meld-preview-cleaned -->" in (comment.get("body") or ""):
            existing = comment
            break
    if existing:
        gh_request(
            "PATCH",
            f"/repos/{repo}/issues/comments/{existing['id']}",
            {"body": body},
        )
        return
    gh_request("POST", f"/repos/{repo}/issues/{pr}/comments", {"body": body})


def resolve_guid(repo: str, pr: int) -> str:
    found = guids_from_comments(list_comments(repo, pr))
    if found:
        return found[0]
    return str(uuid.uuid4())


def pull_request_is_closed(repo: str, pr: int) -> bool:
    payload = gh_request("GET", f"/repos/{repo}/issues/{pr}")
    return (payload or {}).get("state") != "open"


def cleanup_pr(repo: str, pr: int, attempts: int = 5, delay: float = 3) -> list[str]:
    guids = guids_from_comments(list_comments(repo, pr))
    if not guids:
        print(f"no preview registered on pull request {pr}")
        return []
    for guid in guids:
        cleanup_guid(guid, attempts=attempts, delay=delay)
    upsert_cleaned_comment(repo, pr, guids)
    return guids


def prs_for_commit(repo: str, sha: str) -> list[int]:
    payload = gh_request("GET", f"/repos/{repo}/commits/{sha}/pulls")
    numbers = []
    for pr in payload or []:
        number = pr.get("number") if isinstance(pr, dict) else None
        if isinstance(number, int) and number > 0 and number not in numbers:
            numbers.append(number)
    return numbers


def cleanup_promote(repo: str, sha: str) -> None:
    """Delete previews for pull requests in a main-branch merge.

    This is the promote path. It does not deploy production.
    """
    numbers = prs_for_commit(repo, sha)
    if not numbers:
        print("no merged pull request on this commit; production was not deployed")
        return
    pending: list[tuple[int, list[str]]] = []
    for number in numbers:
        guids = guids_from_comments(list_comments(repo, number))
        if guids:
            pending.append((number, guids))
    if not pending:
        print("merged pull request has no preview to delete; production was not deployed")
        return
    require_cf()
    for number, guids in pending:
        for guid in guids:
            cleanup_guid(guid)
        upsert_cleaned_comment(repo, number, guids)


def _cmd_resolve_guid() -> int:
    guid = resolve_guid(require_repo(), require_pr())
    set_output("guid", guid)
    return 0


def _cmd_comment() -> int:
    guid = validate_guid(os.environ.get("PREVIEW_GUID", ""))
    url = os.environ.get("PREVIEW_URL") or None
    upsert_preview_comment(require_repo(), require_pr(), guid, url)
    return 0


def _preview_pr() -> tuple[str, int] | None:
    if os.environ.get("PR_NUMBER") and os.environ.get("REPO"):
        return require_repo(), require_pr()
    return None


def _remove_because_closed(guid: str, repo: str, pr: int, reason: str) -> int:
    cleanup_guid(guid)
    upsert_cleaned_comment(repo, pr, [guid])
    print(reason)
    return 0


def _cmd_deploy() -> int:
    guid = validate_guid(os.environ.get("PREVIEW_GUID", ""))
    pr = _preview_pr()
    # Close and deploy are different workflows, so a merge can land while
    # Wrangler is still uploading. Recheck and delete before leaving a preview up.
    if pr and pull_request_is_closed(pr[0], pr[1]):
        return _remove_because_closed(
            guid, pr[0], pr[1], "pull request is closed; preview removed"
        )
    result = deploy(guid)
    if pr and pull_request_is_closed(pr[0], pr[1]):
        return _remove_because_closed(
            guid, pr[0], pr[1], "pull request closed during deploy; preview removed"
        )
    set_output("preview_url", result["url"])
    set_output("worker", result["worker"])
    set_output("d1", result["d1"])
    if pr:
        upsert_preview_comment(pr[0], pr[1], guid, result["url"])
    return 0


def _cmd_cleanup() -> int:
    delay = float(os.environ.get("MELD_PREVIEW_RETRY_DELAY", "3"))
    cleanup_guid(validate_guid(os.environ.get("PREVIEW_GUID", "")), delay=delay)
    return 0


def _cmd_cleanup_pr() -> int:
    delay = float(os.environ.get("MELD_PREVIEW_RETRY_DELAY", "3"))
    cleanup_pr(require_repo(), require_pr(), delay=delay)
    return 0


def _cmd_cleanup_promote() -> int:
    cleanup_promote(require_repo(), require_sha())
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        sys.stdout.write(HELP)
        return 0 if args and args[0] in {"-h", "--help"} else 1
    commands = {
        "resolve-guid": _cmd_resolve_guid,
        "comment": _cmd_comment,
        "deploy": _cmd_deploy,
        "cleanup": _cmd_cleanup,
        "cleanup-pr": _cmd_cleanup_pr,
        "cleanup-promote": _cmd_cleanup_promote,
    }
    command = commands.get(args[0])
    if command is None:
        raise SystemExit(f"unknown command {args[0]}")
    if len(args) != 1:
        raise SystemExit("commands take their inputs from the environment, not flags")
    return command()


if __name__ == "__main__":
    raise SystemExit(main())
