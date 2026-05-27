# Wolfpaw
### Tread lightly.

Wolfpaw is an agentic worker — an intelligent employee who can follow instructions, learn, and *get real work done* on your behalf. Not just an assistant that answers questions in chat: Wolfpaw runs code, takes on multi-day tasks, produces real deliverables (spreadsheets, PDFs, slide decks), and works in the background while you're doing other things. You decide what it can see and how you want to talk to it: web, Telegram, email forwarding, voice (later), Slack (later). You control your data; Wolfpaw earns its keep through quiet, careful, useful work.

Wolfpaw learns from resources you give it (links, uploaded documents, forwarded emails) and from the work you ask it to do. Over time it gets better at *your* tasks: tracking bills and receipts, summarizing newsletters and research, drafting replies for you to send, watching the data you care about (markets, fitness, news, calendar), reminding you of things, doing your bookkeeping or marketing or inventory if you give it the inputs. Whatever you want a careful assistant for.

## Two ways to use Wolfpaw

**Wolfpaw Cloud (paid, hosted).** Sign up at wolfpaw.ai, pick a plan, talk to your agent in minutes. No API keys, no installation, no developer setup. Inference is included up to your tier's allowance. Pay-as-you-go is opt-in, never automatic.

**Wolfpaw Open Source (self-hosted, free).** Same code, deployable to your own server, laptop, or homelab. Bring your own API keys. Operate your own Telegram bot. You own everything. MIT licensed.

This is the WordPress.org / WordPress.com model: one project, two distributions. The hosted version exists so non-developers can use Wolfpaw without setup; the OSS version exists so developers can hack on it, run it privately, and verify what it does with their data.

## What makes Wolfpaw different

- **Actually does work.** Wolfpaw runs Python in a sandbox, processes files, hits APIs, produces real deliverables (Excel, PDF, charts, slides). Not a chatbot — a worker.
- **Long-running tasks.** Hand Wolfpaw a multi-step job ("research my upcoming vendors and put together a comparison spreadsheet"); it works in the background, pings you when blocked or done, picks up where it left off across days.
- **Sub-agents in parallel.** Big jobs get split into parallel branches with their own budget and their own work. The synthesis comes back as one result.
- **No setup tax.** Most agent projects ask the user to manage API keys, configure model providers, and stand up infrastructure. Wolfpaw Cloud removes all of that. Click subscribe → talk to your agent.
- **Multiple ways to talk to it.** Web chat for the rich UI, Telegram for everyday pings from your phone, email forwarding for "here, deal with this" handoffs. Each channel is built for the way it's actually used.
- **Predictable cost.** Flat-rate plans with a clear allowance. When you hit the cap, the service pauses — it never silently runs up a bill. Overage is opt-in.
- **Tread lightly.** Wolfpaw's persona is the careful employee, not the over-eager assistant. It tells you what it's doing, asks before destructive actions, and admits when it doesn't know.
- **Learns over time.** Procedural memory means Wolfpaw remembers how it solved past problems and gets better at the things you ask for repeatedly.

## Wolfpaw vs OpenClaw and Cowork — honest comparison

Wolfpaw has two close neighbors worth comparing against:

- **OpenClaw** — the open-source, local-first, power-user agent. A few years of head start, mature community, excellent at what it's built for: a hackable worker running on your own machine with full system access.
- **Claude Cowork** — Anthropic's first-party agentic AI for knowledge work. Bundled inference, scheduled tasks, real deliverables (xlsx/pptx/docx/pdf skills), Google Drive / Gmail / Slack / Chrome / DocuSign / FactSet connectors, approval workflows. Desktop-first with deep local file and app access; lives inside Anthropic's accounts and paid plans.

The three products are *not* trying to win the same race. OpenClaw is for power users who want full system access on their own hardware. Cowork is for desktop knowledge workers who want a polished hosted agent from the model vendor itself. Wolfpaw is for people who want an agent that *just works* via the channels they already use — web, phone, inbox — without managing infrastructure, with explicit cost mechanics, read-only-by-default scopes, and the option to self-host.

The tables below mark where each product stands today. Honest read: OpenClaw is ahead on local capability and ecosystem; Cowork is ahead on integrations, distribution, and Skills depth; Wolfpaw is ahead on channel mix, cost transparency, privacy posture, and the OSS / self-host option.

Legend: ✅ supported today · 🚧 planned (with version) · ❌ not supported / not on roadmap · ❓ unclear from public docs

### Setup & onboarding

