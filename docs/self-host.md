# Self-hosting Wolfpaw

This is the OSS-deployment doc. The `docker-compose.yml` + `Dockerfile` +
`install.sh` at the repo root are the supported quickstart path; this
document explains what they do, what env vars matter, and what to change
when you put the app behind a real domain.

## Quickstart

```bash
git clone <this repo> wolfpaw
cd wolfpaw
./install.sh
```

That:
1. Copies `.env.example` → `.env`.
2. Generates a random `WOLFPAW_SECRET_KEY` (session signing).
3. Asks you to fill in `WOLFPAW_ANTHROPIC_API_KEY` in `.env`.
4. On re-run, brings up the stack: `docker compose up -d --build`.

The browser visits `http://localhost:3000`. Sign in with any email — the
verify URL prints to the app container's stdout when `WOLFPAW_EMAIL_BACKEND=console`
(the default). Tail it with:

```bash
docker compose logs -f app
```

## What's in the stack

| Service   | Image                            | Purpose                                         |
|-----------|----------------------------------|-------------------------------------------------|
| `db`      | `pgvector/pgvector:pg16`         | Postgres + pgvector. Data on the `wolfpaw_db` volume. |
| `redis`   | `redis:7-alpine`                 | Backs the arq queue. Data on the `wolfpaw_redis` volume (appendonly). |
| `migrate` | `wolfpaw-app:local` (one-shot)   | Applies `migrations/*.sql` idempotently. Records what's been applied in `schema_migrations`. |
| `app`     | `wolfpaw-app:local`              | FastAPI backend on :8000. Workspace files on the `wolfpaw_workspace` volume. |
| `worker`  | `wolfpaw-app:local`              | arq worker. Runs background jobs (thread compaction, message embedding, queued Tasks, Telegram + Slack dispatch). Same image as `app`. |
| `web`     | `wolfpaw-web:local` (nginx)      | Serves the React build + proxies API paths to `app`. Published on host :3000. |

The workers queue gates on `WOLFPAW_WORKERS_ENABLED` (compose sets it
to `true`). With it off, the app falls back to in-process
`asyncio.create_task` for the same jobs — fine for local debugging
without Redis. Web SSE always runs in-process either way; the worker
is for the work that *isn't* tied to a streaming HTTP response.

## Required env vars

Set in `.env`:

- `WOLFPAW_ANTHROPIC_API_KEY` — without this, the agent loop can't make
  model calls. Everything else degrades gracefully without their keys.
- `WOLFPAW_SECRET_KEY` — signs session cookies + magic-link tokens.
  `install.sh` generates a random one on first run. Rotate by setting a
  new value (invalidates all existing sessions).

## Optional env vars

- `WOLFPAW_VOYAGE_API_KEY` — real embeddings. Without it, set
  `WOLFPAW_EMBEDDING_BACKEND=stub` for a deterministic SHA-based fallback
  that's fine for testing but useless for retrieval quality.
- `WOLFPAW_TAVILY_API_KEY` — the `web_search` tool.
- `WOLFPAW_TELEGRAM_BOT_TOKEN` + `WOLFPAW_TELEGRAM_BOT_USERNAME` +
  `WOLFPAW_TELEGRAM_WEBHOOK_SECRET` — to enable the Telegram channel.
  Requires HTTPS at your webhook URL; put a TLS-terminating reverse proxy
  in front of nginx (see "Behind a real domain" below).
- `WOLFPAW_TRACE_SINK_ENABLED` (default `true`) — per-call request/response
  logging to the `model_call_logs` table. This is the only record of *failed*
  model calls, so leave it on unless you have a specific reason not to.
  `WOLFPAW_TRACE_RETENTION_DAYS` (default `14`) sets how long payloads are
  kept — enforced by dropping monthly partitions, so the real cutoff rounds up
  to a month boundary. `WOLFPAW_TRACE_PAYLOAD_MAX_BYTES` (default `64000`)
  caps per-call payload size. Nothing leaves your deployment. Note that
  retention is enforced by a daily job in the arq worker — if you run the API
  without the worker, traces accumulate indefinitely.
- `WOLFPAW_E2B_API_KEY` (plus `WOLFPAW_SANDBOX_BACKEND=e2b`) — managed
  sandbox provider. The default `subprocess` backend is fine for
  single-user self-host but is **not a security boundary**.

## Behind a real domain

The compose stack listens on `localhost:3000` by default. To put it
behind a real domain with TLS:

1. Run a reverse proxy (Caddy, nginx, Traefik) on the host that terminates
   TLS and forwards to `localhost:3000`. Caddy in particular handles
   Let's Encrypt automatically.
2. Set `WOLFPAW_WEB_BASE_URL=https://your-domain.com` in `.env` — magic-link
   verify URLs are constructed from this.
3. For Telegram, point the webhook at
   `https://your-domain.com/channels/telegram/webhook` and set
   `WOLFPAW_TELEGRAM_WEBHOOK_SECRET` to a random string. The webhook handler
   validates the `X-Telegram-Bot-Api-Secret-Token` header against it.

