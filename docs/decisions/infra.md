# ADR: Public app repo + private infra repo (two-repo split)

**Status:** Proposed
**Date:** 2026-05-08

## Context

Wolfpaw ships as both an OSS self-host distribution and a hosted SaaS — the WordPress.org / WordPress.com pattern, see [spec.md](../../spec.md). That gives two audiences with different needs from version control:

- **Self-hosters** want enough deployment scaffolding to run Wolfpaw on a server, laptop, or homelab — but should never see SaaS-only concerns (billing webhooks, multi-tenant glue, prod secrets, customer DNS, monitoring API keys).
- **The SaaS** needs full IaC, secrets management, CD pipelines, and billing integration — none of which belongs in a public repo.

Putting both in one repo leaks SaaS internals into the OSS surface and forces every public PR to think about prod. Putting *no* deployment in the public repo abandons self-hosters and undermines the OSS positioning. Git also can't make a single subdirectory private without footguns (submodules, git-crypt) that leak filenames and create permanent risk of an outside contributor's PR touching private files.

## Decision

Two repositories:

```
github.com/dmitriwolf/wolfpaw            (PUBLIC, OSS)
  └── application code, agents, prompts, schema,
      Dockerfile, docker-compose example, helm chart

github.com/dmitriwolf/wolfpaw-infra      (PRIVATE)
  └── terraform, k8s overlays, CD pipelines,
      billing, multi-tenancy, secrets refs
```

The private repo consumes the public one as a **released Docker image** (`ghcr.io/dmitriwolf/wolfpaw:<tag>`), not as a git submodule. Dependency direction is one-way: `wolfpaw-infra` → `wolfpaw`, never the reverse.

## Repo layouts

```
wolfpaw/                                 (public)
├── src/                                 ← all application code
├── migrations/                          ← schema
├── prompts/                             ← prompt templates
├── tests/
├── Dockerfile                           ← single source of truth for the image
├── docker-compose.yml                   ← self-host: "docker compose up" works
├── deploy/
│   ├── helm/wolfpaw/                    ← optional helm chart for self-hosters
│   └── examples/
│       ├── env.example
│       └── caddy.example.conf
├── docs/
└── README.md                            ← self-host quickstart up top

wolfpaw-infra/                           (private)
├── terraform/
│   ├── modules/
│   └── envs/{staging,prod}/
├── k8s/
│   ├── base/                            ← references public helm chart by version
│   └── overlays/{staging,prod}/
├── pipelines/                           ← GitHub Actions / ArgoCD specs for prod
├── billing/                             ← Stripe webhook handler, plan logic
├── tenancy/                             ← multi-tenant routing, quota enforcement
├── secrets/                             ← SOPS-encrypted, or refs to AWS Secrets Manager
└── runbooks/
```

## What lives where

| Artifact | Public | Private | Reasoning |
|---|:---:|:---:|---|
| Application code, agents, prompts | ✅ | ❌ | The product itself; self-hosters need it |
| Database migrations | ✅ | ❌ | Schema is part of the app |
| Dockerfile | ✅ | ❌ | One image, two deployments |
| `docker-compose.yml` (single-node self-host) | ✅ | ❌ | The OSS quickstart path |
| Helm chart (generic) | ✅ | ❌ | Lets serious self-hosters run on k8s |
| App-level CI (lint, test, build image) | ✅ | ❌ | Belongs next to the code it tests |
| Example `.env` with placeholders | ✅ | ❌ | Documents required config without leaking values |
| Terraform for AWS prod | ❌ | ✅ | Prod topology is a SaaS concern |
| Stripe / billing logic | ❌ | ✅ | SaaS-only feature |
| Multi-tenancy routing, quota enforcement | ❌ | ✅ | SaaS-only feature |
| Plan / tier metadata (Pro = N req/day) | ❌ | ✅ | Business config; OSS reads from env, runs without it |
| Secrets (any form, encrypted or not) | ❌ | ✅ | Never in public, even encrypted |
| LangSmith / CloudWatch keys | ❌ | ✅ | Per [observability.md](observability.md) — third-party data handlers |
| Customer-specific config | ❌ | ✅ | Don't even tempt it |
| CD pipelines to prod | ❌ | ✅ | Operates on private resources |