| Feature | OpenClaw | Cowork | Wolfpaw |
|---|:---:|:---:|:---:|
| Zero-setup hosted version (no install, no keys) | ❌ | partial — desktop app install required | ✅ |
| One-line install for self-host (`curl \| bash`) | ✅ | ❌ | 🚧 v1.5 |
| Docker Compose self-host | ❓ | ❌ | 🚧 v1.5 |
| User must obtain & paste model API keys | required | ❌ (bundled in Anthropic plan) | self-host only |
| User must create their own bot accounts (Telegram, etc.) | required | ❌ | self-host only |
| Web sign-up flow with email + payment | ❌ | ✅ (Anthropic plans) | 🚧 v1 |

### Channels

| Channel | OpenClaw | Cowork | Wolfpaw |
|---|:---:|:---:|:---:|
| Web chat (browser UI) | ❌ | ❌ (desktop-only) | 🚧 v1 |
| Desktop app (native macOS / Windows) | ❌ | ✅ | ❌ |
| Mobile companion (paired to desktop) | ❌ | ✅ | ❌ |
| Telegram | ✅ | ❌ | 🚧 v1 |
| Email forwarding (inbound) | ❌ | ❌ | 🚧 v1.5 |
| Email drafts back to owner | ❌ | ✅ (via Gmail connector) | 🚧 v1.5 |
| Slack | ✅ | ✅ | 🚧 v2 |
| Discord | ✅ | ❌ | ❌ |
| WhatsApp | ✅ | ❌ | ❌ |
| Signal | ✅ | ❌ | ❌ |
| iMessage | ✅ | ❌ | ❌ |

### Local-machine capabilities (where OpenClaw and Cowork are structurally ahead)

Both OpenClaw and Cowork are local-first by architecture. Cowork ships an installable desktop app with file/app access; OpenClaw runs as a local agent with full system access. Wolfpaw Cloud cannot match these by design. Wolfpaw self-host could implement them, but it isn't a v1 priority — Wolfpaw's audience is people who *don't* want an agent with root on their laptop.

| Feature | OpenClaw | Cowork | Wolfpaw |
|---|:---:|:---:|:---:|
| Filesystem read/write on user's machine | ✅ | ✅ (user-designated folders) | ❌ |
| Shell command execution on user's machine | ✅ | ❓ (Code mode, not Cowork itself) | ❌ |
| Browser automation on user's machine | ✅ | ✅ (Chrome connector) | 🚧 v3 (via browser extension; never via cloud headless or native helper) |
| Native cross-platform (macOS / Windows / Linux) | ✅ | partial — macOS / Windows; Linux ❓ | ❌ |
| Runs on Raspberry Pi / low-end hardware | ✅ | ❌ | ❌ |
| Multi-machine orchestration | ✅ | ❌ | ❌ |

### Cloud integrations & tools

| Feature | OpenClaw | Cowork | Wolfpaw |
|---|:---:|:---:|:---:|
| Web search (Tavily/Brave/SerpAPI) | ✅ | ✅ | 🚧 v1 |
| HTTP fetch | ✅ | ✅ | 🚧 v1 |
| Sandboxed SQL / table creation per user | ❓ | ❓ | 🚧 v1 |
| Per-user file workspace | ❓ | ✅ (local folders) | 🚧 v1 |
| Gmail OAuth (read) | ✅ | ✅ | 🚧 v2 (`gmail.readonly` only; CASA audit timed to public launch) |
| Gmail OAuth (send / compose / delete / modify) | ✅ | ✅ | ❌ by policy (never) |
| Google Calendar OAuth | ✅ | ❓ | 🚧 v2 |
| Google Drive OAuth (workspace folder) | ✅ | ✅ | 🚧 v2 (`drive.file` scope) |
| Dropbox OAuth (App folder) | ❓ | ❓ | 🚧 v2 |
| OneDrive OAuth (workspace folder) | ❓ | ❓ | 🚧 v3 |
| iCloud Drive | ❓ | ❌ | ❌ no public API for hosted services |
| Microsoft Calendar OAuth | ✅ | ❓ | 🚧 v2 |
| GitHub OAuth | ✅ | ❓ | ❌ hosted / ✅ self-host *(wrong audience for hosted)* |
| Notion / Obsidian | ✅ | ❓ | 🚧 v2 (Notion only) |
| DocuSign | ❓ | ✅ | ❌ |
| FactSet | ❓ | ✅ | ❌ |
| 50+ pre-built integrations | ✅ | ❓ growing | ❌ |

### Memory & learning

