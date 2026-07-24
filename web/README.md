# web/

React + Vite SPA for Wolfpaw — step 19. Talks to the FastAPI backend in `src/wolfpaw/` over the same session cookie everything else uses.

## Layout

```
web/
  package.json
  vite.config.ts        # dev proxy: /auth, /channels, /workspace, /tasks, /usage, /monitor, /me, /health → :8000
  tsconfig.json
  index.html
  src/
    main.tsx            # entry — mounts <App />
    App.tsx             # routes + AuthProvider + AppLayout
    api/
      client.ts         # typed fetch wrapper. Cookie auth (same-origin), no Authorization header.
      types.ts          # request/response shapes (hand-maintained mirrors of Pydantic models)
    auth/
      AuthContext.tsx   # session state via GET /auth/me
      RequireAuth.tsx   # route guard
      SignInPage.tsx    # magic-link request
      VerifyPage.tsx    # consumes ?token=… from the magic link
    chat/
      ChatPage.tsx      # main chat surface
      sseClient.ts      # POST-based SSE parser (EventSource is GET-only)
    tasks/
      TasksPage.tsx       # list
      TaskDetailPage.tsx  # detail + cancel
    files/
      FilesPage.tsx     # workspace files list + download
    profile/
      ProfilePage.tsx   # User File editor + Telegram link minting
    usage/
      UsagePage.tsx     # /usage tabs (default / today / month / all)
    monitor/
      MonitorPage.tsx   # model-call observability: health tiles, failure
                        # breakdown, trace list → per-call drill-down
    layout/
      AppLayout.tsx     # nav + <Outlet />
      Nav.tsx
    lib/
      format.ts         # date / dollars / int helpers
    styles.css          # one global stylesheet; no Tailwind / CSS framework
```

## Run it

```bash
cd web
npm install
npm run dev     # http://localhost:5173 ← React app
                # http://localhost:8000 ← FastAPI backend (see top-level README)
```

The dev server proxies API paths so the cookie stays on a single origin. In production both sides land behind Caddy on the same domain (step 21 wires that).

## Sign-in flow in dev

1. `npm run dev` (React, :5173) + `uvicorn wolfpaw.api:app --reload` (FastAPI, :8000) in two terminals.
2. Visit http://localhost:5173 → bounces to `/signin`.
3. Enter your email, submit.
4. The backend's `console` email backend (default) logs the verify URL to stdout. Look in the uvicorn output for a line like `auth.magic_link.issued ... Sign in to Wolfpaw: http://localhost:3000/auth/verify?token=...`. Replace the host with `http://localhost:5173/signin/verify` and open that URL.
5. `VerifyPage` consumes the token, the backend sets the `wp_session` cookie, you land on `/chat`.

## What's deferred

These ship as follow-ups; the backend supports them but the React UI doesn't surface them in v1:

- **File uploads.** Backend has `POST /workspace/upload-url` + PUT bytes + `POST /workspace/files` already; needs a drag-and-drop UI.
- **Charts on the usage dashboard.** v1 is tables only; a small Recharts integration is a one-pager once tables work.
- **Visual design / theming.** Minimal CSS, no dark mode toggle, no design system. Replace `styles.css` with Tailwind or a real design system when ready.
- **Frontend tests.** No vitest setup. The backend tests cover the API contract; the UI is small enough to manually verify in v1.
- **PWA / offline / service worker.** Not in scope.
- **i18n.** English only.

## Integration with the rest of the codebase

- **Session cookie** (`wp_session`) is the source of truth — set by the backend on `/auth/verify`, sent automatically on every same-origin request, validated by `Depends(require_user_id)` on every protected endpoint.
- **Slash commands** still work in the chat surface: type `/help`, `/usage`, `/tasks`, `/task <id>`, `/cancel <id>` exactly as in `curl`. The dispatcher intercepts before any model call.
- **SSE stream** events the chat page handles: `thread` (remember the id for follow-up turns), `command` (slash command result text), `triage` / `plan` / `tool` / `step.start` / `step.end` / `step.error` / `score` / `task` (collected into a per-turn timeline), `ask_user` (surfaces an inline reply form that POSTs to `/channels/web/answer`), `delta` (the final answer streamed into the assistant turn), `done`, `error`.
- **Profile edits** (`/me/profile`) flow into every agent's system prompt on the next turn via `persona.builder.build_for_agent` (step 17).
- **Telegram link** uses `POST /channels/telegram/link-token` to mint a deep link; the user taps it, the bot's webhook handler writes `channel_links`, and they're connected.