## How they connect

```
       wolfpaw (public)                          wolfpaw-infra (private)
       ────────────────                          ────────────────────────
            │
       tag v1.4.2
            │
            ▼
       GitHub Actions
       build + push image
            │
            ▼
       ghcr.io/dmitriwolf/wolfpaw:1.4.2 ◄──────── pinned in
                                                  k8s/base/values.yaml
                                                       │
                                                       ▼
                                                  human PR bumps tag
                                                       │
                                                       ▼
                                                  CD → staging → prod
```

`wolfpaw-infra` always pins to a **released tag**, not `main`. Bumping the pin is a deliberate, reviewable PR. This means:
- A broken commit in the app cannot instantly break prod.
- Rollback is `git revert` on a single line in the infra repo.
- The release tag is the contract between the two repos.

For cross-repo iteration during dev, a sibling-checkout pattern works: `wolfpaw-infra/dev/local-compose.yml` mounts `../wolfpaw/src` and rebuilds on change.

## Alternatives considered

### Single repo, all deployment included
Rejected. Either you publish your prod terraform (security and competitive footgun, plus exposes resource names and account IDs that help an attacker map your infra) or you obscure it (defeats the point of OSS). Also forces every external contributor's PR to reason about your prod cluster.

### Single repo with deployment in a private subdirectory (submodules / git-crypt)
Rejected. Git cannot cleanly make a subdirectory private. Submodules leak the existence and history of the private repo; git-crypt leaks filenames. Both create a permanent risk where a contributor's well-intentioned PR mutates private state.

### Two public repos (`wolfpaw` + `wolfpaw-deploy`)
Rejected. Anything genuinely deployable for the SaaS contains either secrets or the *shape* of secrets (resource names, account IDs, tenant routing). The right split isn't public-app vs. public-deploy, it's *example* deployment (public, in `wolfpaw/deploy/`) vs. *prod* deployment (private, in `wolfpaw-infra`).

### Infra repo tracks `main` of app repo
Rejected. Reverse dependency direction. The infra repo should always pin to a released artifact so a broken commit in the app can never instantly break prod, and so the version that ran in prod yesterday is recoverable by SHA.

## Consequences

- **Coordinated changes need two PRs.** A schema change that needs a corresponding infra config change is two PRs across two repos. Mitigated by the release-tag contract: schema migrations ship with a release, infra picks them up on a deliberate version bump.
- **Self-hosters get a real path, not lip service.** `git clone` + edit `.env` + `docker compose up` is the OSS experience. The helm chart is for self-hosters running at scale.
- **SaaS-only features stay invisible.** Billing, multi-tenancy, plan enforcement, prod monitoring keys never appear in public history. The "we accidentally committed secrets" incident becomes structurally harder.
- **Release discipline becomes load-bearing.** The public repo must actually cut releases (semver tags, changelog, image build) for the infra repo to have something to pin to. Skipping this leaves infra pinning to a SHA, which works but obscures intent.
- **External contributors cannot reproduce prod.** Correct and expected. They can reproduce *self-host*, which is the contract.

## Implementation notes

- Cut `v0.1.0` on `wolfpaw` as soon as the docker-compose path works end-to-end self-hosted. That tag becomes the first artifact `wolfpaw-infra` pins to.
- GitHub Actions workflow in `wolfpaw`: on tag push, build multi-arch image, push to `ghcr.io/dmitriwolf/wolfpaw:<tag>`, generate SBOM, attach release notes.
- `wolfpaw-infra` CD watches GHCR for new tags but does *not* auto-deploy — bumping the pinned tag is a human PR.
- The public README leads with self-host instructions and links to wolfpaw.ai for the hosted version. It never references `wolfpaw-infra` — private repos shouldn't appear in public docs.
- License question (TBD per [spec.md](../../spec.md)) is independent of this decision but should be settled before the first public tag.
