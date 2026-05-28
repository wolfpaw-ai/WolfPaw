# Photo Tools: Barcode Reader + Photo Capture

Two specialized tools that let the agent invoke a camera-driven widget on the user's device and receive a structured result. The agent never authors the page — the widgets are first-party React components shipped with the web app. The agent only triggers them and consumes the output.

Both tools ride on the existing `ask_user_registry` plumbing in [src/wolfpaw/tasks/ask_user_registry.py](../../wolfpaw/src/wolfpaw/tasks/ask_user_registry.py): register a pending question, pause the task, push a prompt to the user's channel, await the future, resume.

---

## Shared groundwork (do once, both tools depend on it)

1. **Typed questions in `ask_user_registry`.** Add a `question_type` field (`"text" | "barcode_scan" | "photo_capture"`). Default `"text"` so today's `ask_user` is unchanged. Persist on the `user_question` task_event so the web client can pick the right widget when re-hydrating a paused task.
2. **Web client dispatcher.** In the chat UI, where a pending `ask_user` currently renders a text input, switch on `question_type` and mount the matching component. Text falls back to the existing input.
3. **Per-question token URL for non-web channels.** For Telegram/Slack: push a `https://wolfpaw.ai/forms/<token>` link instead of an inline widget. Token binds (user_id, task_id, question_id), single-use, expires on submit or timeout. The `/forms/<token>` page mounts the same React widgets standalone — the camera-bearing surfaces don't care whether they're in the chat app or a standalone form page.
4. **Tool registration.** Both tools live next to [ask_user.py](../../wolfpaw/src/wolfpaw/toolbox/tools/ask_user.py) and follow the same `register_tool` pattern, including the `ctx.task_id` requirement.

---

## Tool 1: `scan_barcode`

### Behavior
Pauses the task, asks the user to scan a barcode with their device camera, returns the decoded value.

### Backend
- New tool `scan_barcode` in [src/wolfpaw/toolbox/tools/scan_barcode.py](../../wolfpaw/src/wolfpaw/toolbox/tools/scan_barcode.py).
- Inputs: `prompt` (str, what to scan and why), `formats` (optional list — `["EAN_13", "UPC_A", "QR_CODE", ...]`, default = common product codes), `timeout_seconds` (default 180).
- Registers a `barcode_scan` question, awaits the future, returns `{ code: str, format: str, scanned_at: iso8601 }`.

### Frontend
- New component `web/src/widgets/BarcodeScanner.tsx`.
- `@zxing/browser` for decoding (lazy-loaded — keep it out of the main chat bundle).
- Live `<video>` preview from `navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } })`.
- Calls `onResult(code, format)` on first successful decode; component then dims the preview and shows "Scanned: <code>. Submit / rescan."
- Submit POSTs to the existing answer endpoint with `{ question_id, answer: { code, format } }`.

### Edge cases
- iOS Safari: `getUserMedia` requires a user gesture — the widget mounts an explicit "Start camera" button rather than auto-starting.
- Permission denied: surface "Camera blocked. Open a settings link or type the code manually" — fall back to a text input that submits the same shape.
- Format restriction: pass `formats` through to ZXing's hints so the decoder doesn't waste cycles on irrelevant symbologies.

---

## Tool 2: `capture_photo`

### Behavior
Pauses the task, asks the user to take or upload a photo, stores it in their workspace, returns a workspace file reference the agent can pass to other tools (OCR, vision model, receipt parsing, etc.).

### Backend
- New tool `capture_photo` in [src/wolfpaw/toolbox/tools/capture_photo.py](../../wolfpaw/src/wolfpaw/toolbox/tools/capture_photo.py).
- Inputs: `prompt` (str, what to photograph and why), `max_bytes` (default 8 MB), `accept` (`"image/*"` default, can restrict to `"image/jpeg"`), `timeout_seconds` (default 300).
- Registers a `photo_capture` question. When the user submits, the upload endpoint writes through [storage/](../../wolfpaw/src/wolfpaw/storage/) and creates a `workspace_files` row (reusing the existing workspace upload path), then resolves the future with `{ workspace_file_id, mime_type, width, height, bytes }`.

### Frontend
- New component `web/src/widgets/PhotoCapture.tsx`.
- Two paths in the same widget:
  - **Quick path**: `<input type="file" accept="image/*" capture="environment">` — on mobile this opens the system camera; no live preview, no `getUserMedia`, no permission prompt. Works on every browser. Default.
  - **Live path** (opt-in via a "Use live preview" toggle, or `prompt`-driven hint): same `getUserMedia` stack as the barcode widget but with a shutter button instead of a decoder loop. Useful when the user wants to frame carefully or retake without leaving the page.
- Preview the captured image, "Retake / Submit" buttons, then POSTs multipart to the answer endpoint.

### Edge cases
- Large photos: client-side downscale to a configurable max edge (e.g. 2048 px) before upload — saves bandwidth and tokens downstream if the file gets sent to a vision model.
- HEIC from iOS: convert to JPEG client-side via canvas before upload so server-side consumers don't have to deal with HEIC.
- Workspace quota: reuse the existing workspace upload guardrails; surface a clean error if the user is over.

---

## Order of work

1. Schema: add `question_type` to `ask_user_registry` + `user_question` event payload. Backfill default `"text"`.
2. Web: dispatcher switch in the pending-question renderer. Mount text input today; placeholder branches for the two new types.
3. `/forms/<token>` standalone page + token-mint endpoint, reusing magic-link token patterns from [src/wolfpaw/auth/](../../wolfpaw/src/wolfpaw/auth/).
4. `capture_photo` end-to-end first (simpler — `<input capture>` Quick path only, no `getUserMedia`). Validates the whole pipeline.
5. `scan_barcode` end-to-end, including ZXing lazy-load.
6. Live-preview path for `capture_photo` as a follow-up — only needed if the Quick path proves insufficient.

## Out of scope (deliberately)

- Telegram inline keyboards / native barcode in Telegram. Per-question URL is the bridge.
- Agent-authored custom widgets. These two are first-party, fixed-shape, ship-with-the-app components. Anything else routes through `ask_user` text or a future schema-driven form builder.
- Multi-photo capture in one question. If a task needs three photos, the agent calls `capture_photo` three times.
