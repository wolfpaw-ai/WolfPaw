# channels/

User-facing surfaces — web, Telegram, Slack, eventually email. Every channel implements the same `Channel` interface and feeds normalized messages into a shared slash-command dispatcher before any agent sees them. The [root README](../../../README.md) shows where channels sit at the top-level.

## Connecting

Each channel has two layers: a one-time **operator setup** (creating the bot / app, putting credentials in `.env`) and a per-user **link step** (done from the web app's profile page). The web channel needs neither — it works as soon as you log in.

### Web

Nothing to configure. Log into the web app and start chatting. The Profile page is where you add the other channels.

### Telegram

The bot is shared across all users of a deployment (one `@WolfpawBot`-style handle per instance). Telegram requires a **public HTTPS webhook**, so the deployment must be reachable from the internet — see [`how-to-run.md`](../../../how-to-run.md) for the Tailscale Funnel or DDNS+Caddy options.

**Operator (once):**

1. Create the bot in Telegram: message [`@BotFather`](https://t.me/BotFather) → `/newbot` → pick a display name and a username. Copy the bot token it gives you.
2. Add to `.env`:
   ```
   WOLFPAW_TELEGRAM_BOT_TOKEN=<token from BotFather>
   WOLFPAW_TELEGRAM_BOT_USERNAME=<username without the @>
   WOLFPAW_TELEGRAM_WEBHOOK_SECRET=<any random string>
   ```
3. Restart so the new env is loaded:
   ```bash
   docker compose restart app worker
   ```
4. Register the webhook with Telegram (one-time per deployment):
   ```bash
   curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
     -d "url=https://<your-domain>/channels/telegram/webhook" \
     -d "secret_token=<WOLFPAW_TELEGRAM_WEBHOOK_SECRET>"
   ```
   Telegram will refuse non-HTTPS URLs. Verify with `curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"`.

**Each user:**

1. Log into the wolfpaw web app and open **Profile**.
2. Click **"Mint Telegram link"** — this calls `POST /channels/telegram/link-token` (handler in [`telegram.py`](telegram.py)) and returns a single-use `https://t.me/<bot>?start=link_<token>` URL.
3. Click **"Open in Telegram →"**. The bot receives `/start link_<token>`, [`telegram_tokens.consume`](telegram_tokens.py) maps it back to your wolfpaw `user_id`, and the row is written to `channel_links`. The bot replies "You're linked."

Token TTL is 15 minutes (`WOLFPAW_TELEGRAM_LINK_TOKEN_TTL_MINUTES`). After expiry, mint a new one.

### Slack

Unlike Telegram (one shared bot per deployment), Slack is one **app** per deployment but one **install** per workspace. The OAuth flow gives each workspace its own bot token, stored in `slack_workspaces`. Slack also requires public HTTPS.

**Operator (once per deployment):**

1. Create the Slack app from the manifest at [`docs/slack-app-manifest.yaml`](../../../docs/slack-app-manifest.yaml):
   - https://api.slack.com/apps → **Create New App** → **From an app manifest**.
   - Pick a workspace for development (you can distribute to others later from Settings → Manage Distribution).
   - Paste the YAML, replacing every `<YOUR-DOMAIN>` with your public HTTPS URL.
2. From the new app's **Basic Information → App Credentials** page, copy into `.env`:
   ```
   WOLFPAW_SLACK_CLIENT_ID=<Client ID>
   WOLFPAW_SLACK_CLIENT_SECRET=<Client Secret>
   WOLFPAW_SLACK_SIGNING_SECRET=<Signing Secret>
   ```
3. Restart:
   ```bash
   docker compose restart app worker
   ```

The manifest already points Slack's event-subscriptions URL at `/channels/slack/events` and the OAuth redirect at `/channels/slack/oauth/callback`, so there's nothing to register separately.

**Each user (per workspace):**

1. Log into the wolfpaw web app and open **Profile**.
2. Click **"Connect Slack"** — this calls `GET /channels/slack/install-url`, which mints a state token (same shape as the Telegram link token) and returns Slack's OAuth authorize URL.
3. Approve the install in Slack. Slack redirects to `/channels/slack/oauth/callback`, [`slack.py`](slack.py) exchanges the code for a bot token, upserts the workspace into `slack_workspaces`, and writes a `channel_links` row keyed on `team:slack_user`.
4. DM the bot in Slack, or use `/wolfpaw <message>` in any channel where the bot is installed.

### Email

Not implemented yet — see "Extending" below.


## Files

- **`__init__.py`** — `Channel` ABC + `InboundMessage` dataclass. Every channel implements `receive(payload) → InboundMessage`, `send(user_id, content)`, `supports_streaming()`. The ABC is the contract; concrete channels live as siblings.
- **`commands.py`** — `CommandDispatcher` + `@register("name", "description")` decorator. Slash messages are intercepted here so they never burn model tokens; non-slash messages return `None` and fall through to the Router in [`agents/`](../agents/README.md). `CommandResult` carries an optional `clear_thread` flag — channels with client-side thread state honor it to drop the current thread id. `/help` and `/reset` are built-ins; other commands self-register from their owning module (e.g. `metering/usage_report.py` registers `/usage`, `tasks/commands.py` registers `/tasks` / `/task <id>` / `/cancel <id>`).
- **`web.py`** — `WebChannel` + `POST /channels/web/chat` SSE endpoint + `POST /channels/web/answer` (resolves `ask_user` pending questions). Chat dispatches through `commands` first; non-command messages flow to the Router. SSE events: `thread`, `reset`, `command`, `triage`, `pre_eval`, `plan`, `tool`, `task`, `ask_user`, `step.start` / `step.end` / `step.error`, `step.recover`, `plan.replan`, `score`, `skill_emitted`, `delta`, `done`, `error`.
- **`telegram.py`** — `TelegramChannel` + two routes. `POST /channels/telegram/webhook` validates `X-Telegram-Bot-Api-Secret-Token`, handles `/start link_<token>` onboarding inline, dispatches slash commands inline, and routes free-form messages via [`workers/queue.enqueue_telegram_dispatch`](../workers/README.md). `POST /channels/telegram/link-token` (authed) mints a single-use deep-link URL `https://t.me/<bot>?start=link_<token>`.
- **`telegram_client.py`** — `HttpTelegramClient` (httpx wrapper over `sendMessage`) + `FakeTelegramClient` for tests. `set_telegram_client(...)` is the test-injection hook.
- **`telegram_markdown.py`** — `to_markdown_v2(text)` converts the agent's CommonMark-ish output into Telegram MarkdownV2 with proper escaping.
- **`telegram_tokens.py`** — `issue` / `consume` for `channel_link_tokens` (URL-safe plaintext + SHA-256 hash, single-use). Reused by Slack — the OAuth `state` parameter is one of these tokens, bound to the wolfpaw user who clicked "Connect Slack".
- **`slack.py`** — `SlackChannel` + four routes: `GET /channels/slack/install-url` (mints state + returns OAuth URL), `GET /channels/slack/oauth/callback` (exchanges code + persists workspace), `POST /channels/slack/events` (URL-verification + DM dispatch via the worker queue), `POST /channels/slack/commands` (`/wolfpaw <text>`). HMAC signature on every inbound POST via `slack_signing.py`.
- **`slack_signing.py`** — `verify(...)` against `X-Slack-Signature` + ±5min replay window. Raises `BadSignature` on any failure.
- **`slack_client.py`** — `HttpSlackClient` (httpx wrapper over `oauth.v2.access` + `chat.postMessage`) + `FakeSlackClient` + injection hook.

## Flow

```mermaid
flowchart TD
    Provider[("web client / Telegram / Slack")]
    Provider -->|"HTTP POST"| Channel
    Channel["channels/&lt;provider&gt;.py<br/>parse payload → InboundMessage"]
    Channel --> Auth{"authenticated?<br/>auth/ for web,<br/>HMAC for Slack,<br/>secret token for Telegram"}
    Auth -->|"no"| Reject["401 / 403"]
    Auth -->|"yes"| Dispatch["CommandDispatcher.dispatch"]
    Dispatch -->|"slash match"| Slash["CommandResult<br/>rendered back to user<br/>never reaches model"]
    Dispatch -->|"None"| RouteChoice{"push channel?"}
    RouteChoice -->|"Telegram / Slack"| Enqueue["workers/queue.enqueue_*_dispatch<br/>(or asyncio.create_task in dev)"]
    RouteChoice -->|"Web SSE"| InlineRouter["Router.handle in-process<br/>(SSE stream tied to HTTP)"]
    Enqueue --> Worker["arq worker"]
    Worker --> Router["agents/ Router.handle"]
    InlineRouter --> Router
    Router --> Send["channel.send<br/>or SSE delta"]
    Send --> Provider

    click Channel "."
    click Dispatch "commands.py"
    click Auth "../auth/README.md"
    click Enqueue "../workers/README.md"
    click Router "../agents/README.md"
```

**Why web stays in-process while Telegram + Slack queue:** the web SSE stream is tied to the HTTP connection — routing the agent to arq would require a Redis pub-sub bridge back to the open response. Telegram + Slack don't have that constraint because the reply is pushed asynchronously via `sendMessage` / `chat.postMessage`. So push channels were the easy durability win.

## Telegram onboarding (deep link)

```mermaid
sequenceDiagram
    participant W as Web client (authed)
    participant API as Wolfpaw API
    participant TG as Telegram
    participant U as User

    W->>API: POST /channels/telegram/link-token
    API->>API: telegram_tokens.issue (single-use, TTL)
    API-->>W: { url: t.me/<bot>?start=link_<token> }
    W->>U: shows tap-to-link button
    U->>TG: taps URL → opens bot → /start link_<token>
    TG->>API: POST /channels/telegram/webhook
    API->>API: telegram_tokens.consume → wolfpaw user_id
    API->>API: channel_links.create(user_id, telegram, tg_user_id)
    API->>TG: sendMessage "You're linked."
```

## Slack install (OAuth)

```mermaid
sequenceDiagram
    participant W as Web client (authed)
    participant API as Wolfpaw API
    participant SL as Slack
    participant U as User

    W->>API: GET /channels/slack/install-url
    API->>API: mint state token (same shape as telegram link token)
    API-->>W: { url: slack.com/oauth/v2/authorize?...&state=<token> }
    U->>SL: clicks URL → approval screen → Allow
    SL->>API: GET /channels/slack/oauth/callback?code=...&state=<token>
    API->>API: consume state → wolfpaw user_id
    API->>SL: POST oauth.v2.access (code → bot_token + team + slack_user_id)
    API->>API: slack_workspaces.upsert + channel_links.create<br/>(external_id = team:slack_user)
    API-->>U: HTML "Wolfpaw is installed in <workspace>"
```

The Slack app itself is created by the operator from [`docs/slack-app-manifest.yaml`](../../../docs/slack-app-manifest.yaml) — one app per deployment (production / staging / dev). Each workspace install gets its own bot token in `slack_workspaces`.

## Extending

- **New channel** (email next): subclass `Channel`, parse the payload into `InboundMessage`, mount a webhook/route, run inbound through `dispatcher.dispatch` for slash commands and either `Router.handle` (synchronous) or `workers/queue.enqueue_*_dispatch` (push channels) for free-form messages.
- **New slash command** — `@register("name", "description")` on an `async (msg, args) -> CommandResult` handler in whatever module owns the data. Make sure the module gets imported at app boot so the side-effect registration runs (see `api.py`).
- **Slack: channel mentions + threads** (#35 in [`v2_implementation_plan.md`](../../../v2_implementation_plan.md)). v1 routes DMs only. `app_mention` events are already in scope at install time; wiring them means handling `event.type == "app_mention"` in `_handle_message_event` and mapping Slack `thread_ts` onto wolfpaw's `thread_id`.
- **Telegram file uploads + inline keyboards** (#36) — pull from `Document` / `PhotoSize` payloads and register them via [`workspace/`](../workspace/README.md). Inline keyboards make `ask_user` choices a tap instead of a typed reply.
