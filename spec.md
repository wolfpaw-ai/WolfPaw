# Wolfpaw
### Tread lightly.

Wolfpaw is an agentic personal assistant — an intelligent employee who can follow instructions, learn, and create on your behalf. You decide what Wolfpaw can see and how you want to talk to it: web, Telegram, email forwarding, voice (later), Slack (later). You control your data; Wolfpaw earns its keep through quiet, careful, useful work.

Wolfpaw learns from resources you give it (links, uploaded documents, forwarded emails) and from the work you ask it to do. Over time it gets better at *your* tasks: tracking bills and receipts, summarizing newsletters and research, drafting replies for you to send, watching the data you care about (markets, fitness, news, calendar), reminding you of things, doing your bookkeeping or marketing or inventory if you give it the inputs. Whatever you want a careful assistant for.

## Two ways to use Wolfpaw

**Wolfpaw Cloud (paid, hosted).** Sign up at wolfpaw.ai, pick a plan, talk to your agent in minutes. No API keys, no installation, no developer setup. Inference is included up to your tier's allowance. Pay-as-you-go is opt-in, never automatic.

**Wolfpaw Open Source (self-hosted, free).** Same code, deployable to your own server, laptop, or homelab. Bring your own API keys. Operate your own Telegram bot. You own everything. License TBD.

This is the WordPress.org / WordPress.com model: one project, two distributions. The hosted version exists so non-developers can use Wolfpaw without setup; the OSS version exists so developers can hack on it, run it privately, and verify what it does with their data.

## What makes Wolfpaw different

- **No setup tax.** Most agent projects ask the user to manage API keys, configure model providers, and stand up infrastructure. Wolfpaw Cloud removes all of that. Click subscribe → talk to your agent.
- **Multiple ways to talk to it.** Web chat for the rich UI, Telegram for everyday pings from your phone, email forwarding for "here, deal with this" handoffs. Each channel is built for the way it's actually used.
- **Predictable cost.** Flat-rate plans with a clear allowance. When you hit the cap, the service pauses — it never silently runs up a bill. Overage is opt-in.
- **Tread lightly.** Wolfpaw's persona is the careful employee, not the over-eager assistant. It tells you what it's doing, asks before destructive actions, and admits when it doesn't know.
- **Learns over time.** Procedural memory means Wolfpaw remembers how it solved past problems and gets better at the things you ask for repeatedly.

## Wolfpaw vs OpenClaw — honest comparison

OpenClaw is the closest neighbor to Wolfpaw, and it's a serious project — a few years of head start, mature community, and excellent at what it's built for. The two products are *not* trying to win the same race. OpenClaw is for power users who want a hackable agent running on their own machine with full system access. Wolfpaw is for people who want an agent that *just works* without needing to know what an API key is.

The tables below mark where each product stands today. Honest read: OpenClaw is ahead on capability and ecosystem; Wolfpaw is ahead on accessibility, cost transparency, and SaaS-shape concerns.

Legend: ✅ supported today · 🚧 planned (with version) · ❌ not supported / not on roadmap · ❓ unclear from public docs

### Setup & onboarding

| Feature | OpenClaw | Wolfpaw |
|---|:---:|:---:|
| Zero-setup hosted version (no install, no keys) | ❌ | ✅ |
| One-line install for self-host (`curl \| bash`) | ✅ | 🚧 v1.5 |
| Docker Compose self-host | ❓ | 🚧 v1.5 |
| User must obtain & paste model API keys | required | self-host only |
| User must create their own bot accounts (Telegram, etc.) | required | self-host only |
| Web sign-up flow with email + payment | ❌ | 🚧 v1 |

### Channels

| Channel | OpenClaw | Wolfpaw |
|---|:---:|:---:|
| Web chat (browser UI) | ❌ | 🚧 v1 |
| Telegram | ✅ | 🚧 v1 |
| Email forwarding (inbound) | ❌ | 🚧 v1.5 |
| Email drafts back to owner | ❌ | 🚧 v1.5 |
| Slack | ✅ | 🚧 v2 |
| Discord | ✅ | ❌ |
| WhatsApp | ✅ | ❌ |
| Signal | ✅ | ❌ |
| iMessage | ✅ | ❌ |

### Local-machine capabilities (where OpenClaw is structurally ahead)

These are areas where OpenClaw's local-first architecture gives it abilities Wolfpaw Cloud cannot match by design. Wolfpaw self-host could implement them, but it isn't a v1 priority — Wolfpaw's audience is people who *don't* want an agent with root on their laptop.

| Feature | OpenClaw | Wolfpaw |
|---|:---:|:---:|
| Filesystem read/write on user's machine | ✅ | ❌ |
| Shell command execution on user's machine | ✅ | ❌ |
| Browser automation on user's machine | ✅ | 🚧 v3 (via browser extension; never via cloud headless or native helper) |
| Native cross-platform (macOS / Windows / Linux) | ✅ | ❌ |
| Runs on Raspberry Pi / low-end hardware | ✅ | ❌ |
| Multi-machine orchestration | ✅ | ❌ |

### Cloud integrations & tools

