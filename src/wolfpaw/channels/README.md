# channels/

User-facing surfaces (web, eventually Telegram / email / Slack) all implement the same `Channel` interface and feed normalized messages into a shared slash-command dispatcher before any agent sees them.

## Files

- **`__init__.py`** — `Channel` ABC + `InboundMessage` dataclass. Every channel implements `receive(payload) → InboundMessage`, `send(user_id, content)`, `supports_streaming()`. The ABC is the contract; concrete channels live as siblings.
- **`commands.py`** — `CommandDispatcher` + `@register("name", "description")` decorator. Slash messages (`/help`, `/usage`, …) are intercepted here so they never burn model tokens; non-slash messages return `None` and fall through to the agent pipeline. `/help` is the only built-in registered here; other commands register themselves from their owning module (e.g. `metering/usage_report.py` registers `/usage`).
- **`web.py`** — `WebChannel` + `POST /channels/web/chat` SSE endpoint. The endpoint dispatches through `commands` first; if no command matches, it streams a placeholder `delta` (the Quick Agent lands in step 10). SSE events are framed as `event: command | delta | done | error`.

## How it fits together

Inbound flow: channel adapter parses its provider's payload → `InboundMessage` → `dispatcher.dispatch` → either a `CommandResult` (rendered back to the user) or `None` (continue to the agent pipeline). Outbound flow uses `send()` for proactive pushes (currently only Telegram + email; web push waits for websockets).

## Extending

- **New channel:** subclass `Channel`, parse the provider payload into `InboundMessage`, mount a webhook/route, run inbound through `dispatcher.dispatch`, render outbound for the medium. Telegram and email follow this pattern (steps 18, 23).
- **New slash command:** `@register("name", "description")` on an `async (msg, args) -> CommandResult` handler in whatever module owns the data. Make sure the module gets imported at app boot so the side-effect registration runs (see `api.py`).
