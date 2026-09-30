"""Isolation checks for pull-request preview deploys. No Cloudflare calls."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import preview_ops

ROOT = Path(__file__).resolve().parents[1]
GUID = "018f3c2a-7b1e-4c3a-9d2e-1a2b3c4d5e6f"
PREVIEW_D1 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class NamesAndConfigTest(unittest.TestCase):
    def test_names_share_guid_and_are_not_production(self):
        worker, d1 = preview_ops.resource_names(GUID)
        self.assertEqual(worker, f"meld-prev-{GUID}")
        self.assertEqual(d1, worker)
        self.assertLessEqual(len(worker), 63)
        self.assertNotEqual(worker, preview_ops.PROD_WORKER)
        self.assertNotIn(preview_ops.PROD_D1_NAME, worker)

    def test_rejects_production_names_and_ids(self):
        for name in ("meld", "meld-prod", "meld-prev-not-a-guid", "other-018f3c2a-7b1e-4c3a-9d2e-1a2b3c4d5e6f"):
            with self.assertRaises(SystemExit):
                preview_ops.guard_preview_name(name)
        with self.assertRaises(SystemExit):
            preview_ops.guard_database_id(preview_ops.PROD_D1_ID)
        with self.assertRaises(SystemExit):
            preview_ops.validate_guid("meld")

    def test_render_is_isolated(self):
        text = preview_ops.render_config(GUID, PREVIEW_D1)
        self.assertIn(f'name = "meld-prev-{GUID}"', text)
        self.assertIn(f'database_name = "meld-prev-{GUID}"', text)
        self.assertIn(f'database_id = "{PREVIEW_D1}"', text)
        self.assertIn("workers_dev = true", text)
        lowered = text.lower()
        self.assertNotIn("meld-prod", lowered)
        self.assertNotIn(preview_ops.PROD_D1_ID, lowered)
        self.assertNotIn("stripe", lowered)
        self.assertNotIn("x402", lowered)
        self.assertNotIn("sk_live_", lowered)
        self.assertNotIn("[[routes]]", lowered)
        for line in text.splitlines():
            self.assertNotEqual(line.strip(), 'name = "meld"')

    def test_render_refuses_to_embed_production_d1(self):
        with self.assertRaises(SystemExit):
            preview_ops.render_config(GUID, preview_ops.PROD_D1_ID)


class EnvAndCommandTest(unittest.TestCase):
    def test_scrub_removes_payment_secrets_only(self):
        env = preview_ops.scrubbed_env({
            "CLOUDFLARE_API_TOKEN": "cf-token",
            "CLOUDFLARE_ACCOUNT_ID": "a" * 32,
            "STRIPE_SECRET_KEY": "sk_live_secret",
            "STRIPE_WEBHOOK_SECRET": "whsec_secret",
            "X402_PRIVATE_KEY": "0xabc",
            "PATH": "/usr/bin",
        })
        self.assertEqual(env["CLOUDFLARE_API_TOKEN"], "cf-token")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("STRIPE_SECRET_KEY", env)
        self.assertNotIn("STRIPE_WEBHOOK_SECRET", env)
        self.assertNotIn("X402_PRIVATE_KEY", env)

    def test_deploy_command_is_a_separate_worker(self):
        staging = preview_ops.stage_sources()
        self.addCleanup(lambda: __import__("shutil").rmtree(staging, ignore_errors=True))
        self.assertFalse((staging / "wrangler.toml").exists())
        self.assertFalse((staging / ".dev.vars").exists())
        self.assertTrue((staging / "worker.py").is_file())
        config = staging / "wrangler.preview.toml"
        config.write_text(preview_ops.render_config(GUID, PREVIEW_D1), encoding="utf-8")
        worker, _d1 = preview_ops.resource_names(GUID)
        args = preview_ops.deploy_command(config, worker)
        self.assertIn("deploy", args)
        self.assertNotIn("preview", args)
        self.assertNotIn("--preview", args)
        self.assertEqual(args[args.index("--name") + 1], worker)
        self.assertTrue(str(args[args.index("--config") + 1]).endswith("wrangler.preview.toml"))
        self.assertIn("--no-experimental-provision", args)
        self.assertIn("--no-experimental-auto-create", args)
        self.assertIn("--no-autoconfig", args)
        self.assertNotIn(preview_ops.PROD_WORKER, args)
        self.assertNotIn(preview_ops.PROD_D1_NAME, args)
        self.assertNotIn(preview_ops.PROD_D1_ID, args)

    def test_wrangler_argv_rejects_production_and_preview_subcommand(self):
        worker, _d1 = preview_ops.resource_names(GUID)
        with self.assertRaises(SystemExit):
            preview_ops.assert_wrangler_argv(["wrangler", "deploy", "--name", "meld"], worker)
        with self.assertRaises(SystemExit):
            preview_ops.assert_wrangler_argv(
                ["wrangler", "preview", "--name", worker],
                worker,
            )


class SchemaAndUrlTest(unittest.TestCase):
    def test_schema_statements_cover_fresh_database(self):
        sql = (preview_ops.DEPLOY_DIR / "schema.sql").read_text(encoding="utf-8")
        statements = preview_ops.sql_statements(sql)
        joined = "\n".join(statements)
        self.assertIn("CREATE TABLE IF NOT EXISTS melds", joined)
        self.assertTrue(any(s.startswith("ALTER TABLE melds ADD COLUMN resolver_ip") for s in statements))
        self.assertNotIn("meld-prod", joined.lower())
        self.assertNotIn(preview_ops.PROD_D1_ID, joined)

    def test_preview_url_uses_worker_and_account_subdomain(self):
        worker, _d1 = preview_ops.resource_names(GUID)
        url = preview_ops.preview_url(worker, "mergeinc")
        self.assertEqual(url, f"https://{worker}.mergeinc.workers.dev")
        for host in preview_ops.PROD_HOSTS:
            with self.assertRaises(SystemExit):
                preview_ops.assert_preview_url(f"https://{host}", worker)

    def test_deploy_log_rejects_production_host(self):
        worker, _d1 = preview_ops.resource_names(GUID)
        log = f"Deployed {worker}\n  https://{worker}.mergeinc.workers.dev\n"
        self.assertEqual(
            preview_ops.urls_from_deploy_log(log, worker),
            [f"https://{worker}.mergeinc.workers.dev"],
        )
        with self.assertRaises(SystemExit):
            preview_ops.urls_from_deploy_log(
                "https://meld.mergeinc.workers.dev",
                worker,
            )


class CommentsTest(unittest.TestCase):
    def test_comment_records_guid_and_cleanup_owner(self):
        worker, _d1 = preview_ops.resource_names(GUID)
        url = f"https://{worker}.mergeinc.workers.dev"
        body = preview_ops.comment_body(GUID, url)
        self.assertIn(url, body)
        self.assertIn(f"<!-- meld-preview-guid: {GUID} -->", body)
        self.assertIn("Meld CI", body)
        self.assertIn("meld agent", body)
        self.assertIn("meld-prod", body)
        self.assertNotIn("https://meld.mergeinc.workers.dev", body)
        self.assertNotIn("sk_live_", body)
        self.assertNotIn("x402_", body.lower())

    def test_guids_come_only_from_ci_comments(self):
        comments = [
            {
                "user": {"login": "someone", "type": "User"},
                "body": f"<!-- meld-preview-guid: {GUID} -->",
            },
            {
                "user": {"login": "github-actions[bot]", "type": "Bot"},
                "body": preview_ops.comment_body(GUID, None),
            },
        ]
        self.assertEqual(preview_ops.guids_from_comments(comments), [GUID])
        cleaned = preview_ops.cleaned_body([GUID])
        self.assertNotIn("meld-preview-guid:", cleaned)
        self.assertIn("Meld CI deleted", cleaned)


class CloudflareGuardTest(unittest.TestCase):
    def setUp(self):
        self._saved = {
            key: os.environ.get(key)
            for key in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")
        }
        os.environ["CLOUDFLARE_API_TOKEN"] = "test-token"
        os.environ["CLOUDFLARE_ACCOUNT_ID"] = "a" * 32

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_api_refuses_production_paths_without_calling_network(self):
        account = "a" * 32
        with self.assertRaises(SystemExit):
            preview_ops.cf_json("DELETE", f"/accounts/{account}/workers/scripts/meld")
        with self.assertRaises(SystemExit):
            preview_ops.cf_json(
                "DELETE",
                f"/accounts/{account}/d1/database/{preview_ops.PROD_D1_ID}",
            )

    def test_apply_schema_refuses_production_database(self):
        with self.assertRaises(SystemExit):
            preview_ops.apply_schema(preview_ops.PROD_D1_ID)

    def test_apply_schema_continues_after_duplicate_column(self):
        calls = []

        def fake(_method, path, body=None):
            self.assertNotIn(preview_ops.PROD_D1_ID, path)
            self.assertIn("/d1/database/", path)
            sql = body["sql"]
            calls.append(sql)
            if sql.startswith("ALTER TABLE"):
                raise preview_ops.CloudflareError(
                    400,
                    {"errors": [{"message": "duplicate column name: resolver_ip"}]},
                )
            return {"success": True, "result": [{"success": True, "results": []}]}

        original = preview_ops.cf_json
        preview_ops.cf_json = fake
        try:
            preview_ops.apply_schema(PREVIEW_D1)
        finally:
            preview_ops.cf_json = original
        self.assertGreaterEqual(len(calls), 5)
        self.assertTrue(any(sql.startswith("CREATE TABLE IF NOT EXISTS melds") for sql in calls))
        self.assertTrue(any(sql.startswith("ALTER TABLE") for sql in calls))


class PromoteTest(unittest.TestCase):
    def test_promote_does_not_require_cloudflare_without_a_preview(self):
        calls = []
        original = (
            preview_ops.prs_for_commit,
            preview_ops.list_comments,
            preview_ops.require_cf,
            preview_ops.cleanup_guid,
        )

        def prs(_repo, _sha):
            return [12]

        def comments(_repo, _pr):
            return []

        def require():
            calls.append("cf")
            raise AssertionError("credentials should not be required")

        preview_ops.prs_for_commit = prs
        preview_ops.list_comments = comments
        preview_ops.require_cf = require
        preview_ops.cleanup_guid = lambda *a, **k: calls.append("delete")
        try:
            preview_ops.cleanup_promote("lemonaide152/meld", "ab" * 20)
        finally:
            (
                preview_ops.prs_for_commit,
                preview_ops.list_comments,
                preview_ops.require_cf,
                preview_ops.cleanup_guid,
            ) = original
        self.assertEqual(calls, [])

    def test_promote_deletes_recorded_preview_only(self):
        deleted = []
        original = (
            preview_ops.prs_for_commit,
            preview_ops.list_comments,
            preview_ops.require_cf,
            preview_ops.cleanup_guid,
            preview_ops.upsert_cleaned_comment,
        )
        body = preview_ops.comment_body(GUID, None)

        preview_ops.prs_for_commit = lambda _repo, _sha: [9]
        preview_ops.list_comments = lambda _repo, _pr: [
            {"user": {"login": "github-actions[bot]", "type": "Bot"}, "body": body}
        ]
        preview_ops.require_cf = lambda: ("token", "a" * 32)
        preview_ops.cleanup_guid = lambda guid, **_k: deleted.append(guid)
        preview_ops.upsert_cleaned_comment = lambda _repo, _pr, guids: deleted.append(tuple(guids))
        try:
            preview_ops.cleanup_promote("lemonaide152/meld", "cd" * 20)
        finally:
            (
                preview_ops.prs_for_commit,
                preview_ops.list_comments,
                preview_ops.require_cf,
                preview_ops.cleanup_guid,
                preview_ops.upsert_cleaned_comment,
            ) = original
        self.assertEqual(deleted, [GUID, (GUID,)])


class WorkflowDocTest(unittest.TestCase):
    def test_workflows_deploy_on_pr_and_clean_on_close_and_promote(self):
        workflows = ROOT / ".github" / "workflows"
        deploy = (workflows / "preview-deploy.yml").read_text(encoding="utf-8")
        cleanup = (workflows / "preview-cleanup.yml").read_text(encoding="utf-8")
        promote = (workflows / "preview-promote-cleanup.yml").read_text(encoding="utf-8")
        doc = (ROOT / "ops" / "PREVIEW-DEPLOYS.md").read_text(encoding="utf-8")

        for text in (deploy, cleanup, promote, doc):
            self.assertNotIn("pull_request_target", text)
            self.assertNotIn("STRIPE_SECRET_KEY", text)
            self.assertNotIn("sk_live_", text)

        self.assertIn("opened", deploy)
        self.assertIn("synchronize", deploy)
        self.assertIn("reopened", deploy)
        self.assertIn("preview_ops.py deploy", deploy)
        self.assertIn("head.repo.full_name == github.repository", deploy)
        self.assertIn("meld-preview-pr-", deploy)

        self.assertIn("closed", cleanup)
        self.assertIn("preview_ops.py cleanup-pr", cleanup)
        self.assertIn("workflow_dispatch", cleanup)
        self.assertNotIn("github.event.pull_request.merged", cleanup)

        self.assertIn("branches: [main]", promote)
        self.assertIn("preview_ops.py cleanup-promote", promote)
        self.assertNotIn("preview_ops.py deploy", promote)
        self.assertNotIn("wrangler deploy", promote)
        self.assertIn("does not deploy production", promote.lower())

        self.assertIn("CLOUDFLARE_API_TOKEN", doc)
        self.assertIn("CLOUDFLARE_ACCOUNT_ID", doc)
        self.assertIn("meld-prev-<guid>", doc)
        self.assertIn("meld-prod", doc)
        self.assertIn("meld agent", doc)
        self.assertIn("closes", doc.lower())


if __name__ == "__main__":
    unittest.main()