| Feature | OpenClaw | Cowork | Wolfpaw |
|---|:---:|:---:|:---:|
| Persistent conversational memory | ✅ | ✅ (Claude memory) | 🚧 v1 |
| Procedural memory (past plans + score-based retrieval) | ❓ informal | ❓ | 🚧 v1 (formalized) |
| Vector-based retrieval over memory | ❓ | ❓ | 🚧 v1 |
| Self-modifying skills (agent writes its own tools) | ✅ | partial — Skills system; self-modification ❓ | 🚧 v2 |
| Skills marketplace (community-shared) | ✅ ClawHub | ❓ | ❌ |
| Persona / soul file that shapes agent behavior | ❓ | ❌ (system-prompt customization, not a soul file) | 🚧 v1 |
| Memory inspectable & deletable by user | ❓ | ✅ | 🚧 v1 |

### Real work (the agentic-worker dimension)

| Feature | OpenClaw | Cowork | Wolfpaw |
|---|:---:|:---:|:---:|
| Code execution sandbox (run arbitrary Python) | ✅ via local shell | ✅ (Code mode / sandbox) | 🚧 v1 (sandboxed Python via E2B / Docker) |
| Long-running tasks (work that spans days) | ❓ via heartbeats | partial — scheduled tasks; multi-day continuity ❓ | 🚧 v1 (first-class `Task` objects, status, resume) |
| Sub-agent delegation (parallel work streams) | ❓ | ❓ | 🚧 v1 |
| Real deliverables (.xlsx, .pdf, .pptx, charts) | ❓ | ✅ (xlsx/pptx/docx/pdf skills) | 🚧 v1 |
| Background work / scheduled tasks ("heartbeats") | ✅ | ✅ | 🚧 v2 |
| Per-task budgets independent of period allowance | ❌ | ❌ | 🚧 v1 |
| Compute-time metering separate from token metering | ❌ | ❓ | 🚧 v1 |

### Cost & billing

Both Cowork and Wolfpaw bundle inference. Where Wolfpaw is differentiated is the transparency layer on top — `/usage` visibility, hard caps at a user-set ceiling, and opt-in overage — by design, since the hosted product can only work if cost is bounded and predictable.

| Feature | OpenClaw | Cowork | Wolfpaw |
|---|:---:|:---:|:---:|
| Bundled inference (no model API key needed) | ❌ | ✅ | 🚧 v1 (hosted) |
| Token usage metered per request, per model, per agent | ❓ | ❓ | 🚧 v1 |
| `/usage` command across all channels | ❌ | ❓ (Anthropic usage view exists; not channel-level) | 🚧 v1 |
| Cost notifications at % of budget | ❌ | ❓ | 🚧 v1 |
| Hard pause at user-set cap | ❌ | ❌ (rate limits, not user-set caps) | 🚧 v1 |
| Opt-in pay-as-you-go overage | ❌ | ❓ | 🚧 v1 |
| Subscription tiers via Stripe | ❌ | ✅ (Anthropic billing) | 🚧 v1 |
| User pays inference costs directly to model provider | ✅ | n/a | self-host only |

### Distribution & licensing

| Feature | OpenClaw | Cowork | Wolfpaw |
|---|:---:|:---:|:---:|
| Open source | ✅ | ❌ | ✅ MIT |
| Hosted SaaS option | ❌ | partial — desktop app + Anthropic accounts | 🚧 v1 |
| Self-host option | ✅ | ❌ | 🚧 v1.5 |
| Same codebase serves both distributions | n/a | n/a | 🚧 v1 |
| Multi-tenant architecture | ❌ (single-user local) | ❌ (single-user desktop) | 🚧 v1 |
| Model portability (not locked to one provider) | ✅ | ❌ (Claude only) | 🚧 v2 |

### Community & ecosystem

OpenClaw has a years-long head start. Cowork has Anthropic's distribution. Wolfpaw won't catch up on these dimensions in v1, and probably not in v2.

| Feature | OpenClaw | Cowork | Wolfpaw |
|---|:---:|:---:|:---:|
| Active user community / Discord | ✅ | partial — broad Claude community, not Cowork-specific | ❌ |
| Skills marketplace with community contributions | ✅ | ❓ (Skills system exists; community marketplace ❓) | ❌ |
| Major sponsors (OpenAI, GitHub, NVIDIA, Vercel) | ✅ | n/a — built by Anthropic itself | ❌ |
| Years in market | ✅ | partial — launched 2025–2026 | ❌ pre-launch |
| Mature documentation | ✅ | partial — Anthropic docs + tutorials growing | ❌ |

### Persona & defaults

