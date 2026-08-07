# Self-hosting agami (local → team)

> ### 🧪 Early access (in testing)
> The self-hosted team server is **available to use today**, but it's newer than the local single-player
> path and we're still ironing it out with early users — expect the occasional rough edge. If you hit
> one (or have feedback), please [**open a GitHub issue**](https://github.com/AgamiAI/agami-core/issues).
> The local experience is the stable, generally-available path; this is the team layer on top of it.

Stand up the agami MCP server on your own host so your team can query your semantic model in Claude. We
ship Docker; you deploy it to your own VM/cloud. The default is **secure by construction**: Caddy gives you
automatic HTTPS and is the *only* public service — agami and Postgres stay on the internal network with no
exposed ports.

## What you need
- A host with Docker (a small cloud VM is plenty), with **only ports 80 and 443** open.
- A **hostname** you control (e.g. `agami.acme.com`) with a DNS **A-record → your VM's IP**. (No domain? Use
  the **Cloudflare Tunnel** profile below — no public IP or DNS needed.) TLS and OAuth require a name, not a
  bare IP.
- Your model — the local `agami-artifacts` folder you built with `agami-serve`.

## Deploy (the secure VM bundle — the default)
```bash
git clone <this repo> && cd <repo>/deploy
cp agami.env.example agami.env            # then edit: PUBLIC_BASE_URL (your hostname), admin email + password, DATASOURCE_URL (point at a read-only DB role — required; see Security notes)
ln -s /path/to/your/agami-artifacts ./artifacts   # or set AGAMI_ARTIFACTS_DIR in agami.env
./deploy.sh                     # validates agami.env, generates the signing secret, builds + starts
```
That's it. `deploy.sh` runs the preflight (it generates and **persists** `AGAMI_SIGNING_SECRET` — keep `agami.env`,
it's what keeps connected sessions valid across restarts) then `docker compose up`. The container migrates the
database, loads your model into Postgres, and serves. Once Caddy issues the certificate (a few seconds):

- **Your team:** add `https://<your-host>/mcp` as a custom connector in Claude and sign in.
- **You:** manage who's allowed at `https://<your-host>/admin` (sign in with the admin email + password).

## Updating the model
Edit the model locally in Claude → refresh your `artifacts` → `docker compose restart agami`. The container
re-ingests the model on boot. **No rebuild, no new VM, no database access** — the container does the load.

## Variants (toggle `COMPOSE_PROFILES` in `agami.env`)
| You want… | `agami.env` | command |
|---|---|---|
| **Secure VM** (default) | `COMPOSE_PROFILES=bundled-db,edge` | `./deploy.sh` |
| **External / managed Postgres** (e.g. a Cloud-SQL-like service) | `COMPOSE_PROFILES=edge` + set `APP_DATABASE_URL` (a plain `postgresql://…?sslmode=require` URL — not a cloud connector) | `./deploy.sh` |
| **No public IP** (Cloudflare Tunnel) | `COMPOSE_PROFILES=bundled-db,tunnel` + set `CLOUDFLARE_TUNNEL_TOKEN` | `./deploy.sh` |
| **Cloud Run / serverless** (platform gives TLS) | set `APP_DATABASE_URL`; deploy the built image with these env vars | (your platform) |

## Security notes
- **Only Caddy is public** (80/443). agami and Postgres have no published ports — the DB is never reachable
  from the internet. Lock down SSH separately.
- The admin console is gated by the configured admin email + a session cookie; the `/mcp` query surface is
  gated by per-user OAuth. Both need the HTTPS that Caddy provides.
- `agami.env` holds the signing secret and DB creds — it stays on your host and is never committed. **No data ever
  leaves your environment.**
- **Point `DATASOURCE_URL` at a read-only database role — this is the required posture for a deploy, not
  just a nicety.** agami only ever runs read-only SELECTs against your datasource, and the read-only role is
  the primary, non-bypassable control: a login that can only `SELECT` can't write, run DDL, `COPY`, reach
  files/network, or touch another database even if the app-layer guard were bypassed. Copy-paste
  `CREATE ROLE` / `GRANT SELECT` SQL per engine — and the app-role vs operator-role split — is in
  [../plugins/agami/shared/readonly-grants.md](../plugins/agami/shared/readonly-grants.md). This is your
  **datasource** role; agami's own store (`APP_DATABASE_URL`) is a *separate* database and keeps its normal
  **read-write** user.
- **Always deploy via `./deploy.sh`** (it runs the preflight). If you `docker compose up` directly without
  having run the preflight, `AGAMI_PUBLIC_HOST` is unset and Caddy fails to start (loudly, never insecurely) —
  run `python -m deploy_preflight agami.env` first.

## Runtime identity on serverless platforms

The **Cloud Run / serverless** variant above runs the container under a **cloud identity**, and every
managed-container platform hands you an over-privileged *default* one unless you say otherwise. Because
`/mcp` and `/admin` are internet-facing, a compromised container can read that identity's token from the
platform metadata endpoint and inherit all of its cloud permissions. So run agami under a **dedicated,
least-privilege identity** that can do only two things: read the specific secrets agami uses
(`AGAMI_SIGNING_SECRET`, `APP_DATABASE_URL`, `DATASOURCE_URL`, `AGAMI_ADMIN_PASSWORD`) and reach the
database. Nothing else. Never the platform default.

- **GCP Cloud Run** — the default Compute Engine service account has project **Editor**; don't use it.
  Create a dedicated SA, grant `roles/cloudsql.client` (if you use Cloud SQL) and
  `roles/secretmanager.secretAccessor` **per secret** (not project-wide), then deploy with `--service-account`:
  ```bash
  gcloud iam service-accounts create agami-run
  gcloud projects add-iam-policy-binding PROJECT \
    --member=serviceAccount:agami-run@PROJECT.iam.gserviceaccount.com \
    --role=roles/cloudsql.client
  # grant secret access PER-SECRET, not project-wide:
  for s in agami-signing-secret app-database-url datasource-url agami-admin-password; do
    gcloud secrets add-iam-policy-binding "$s" \
      --member=serviceAccount:agami-run@PROJECT.iam.gserviceaccount.com \
      --role=roles/secretmanager.secretAccessor
  done
  gcloud run deploy agami --service-account=agami-run@PROJECT.iam.gserviceaccount.com ...
  ```
- **AWS ECS / Fargate / App Runner** — give the task a dedicated **task role** with only
  `secretsmanager:GetSecretValue` on the specific secret ARNs (and `rds-db:connect` only if you use RDS
  IAM auth). Keep it separate from the **execution** role, and don't attach broad managed policies
  (`AdministratorAccess`, `PowerUserAccess`).
- **Azure Container Apps / ACI** — assign a **user-assigned managed identity** with a Key Vault access
  policy / RBAC role scoped to `get` on those specific secrets only — not a subscription- or vault-wide role.
- **VM / docker-compose (the default bundle)** — the platform doesn't assign the *container* its own
  identity here, and the container already runs as a non-root user (`uid 10001`). But on a cloud VM (GCE,
  EC2, an Azure VM) the VM's own attached identity is still reachable from the box via the instance metadata
  endpoint, so keep that identity scoped to only what the deployment needs (or block the container's route to
  metadata) — the same least-privilege rule, one level down.

Think of it as two separate safety nets that work together: the **read-only database user** limits what
a breach could do to your data, and a **least-privilege runtime identity** limits what it could do to
your cloud account. Keep both.
