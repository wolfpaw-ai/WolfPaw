# channels/

User-facing surfaces (web, Telegram, eventually email / Slack) all implement the same `Channel` interface and feed normalized messages into a shared slash-command dispatcher before any agent sees them.

## Files

- **`__init__.py`** — `Channel` ABC + `InboundMessage` dataclass. Every channel implements `receive(payload) → InboundMessage`, `send(user_id, content)`, `supports_streaming()`. The ABC is the contract; concrete channels live as siblings.
- **`commands.py`** — `CommandDispatcher` + `@register("name", "description")` decorator. Slash messages (`/help`, `/usage`, …) are intercepted here so they never burn model tokens; non-slash messages return `None` and fall through to the agent pipeline. `/help` is the only built-in registered here; other commands register themselves from their owning module (e.g. `metering/usage_report.py` registers `/usage`, `tasks/commands.py` registers the task commands).
- **`web.py`** — `WebChannel` + `POST /channels/web/chat` SSE endpoint + `POST /channels/web/answer` (resolves `ask_user` pending questions). Chat dispatches through `commands` first; non-command messages flow to the Router which composes Triage → (Quick | Plan | Task). SSE events: `thread`, `command`, `triage`, `plan`, `tool`, `task`, `ask_user`, `step.start` / `step.end` / `step.error`, `score`, `delta`, `done`, `error`.
- **`telegram.py`** — `TelegramChannel` + two routes. `POST /channels/telegram/webhook` (validated against `X-Telegram-Bot-Api-Secret-Token`) parses Telegram updates, handles `/start link_<token>` onboarding inline, dispatches slash commands inline, and fires the Router as a background `asyncio.create_task` for free-form messages — webhook returns 200 fast and the response is pushed back via `sendMessage`. `POST /channels/telegram/link-token` (authenticated Wolfpaw user) mints a single-use deep-link URL: `https://t.me/<bot_username>?start=link_<token>`.
- **`telegram_client.py`** — `HttpTelegramClient` (httpx wrapper over the Bot API's `sendMessage`) + `FakeTelegramClient` for tests. `get_telegram_client()` is the singleton; `set_telegram_client(fake)` is the test-injection hook.
- **`telegram_tokens.py`** — `issue(...)` mints a token (URL-safe plaintext + SHA-256 hash) into `channel_link_tokens`; `consume(...)` validates + marks used atomically in a transaction and returns the bound user_id. Same shape as `magic_link_tokens`.

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

## Extending

- **New channel** (email, Slack): subclass `Channel`, parse the provider payload into `InboundMessage`, mount a webhook/route, run inbound through `dispatcher.dispatch` for slash commands and `Router.handle` for free-form messages. For push channels (Telegram, Slack) keep the webhook fast and offload the agent work to a background task or queue.
- **New slash command:** `@register("name", "description")` on an `async (msg, args) -> CommandResult` handler in whatever module owns the data. Make sure the module gets imported at app boot so the side-effect registration runs (see `api.py`).
- **arq async processing** for the webhook background tasks lands together with the arq worker deferred from step 15.
