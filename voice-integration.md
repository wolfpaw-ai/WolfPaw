# Voice Integration (Raspberry Pi)

Goal: a voice device (for example a Raspberry Pi with a headset) talks to Wolfpaw over a dedicated `voice` channel. The device does wake word, speech-to-text and text-to-speech locally; Wolfpaw exchanges plain text with it and can speak first (replies, questions, reminders, timers, alarms).

## What exists today

- `POST /chat` ([channels/web.py](src/wolfpaw/channels/web.py)) streams replies as SSE, but auth is cookie-only ([auth/deps.py](src/wolfpaw/auth/deps.py)), so a headless device has no way in.
- `Channel.send` (proactive push) is `NotImplemented` for web, so reminders, scheduled tasks and `ask_user` questions can't reach a pull-only client.
- [telegram.py](src/wolfpaw/channels/telegram.py) already has the pieces to copy: link-token onboarding, a `channel_links` row per linked user, proactive `send`, and `pending_questions` for `ask_user`.

## Design: a `voice` channel over WebSocket

The device keeps one outbound WebSocket open to `/channels/voice/ws`. Because the connection starts at the device, the home network needs no public ingress.

- `VoiceChannel(Channel)`: `receive` normalizes `utterance` and `answer` messages; `send` pushes to the user's live socket.
- **Fallback:** if no connected device can speak (no socket, or every device last reported `status {audio: "unavailable"}`), `send` falls back to Telegram straight away. Announcements are not queued for a device that is offline; the Telegram message is the delivery.
- **Reaching a socket from another process:** the socket lives in an API process, but a scheduled task runs in an arq worker. With workers off, delivery is in-process. With workers on, API processes publish which devices they hold to a Redis presence hash (stale after `voice_presence_ttl_seconds`) and listen on a Redis channel; senders pick a device from the presence data and publish the message for the process holding it.
- **`ask_user`:** the server sends `question {question_id}` and stores a pending question as Telegram does. The device returns `answer {question_id, text}`. The server never guesses that "the next utterance" is the answer, because utterances can overlap.
- **Pairing:** copy the Telegram link-token flow. The Profile page mints a short-lived code, the device presents it once, and the server issues a long-lived device token (stored hashed) and writes a `channel_links` row with `channel = "voice"`.
- **Auth:** the socket takes `Authorization: Bearer <device_token>`. The server accepts the connection first and then closes with code `4001` if the token is unknown or revoked, so a device can tell "pair again" apart from a network failure. Revoking a token closes that device's open socket (in whichever process holds it) with `4001`.
- **Pairing codes:** 8 characters from an alphabet with no look-alikes (no 0/O, 1/I/L), shown as `ABCD-2345`, single-use, valid for `voice_pair_code_ttl_minutes` (15), and stored hashed in `channel_link_tokens`. The pair endpoint allows 10 attempts per minute per client.
- **Profile page:** `POST /channels/voice/pair-code` (signed in) mints a code; `GET /channels/voice/devices` lists paired devices and whether each is connected; `DELETE /channels/voice/devices/{id}` revokes one.
- **Multiple devices:** a user can pair several devices (kitchen, living room, a headset). Each pairing is its own `channel_links` row and device token, with the `device_name` given at pairing, and each can hold its own socket. Replies and questions go back to the device that sent the utterance. Anything the server starts (`send`, announcements, schedules) goes to the device the user most recently sent a message from, falling back to any other connected device, then to Telegram. A schedule may name a specific device to override this. Revoking one device's token closes only that device's socket.

## Speech-shaped replies

When `ctx.channel == "voice"`, the agent gets a short hint: answer in 1–3 plain spoken sentences, with no markdown, tables or links, and offer to send detail to Telegram or the web instead.

## Announcements (timers, reminders, alarms)

Schedules already carry a `channel` ([schedule_task.py](src/wolfpaw/toolbox/tools/schedule_task.py)).

