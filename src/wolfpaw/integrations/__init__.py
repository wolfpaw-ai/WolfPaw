"""OAuth integrations with the user's real services.

v2 Phase C adds the first wave: Dropbox (step 29), Notion (step 30),
Microsoft Calendar (step 32). Google Calendar (31) + Gmail readonly
(33) come later, gated on Google's sensitive-tier / CASA verification
work that lives outside this codebase.

Shape every integration follows:

  1. Per-user OAuth flow (`<provider>/oauth.py`): mint a state token
     bound to the Wolfpaw user_id, redirect to the provider, accept
     the callback, exchange the code for tokens, persist them.
  2. Per-provider token table (`<provider>_links`) holding
     access + refresh tokens + expiry.
  3. A client (`<provider>/client.py`) that handles HTTP +
     token-refresh-on-expiry transparently.
  4. Tool implementations (`<provider>/tools.py`) registered via
     `@register_tool` so the Planner picks them up like builtins. The
     tool's run() resolves the user's token at call time; tools fail
     gracefully (with an "install the integration" message) when the
     user hasn't connected the provider yet.
  5. The Planner's prompt surfaces which integrations the user has
     connected so it only picks integration tools when relevant.

Shared infrastructure (`integrations/oauth_state.py`) covers the
state-token issuance + verification pattern. Each provider's tokens
live in its own DAO because the shape varies (Dropbox expires every
~4h, Notion tokens don't expire, MS uses tenant-scoped tokens).
"""
