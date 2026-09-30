# Preview deploys

Meld CI deploys each same-repo pull request to its own Cloudflare Worker and D1 database, comments the preview URL, and deletes both resources when the pull request closes. The merge promote path deletes them again. Teardown is owned by the meld agent / GitHub Actions. You do not delete preview Workers or D1 databases yourself, in the dashboard or otherwise.

These workflows do not deploy production.

## Secrets

Repository Actions secrets:

| Secret | Purpose |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | Cloudflare API token used only by the preview workflows |
| `CLOUDFLARE_ACCOUNT_ID` | Account that owns the Workers (32 hex characters) |

Token permissions on that account:

- Workers Scripts: Edit
- D1: Edit
- Workers Scripts: Read, if the token does not already include it (used to read the `workers.dev` subdomain)

Fork pull requests do not receive these secrets. Preview jobs run from `pull_request` on this repository only.

Do not put Stripe live keys, Stripe webhook secrets, or X402 secrets in these workflows. Preview checkout stays unconfigured on purpose.

## Names

One random UUID per pull request, stored in the CI comment and reused on later pushes.

| Resource | Name |
| --- | --- |
| Worker | `meld-prev-<guid>` |
| D1 | `meld-prev-<guid>` |
| URL | `https://meld-prev-<guid>.<account-subdomain>.workers.dev` |

Production is Worker `meld` and D1 `meld-prod` (`a550ad8f-9359-4666-ac10-c440ad461464`), served at `meld.mergeinc.workers.dev`. A preview on that account looks like `https://meld-prev-<guid>.mergeinc.workers.dev`. The GUID is in the Worker name, which is the first label of the hostname.

The preview config is generated in CI and is not `deploy/wrangler.toml`. It binds only the preview D1.

## Cleanup triggers

| Workflow | When | Action |
| --- | --- | --- |
| `.github/workflows/preview-deploy.yml` | Pull request opened, synchronized, or reopened | Deploy `meld-prev-<guid>` and comment the URL |
| `.github/workflows/preview-cleanup.yml` | Pull request closes, merged or not | Delete that Worker and D1 |
| `.github/workflows/preview-promote-cleanup.yml` | Push to `main` (approval and merge promote path) | Delete the merged pull request's preview again. Does not deploy production |

Deploy checks the pull request again after upload. If it closed while the deploy was running, that same job deletes the preview it just created.

If a cleanup run fails, re-run that GitHub Actions workflow. `workflow_dispatch` on Preview cleanup is the CI retry for one GUID. That re-run is still meld CI. A dashboard delete is not part of the process.

Workflows take effect for later pull requests once this change is on `main`. Opening this pull request can run Preview deploy from the branch; it still will not deploy Worker `meld`.