| Feature | OpenClaw | Cowork | Wolfpaw |
|---|:---:|:---:|:---:|
| Default-cautious ("ask before destructive action") | ❓ | ✅ (approval workflows) | 🚧 v1 |
| Default-capable ("eyes and hands at a desk") | ✅ | ✅ | partial — by design more conservative |
| Proactive scheduled tasks ("heartbeats") | ✅ | ✅ | 🚧 v2 |
| Agent admits uncertainty plainly | ❓ | ❓ | 🚧 v1 (in soul file) |

### Reading the tables

A few takeaways an honest reader should walk away with:

- **OpenClaw is the more capable product today on its own machine.** Different audience (power users), different shape (local-first, hackable). If that's the audience, OpenClaw is the better recommendation.
- **Cowork is the closest structural competitor.** It already has bundled inference, scheduled tasks, real-deliverable skills, Slack integration, and Anthropic's distribution. Wolfpaw's defensible ground is *not* "we have scheduled tasks too" — it's the channel mix (Telegram + email forwarding), explicit cost mechanics (`/usage`, hard pause at cap, opt-in overage), read-only-by-default scopes, model portability, and the OSS / self-host option.
- **Wolfpaw's structural wins** are in cost transparency, channel reach beyond desktop, privacy posture (read-only Gmail by policy, no sending), and the OSS / self-host distribution. Those flow from a SaaS shape that's intentionally not desktop-first and intentionally not Anthropic-only.
- **Wolfpaw's structural losses** are local-machine capabilities (by design, vs. both neighbors), ecosystem maturity (vs. OpenClaw), and the depth of integrations and distribution Anthropic itself can ship (vs. Cowork). Local capabilities won't be matched in the hosted version; ecosystem maturity is just time; matching Anthropic on first-party integrations isn't the bet.
- **The ❓ marks are real.** I haven't independently verified every OpenClaw or Cowork feature; some entries reflect uncertainty about what their docs claim vs. what the community has built around them. Treat the table as a working draft, not a fact sheet.

## How it works (high level)

A user message — typed in the web app, sent to the Telegram bot, or forwarded by email — flows through a structured loop:

1. **Triage.** A small fast model figures out what the user actually wants and how big a job it is.
2. **Plan** (when the job is non-trivial). A larger model breaks the goal into steps, consults procedural memory for similar past plans, and assembles a plan. If the work is multi-step or produces deliverables, Wolfpaw creates a **Task** — a persistent unit of work that can run for hours or days, pause when blocked, and resume later.
3. **Execute.** Each step runs — pure tool calls (search, SQL, file read), reasoning steps that use a model, or **code execution** in a sandboxed Python environment. Big plans branch into parallel **sub-agents** with their own budgets, then synthesize their outputs.
4. **Evaluate.** The plan's outcome is scored and stored, so the next similar request can build on it.

Memory is built in at multiple layers: conversational (per-thread chat), procedural (past plans + scores), and a soul file that defines Wolfpaw's persona. Tasks persist across sessions; artifacts (spreadsheets, PDFs, slides, charts) are saved to your workspace folder.

For technical detail, schema, deployment, and build order, see [implementation_plan.md](implementation_plan.md).

## Channels (v1 → v2)

- **v1:** Web chat, Telegram bot
- **v1.5:** Email forwarding (`alice@wolfpaw.ai` accepts forwarded mail; Wolfpaw replies with summaries or drafts to the verified owner address only — never sends to the world)
- **v2:** Slack workspace app, scheduled tasks, voice
- **v3+:** Mobile app, iMessage, WhatsApp

## Subscription model

Flat-rate plans with included inference allowance. When you hit your cap, the service pauses and tells you. You can upgrade your plan, or opt in to pay-as-you-go overage at a fixed rate. We send cost notifications at 50%, 80%, and 100% of your allowance, plus an opt-in daily summary.

Token usage is metered per request, per model, per channel from day one — visible to you at any time via the `/usage` command in any channel, or in the web app. We don't profit from confusion about your bill.

**Tier structure is intentionally undecided.** We'll set specific tiers, allowances, and prices after watching how people actually use Wolfpaw. The mechanics — flat rate + included allowance, pause-at-cap, opt-in overage, transparent metering — are the commitments. The numbers come later.

## What you control

- **What channels Wolfpaw uses to reach you** — turn each on or off.
- **What memory Wolfpaw keeps** — view, search, and delete entries from conversational and procedural memory at any time.
- **What tools Wolfpaw has** — opt-in to integrations (Gmail/Calendar/Drive/etc) one at a time. Wolfpaw never gets access you didn't grant.
- **Whether overage is allowed** — off by default. The service pauses at your cap unless you opt in.
- **Whether your data leaves your tenant** — never used for model training. Hosted on AWS in our infra; encrypted at rest. (Self-host = your infra, your call.)

## License

MIT — see [LICENSE](LICENSE).