- `"voice"` is a valid schedule channel, and it is the default for a reminder asked for by voice. A scheduled run reaches the user with the `announce` tool, the voice counterpart of `send_telegram_message`; the run's instructions say so when the schedule's channel is voice.
- `announce(text, priority?, expires_in_seconds?)` speaks on a device, or falls back to Telegram and says so in its result.
- **Device-local timers:** for countdowns, the `set_timer(seconds, label)` tool sends `set_timer` to a device. The device runs the timer, survives network drops and reboots, speaks the label itself, and reports `timer_fired`. With no device connected the tool fails so the agent can offer a reminder instead.
- **Not built yet:** direct-text schedules (a "say this at time T" kind that skips the agent) and queueing announcements for a device that is offline. Both are optional follow-ups; today a scheduled voice reminder runs a short agent task that calls `announce`.

## Device protocol (version 1)

Every message is JSON with a `type`. Messages that need correlation carry an `id`. Unknown types are ignored by both sides, so either can add messages without breaking the other.

**Pairing (once)**
- The user reads a code off the Wolfpaw Profile page and gives it to the device.
- `POST /channels/voice/pair {code, device_name}` → `{device_token}`. The device stores the token on disk, readable only by its app.

**Connection**
- `wss://<host>/channels/voice/ws` with `Authorization: Bearer <device_token>`.
- Device sends `hello {protocol: 1, capabilities: ["speech"]}`; server replies `welcome {server_time}` (ISO 8601). The device uses `server_time` to detect a wrong clock.
- Device sends `ping` every 30 s; the server answers `pong` and refreshes the device's presence. If the device hears nothing for about 75 s it drops the connection and reconnects, with exponential backoff and jitter capped at about 60 s.
- Close code `4001` means the token is unknown or revoked: the device stops reconnecting and asks the user to pair again. `4002` means the protocol version isn't supported (the server first sends `{type: "error", code: "unsupported_protocol"}`).
- Nothing except `hello` is accepted before `hello`. Frames that aren't JSON objects are ignored.

**Device → server**
- `utterance {id, text}`: one finished speech-to-text result. The device generates `id`.
- `answer {question_id, text}`: the reply to a pending `question`.
- `ack {id}`: a `notify` was played.
- `alarm_ack {id, action: "stop" | "snooze", minutes?}`. The device stops a ringing alarm when the user says the wake word. To snooze, the user asks Wolfpaw ("remind me in ten minutes"); the device currently sends only `action: "stop"`.
- `timer_fired {id}`: a device-local timer finished.
- `status {audio: "ok" | "unavailable"}`: the headset or speaker connected or disconnected.
- `cancel {in_reply_to}` (optional): the user interrupted; the server may stop work on that utterance.

**Server → device**
- `progress {in_reply_to}`: optional "thinking" earcon. Not spoken.
- `reply {in_reply_to, text}`: speak it.
- `question {question_id, text, expires_at}`: speak it, then listen for the answer until `expires_at`. If no answer arrives, the pending question expires the same way an unanswered Telegram question does.
- `notify {id, text, priority: "info" | "alarm", expires_at}`: speak it (chime first for `info`), then `ack`. Repeat an `alarm` until `alarm_ack`. The server may redeliver a `notify` whose `ack` was lost, so the device dedupes by `id`. Anything past `expires_at` is dropped.
- `set_timer {id, seconds, label}`: run the timer on the device; when it fires, speak the label and send `timer_fired`.
- `cancel_timer {id}`: stop a device-local timer.

**Text**
- All `text` is plain speech. The device strips stray markdown defensively.

**Ordering and failures**
- Each utterance runs as its own turn, so replies can arrive out of order; the device speaks them as they arrive. A failed turn still produces a spoken `reply` ("Sorry, something went wrong on my end.").
- Only the first agent event of a turn produces a `progress` message.

## Order

Built: device table and tokens, pairing, the WebSocket channel with `reply`/`progress`/`question`/`answer`, device choice and Telegram fallback, the Redis bridge for workers, the voice schedule channel, `announce` and `set_timer`, and the spoken-style hint (see `tests/test_channels_voice.py` and `tests/test_tool_announce.py`; the database tests are in `tests/test_voice_devices_db.py` and need `WOLFPAW_TEST_DATABASE_URL`).

Not built: direct-text schedules, queueing for offline devices, a Profile-page UI for pairing codes (the endpoints exist), and `cancel_timer` from a tool (the message exists; no tool sends it yet).
