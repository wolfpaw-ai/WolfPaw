# channels/

User-facing surfaces (web, Telegram, eventually email / Slack) all implement the same `Channel` interface and feed normalized messages into a shared slash-command dispatcher before any agent sees them.

## Files

- **`__init__.py`** — `Channel` ABC + `InboundMessage` dataclass. Every channel implements `receive(payload) → InboundMessage`, `send(user_id, content)`, `supports_streaming()`. The ABC is the contract; concrete channels live as siblings.
- **`commands.py`** — `CommandDispatcher` + `@register("name", "description")` decorator. Slash messages (`/help`, `/usage`, `/reset`, …) are intercepted here so they never burn model tokens; non-slash messages return `None` and fall through to the agent pipeline. `CommandResult` carries an optional `clear_thread` flag — channels with client-side thread state (web SSE) honor it to drop the current thread_id. `/help` and `/reset` are built-ins; other commands register themselves from their owning module (e.g. `metering/usage_report.py` registers `/usage`, `tasks/commands.py` registers the task commands).
- **`web.py`** — `WebChannel` + `POST /channels/web/chat` SSE endpoint + `POST /channels/web/answer` (resolves `ask_user` pending questions). Chat dispatches through `commands` first; non-command messages flow to the Router which composes Triage → (Quick | Plan | Task). SSE events: `thread`, `reset`, `command`, `triage`, `plan`, `tool`, `task`, `ask_user`, `step.start` / `step.end` / `step.error`, `score`, `delta`, `done`, `error`. `reset` is emitted when a slash command (currently `/reset`) sets `clear_thread=True` so the client forgets its thread id.
- **`telegram.py`** — `TelegramChannel` + two routes. `POST /channels/telegram/webhook` (validated against `X-Telegram-Bot-Api-Secret-Token`) parses Telegram updates, handles `/start link_<token>` onboarding inline, dispatches slash commands inline, and fires the Router as a background `asyncio.create_task` for free-form messages — webhook returns 200 fast and the response is pushed back via `sendMessage` formatted as MarkdownV2 (so the agent's `**bold**` and code spans render). `POST /channels/telegram/link-token` (authenticated Wolfpaw user) mints a single-use deep-link URL: `https://t.me/<bot_username>?start=link_<token>`. `/reset` from Telegram creates a fresh thread server-side (Telegram has no client thread state).
- **`telegram_client.py`** — `HttpTelegramClient` (httpx wrapper over the Bot API's `sendMessage`, optional `parse_mode`) + `FakeTelegramClient` for tests. `get_telegram_client()` is the singleton; `set_telegram_client(fake)` is the test-injection hook.
- **`telegram_markdown.py`** — `to_markdown_v2(text)` converts the agent's CommonMark-ish output (`**bold**`, ` `code` `, ` ```block``` `, `[text](url)`, `_italic_`) into Telegram MarkdownV2 with proper escaping for every reserved character. Unknown / unmatched markup falls back to character-level escaping so the Bot API never rejects the message.
- **`telegram_tokens.py`** — `issue(...)` mints a token (URL-safe plaintext + SHA-256 hash) into `channel_link_tokens`; `consume(...)` validates + marks used atomically in a transaction and returns the bound user_id. Same shape as `magic_link_tokens`. **Reused by Slack** — the OAuth `state` parameter is one of these tokens, bound to the Wolfpaw user who clicked "Connect Slack".
- **`slack.py`** — `SlackChannel` + four routes. `GET /channels/slack/install-url` (authed) mints a state token + returns the Slack OAuth URL. `GET /channels/slack/oauth/callback` consumes the state token, exchanges the code via `oauth.v2.access`, persists the workspace + a `channel_links` row (external_id = `<team_id>:<slack_user_id>` so the same person in two workspaces is two distinct identities). `POST /channels/slack/events` handles the Events API: URL-verification handshake + DM dispatch (filters out bot-authored messages + non-IM channels). `POST /channels/slack/commands` handles the `/wolfpaw` slash command — dispatches `/help`-style sub-commands inline, fires the Router for free-form text with an "Working on it…" ack (Slack has a 3s response window) and pushes the real reply via `chat.postMessage`. All inbound POSTs verify the HMAC signature before parsing.
- **`slack_signing.py`** — `verify(...)` checks the `X-Slack-Signature` HMAC against the raw request body keyed by the signing secret, plus rejects timestamps outside ±5min. Raises `BadSignature` on any failure (don't leak which check failed). Must hash the raw bytes, not re-serialized JSON.
- **`slack_client.py`** — `HttpSlackClient` (httpx wrapper over `oauth.v2.access` + `chat.postMessage`) + `FakeSlackClient` for tests + `set_slack_client(...)` injection hook. `SlackApiError` wraps non-ok responses so callers don't have to check `["ok"]` everywhere.

## How it fits together

Inbound flow: channel adapter parses its provider's payload → `InboundMessage` → `dispatcher.dispatch` → either a `CommandResult` (rendered back to the user) or `None` (continue to the Router). Outbound flow uses `send()` for proactive pushes (Telegram is push-native; web push waits for websockets).

Telegram onboarding flow (deep link):
```
web client     →  POST /channels/telegram/link-token              (authed)
                ←  {url: "https://t.me/<bot>?start=link_<token>"}
user           →  taps URL → opens Telegram → taps "Start"
Telegram       →  POST /channels/telegram/webhook  body=`/start link_<token>`
us             →  consume token → channel_links.create(user_id, telegram, tg_user_id)
us             →  sendMessage "You're linked." back to the chat
```

Slack install flow (OAuth):
```
web client     →  GET  /channels/slack/install-url                (authed)
                ←  {url: "https://slack.com/oauth/v2/authorize?...&state=<token>"}
user           →  clicks URL → Slack approval screen → "Allow"
Slack          →  GET  /channels/slack/oauth/callback?code=...&state=<token>
us             →  consume state token → wolfpaw user_id
us             →  POST slack.com/api/oauth.v2.access  with code → bot_token + team + slack_user_id
us             →  slack_workspaces.upsert(team_id, bot_token, ...)
us             →  channel_links.create(user_id, slack, external_id=`<team_id>:<slack_user_id>`)
us             →  render "Wolfpaw is now installed in <workspace>." HTML page
```

The Slack app itself is created by the operator from [`docs/slack-app-manifest.yaml`](../../../docs/slack-app-manifest.yaml) — one app per deployment (production / staging / dev), each with its own client id + signing secret. Each workspace's install gets its own bot token, stored in `slack_workspaces`.

## Extending

- **New channel** (email next): subclass `Channel`, parse the provider payload into `InboundMessage`, mount a webhook/route, run inbound through `dispatcher.dispatch` for slash commands and `Router.handle` for free-form messages. For push channels (Telegram, Slack) keep the webhook fast and offload the agent work to a background task or queue.
- **New slash command:** `@register("name", "description")` on an `async (msg, args) -> CommandResult` handler in whatever module owns the data. Make sure the module gets imported at app boot so the side-effect registration runs (see `api.py`).
- **arq async processing** is wired (step 23). The Telegram + Slack inbound dispatch goes through `wolfpaw.workers.queue.enqueue_telegram_dispatch` / `enqueue_slack_dispatch`, which route to the arq worker when `WOLFPAW_WORKERS_ENABLED=true` and fall back to `asyncio.create_task` otherwise. Web SSE intentionally stays in-process because the event stream is bound to the HTTP connection.
- **Slack: channel mentions + threads.** v1 routes DMs only. `app_mention` events are already in scope at install time; wiring them up means handling `event.type == "app_mention"` in `_handle_message_event` and choosing a reply channel (the originating channel rather than the user's IM). Slack threads (`thread_ts`) would map cleanly onto Wolfpaw's `thread_id` for in-channel continuity.