| Feature | OpenClaw | Wolfpaw |
|---|:---:|:---:|
| Web search (Tavily/Brave/SerpAPI) | ✅ | 🚧 v1 |
| HTTP fetch | ✅ | 🚧 v1 |
| Sandboxed SQL / table creation per user | ❓ | 🚧 v1 |
| Per-user file workspace | ❓ | 🚧 v1 |
| Gmail OAuth (read) | ✅ | 🚧 v2 (`gmail.readonly` only; CASA audit timed to public launch) |
| Gmail OAuth (send / compose / delete / modify) | ✅ | ❌ by policy (never) |
| Google Calendar OAuth | ✅ | 🚧 v2 |
| Google Drive OAuth (workspace folder) | ✅ | 🚧 v2 (`drive.file` scope) |
| Dropbox OAuth (App folder) | ❓ | 🚧 v2 |
| OneDrive OAuth (workspace folder) | ❓ | 🚧 v3 |
| iCloud Drive | ❓ | ❌ no public API for hosted services |
| Microsoft Calendar OAuth | ✅ | 🚧 v2 |
| GitHub OAuth | ✅ | ❌ hosted / ✅ self-host *(wrong audience for hosted)* |
| Notion / Obsidian | ✅ | 🚧 v2 (Notion only) |
| 50+ pre-built integrations | ✅ | ❌ |

### Memory & learning

| Feature | OpenClaw | Wolfpaw |
|---|:---:|:---:|
| Persistent conversational memory | ✅ | 🚧 v1 |
| Procedural memory (past plans + score-based retrieval) | ❓ informal | 🚧 v1 (formalized) |
| Vector-based retrieval over memory | ❓ | 🚧 v1 |
| Self-modifying skills (agent writes its own tools) | ✅ | 🚧 v2 |
| Skills marketplace (community-shared) | ✅ ClawHub | ❌ |
| Persona / soul file that shapes agent behavior | ❓ | 🚧 v1 |
| Memory inspectable & deletable by user | ❓ | 🚧 v1 |

### Cost & billing

This is where Wolfpaw is most clearly ahead — by design, since the hosted product can only work if cost is transparent and bounded.

| Feature | OpenClaw | Wolfpaw |
|---|:---:|:---:|
| Bundled inference (no model API key needed) | ❌ | 🚧 v1 (hosted) |
| Token usage metered per request, per model, per agent | ❓ | 🚧 v1 |
| `/usage` command across all channels | ❌ | 🚧 v1 |
| Cost notifications at % of budget | ❌ | 🚧 v1 |
| Hard pause at user-set cap | ❌ | 🚧 v1 |
| Opt-in pay-as-you-go overage | ❌ | 🚧 v1 |
| Subscription tiers via Stripe | ❌ | 🚧 v1 |
| User pays inference costs directly to model provider | ✅ | self-host only |

### Distribution & licensing

| Feature | OpenClaw | Wolfpaw |
|---|:---:|:---:|
| Open source | ✅ | 🚧 v1 (license TBD) |
| Hosted SaaS option | ❌ | 🚧 v1 |
| Self-host option | ✅ | 🚧 v1.5 |
| Same codebase serves both distributions | n/a | 🚧 v1 |
| Multi-tenant architecture | ❌ (single-user local) | 🚧 v1 |

### Community & ecosystem

This is the area where OpenClaw's head start is hardest to close. Wolfpaw won't catch up on these dimensions in v1, and probably not in v2.

| Feature | OpenClaw | Wolfpaw |
|---|:---:|:---:|
| Active user community / Discord | ✅ | ❌ |
| Skills marketplace with community contributions | ✅ | ❌ |
| Major sponsors (OpenAI, GitHub, NVIDIA, Vercel) | ✅ | ❌ |
| Years in market | ✅ | ❌ pre-launch |
| Mature documentation | ✅ | ❌ |

### Persona & defaults

| Feature | OpenClaw | Wolfpaw |
|---|:---:|:---:|
| Default-cautious ("ask before destructive action") | ❓ | 🚧 v1 |
| Default-capable ("eyes and hands at a desk") | ✅ | partial — by design more conservative |
| Proactive scheduled tasks ("heartbeats") | ✅ | 🚧 v2 |
| Agent admits uncertainty plainly | ❓ | 🚧 v1 (in soul file) |

### Reading the tables

A few takeaways an honest reader should walk away with:

- **OpenClaw is the more capable product today** in every dimension that matters for power users on their own machine. If that's the audience, OpenClaw is the better recommendation.
- **OpenClaw doesn't serve the audience Wolfpaw is built for** — the person who wants an agent that just works, with predictable cost, accessed from a phone or browser, without managing infrastructure. That's not a flaw in OpenClaw; it's a different product shape.
- **Wolfpaw's structural wins are in cost transparency and zero-setup onboarding.** Those flow directly from the hosted SaaS model and are inherent to the architecture.
- **Wolfpaw's structural losses are in local-machine capabilities and ecosystem maturity.** Local capabilities are by-design absent in the hosted version. Ecosystem maturity is just time.
- **The ❓ marks are real.** I haven't independently verified every OpenClaw feature; some entries reflect uncertainty about what their docs actually claim vs. what the community has built around it. Treat the table as a working draft, not a fact sheet.

## How it works (high level)

A user message — typed in the web app, sent to the Telegram bot, or forwarded by email — flows through a structured loop:

1. **Triage.** A small fast model figures out what the user actually wants and how big a job it is.
2. **Plan** (when the job is non-trivial). A larger model breaks the goal into steps, consults procedural memory for similar past plans, and assembles a plan using the available tools.
3. **Execute.** Each step runs — some are pure tool calls (search, calculator, SQL, file read), some are reasoning steps that use a model.
4. **Evaluate.** The plan's outcome is scored and stored, so the next similar request can build on it.

Memory is built in at multiple layers: conversational (per-thread chat), procedural (past plans + scores), and a soul file that defines Wolfpaw's persona. A static toolbox handles the common operations; later versions will let Wolfpaw create its own tools.

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

TBD. Will be open-source-friendly; specific license decided later.