Sample Caddyfile:

```caddy
your-domain.com {
    reverse_proxy localhost:3000
}
```

### Camera-driven tools (HTTPS required)

`scan_barcode` and the live-preview path of `capture_photo` (see
[`photo-tools.md`](../photo-tools.md)) call
`navigator.mediaDevices.getUserMedia`, which browsers only expose on
HTTPS origins (`localhost` excepted). Reach them from another device on
your network and they will fail silently on plain `http://`. Options:

- **Tailscale MagicDNS** (easiest). Run Tailscale on the host and on the
  devices you want to scan from; reach the app at
  `https://<host>.<your-tailnet>.ts.net` with a real cert and no router
  config. Works inside and outside your home network.
- **mkcert + your own CA.** Generate a local CA once with
  [`mkcert`](https://github.com/FiloSottile/mkcert), install it on each
  household device, mint a cert for `wolfpaw.local` (or whatever
  hostname Avahi/Bonjour gives the host), terminate TLS at Caddy/nginx
  with that cert. New guest devices need the CA installed before they
  can scan.
- **Self-signed cert with browser warning.** Cheapest, but every device
  has to click through a security warning the first time.

The text-based `ask_user` and the file-picker path of `capture_photo`
(plain `<input type="file" capture="environment">`) do not need
`getUserMedia` and work on plain HTTP — only the live-camera widgets
require this.

## Operations

### Logs

```bash
docker compose logs -f app          # backend
docker compose logs -f web          # nginx access + error logs
docker compose logs -f db           # postgres
```

The backend emits structured JSON to stdout — one event per line, with
`trace_id` threaded through. Pipe into your log shipper of choice (Loki,
journald, CloudWatch agent, Datadog, etc.).

### Migrations

```bash
docker compose run --rm migrate
```

Idempotent — already-applied files are skipped. On a failure, the failing
migration isn't marked applied, so a fix + retry runs only it.

### Backup

The two stateful volumes are `wolfpaw_db` (Postgres) and
`wolfpaw_workspace` (user files).

```bash
# postgres dump
docker compose exec -T db pg_dump -U wolfpaw wolfpaw | gzip > backup-$(date +%F).sql.gz

# workspace
docker run --rm -v wolfpaw_workspace:/data -v "$PWD":/backup alpine \
    tar -C /data -czf /backup/workspace-$(date +%F).tar.gz .
```

### Updating

```bash
git pull
docker compose build
docker compose up -d
```

The `migrate` service runs again on `up` — applies any new migration
files, no-ops on the already-applied ones.

### Resetting everything

```bash
docker compose down -v        # drops the volumes (DESTROYS DATA)
./install.sh                  # fresh start
```

## What's NOT in the OSS image

These are intentionally out of scope. If you need them, add them in your
own fork or layered image:

- **Billing / Stripe** — the `subscriptions` + `tier_limits` schema is
  present so you can wire it in, but the OSS `Enforcer` is a no-op
  (everyone runs as `tier="dev"`).
- **Multi-tenant hardening** — rate limits per user, sandbox isolation
  beyond Subprocess, S3 prefix scoping, PII redaction in logs.
- **Hosted observability** — CloudWatch / Datadog / etc. The OSS app
  emits structured JSON to stdout; pick a shipper that fits your
  environment.
- **Email forwarding inbound** (step 23, deferred) — `POST /channels/email/inbound`
  is the endpoint, but the OSS image doesn't ship an SES Lambda /
  mail-provider integration. Self-host operators wire their preferred
  inbound mail path to that endpoint.

## Troubleshooting

- **`docker compose up` says the app exits with database connection
  errors.** Wait — the first `db` boot takes ~10s to initialize. The
  healthcheck + `depends_on: service_healthy` should make the app wait;
  if it's still racing, increase the `db` healthcheck retries.
- **The web UI loads but API calls 404.** Check `docker compose logs web`
  — nginx may not be proxying to `app:8000` (DNS resolution inside the
  docker network failed, or the `app` service isn't healthy yet).
- **Magic-link sign-in: I never see the verify URL.** The console email
  backend prints to `app`'s stdout: `docker compose logs -f app | grep verify`.
  Set `WOLFPAW_EMAIL_BACKEND=` to your real provider when you're ready.
- **`run_python` tool errors with "Permission denied".** The Subprocess
  sandbox writes to `/tmp` inside the app container. If you've mounted
  a read-only filesystem, it'll fail; either un-mount read-only or switch
  to `WOLFPAW_SANDBOX_BACKEND=e2b`.

## Local development (without Docker)

If you'd rather run the app directly against a local Postgres for fast
iteration, the dev workflow is documented in the top-level [README.md](../README.md#trying-it-locally-today)
under "Trying it locally today". The Docker path is for self-hosting; the
direct path is for contributing.
