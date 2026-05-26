# OAuth integrations — provider setup

Phase C (v2) ships three OAuth integrations. Each one needs an app
registered with the provider before `.env` credentials work.

Every redirect URL has the same shape:

    {WOLFPAW_WEB_BASE_URL}/integrations/{provider}/oauth/callback

Make sure `WOLFPAW_WEB_BASE_URL` matches the domain users hit (https
in production). The redirect must be **exact** — providers reject
mismatches.

After updating `.env`, restart the `app` service:

    docker compose up -d --build app
    docker compose restart web   # nginx upstream DNS

---

## Dropbox (step 29)

Three tools: `dropbox_list_folder`, `dropbox_read_file`, `dropbox_write_file`. App-folder scope — read/write stays under `/Apps/Wolfpaw/`.

1. Go to [dropbox.com/developers/apps](https://www.dropbox.com/developers/apps) → **Create app**.
2. Choose:
   - **API**: Scoped access
   - **Type of access**: **App folder** (not Full Dropbox)
   - **Name**: Wolfpaw (or anything unique)
3. In **Settings**, set the redirect URI:
   ```
   https://ec2.wolfpaw.ai/integrations/dropbox/oauth/callback
   ```
4. In **Permissions**, enable: `files.content.read`, `files.content.write`. Click **Submit**.
5. Copy the **App key** and **App secret** into `.env`:
   ```
   WOLFPAW_DROPBOX_CLIENT_ID=<App key>
   WOLFPAW_DROPBOX_CLIENT_SECRET=<App secret>
   ```

---

## Notion (step 30)

Three tools: `notion_search`, `notion_read_page`, `notion_create_page`. Workspace-scoped — only sees pages the user explicitly shares with the integration.

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) → **New integration**.
2. Choose **Public** integration (lets multiple users authorize).
3. Set the OAuth redirect URI:
   ```
   https://ec2.wolfpaw.ai/integrations/notion/oauth/callback
   ```
4. Capabilities: enable **Read content**, **Update content**, **Insert content**. Default user-info access is fine.
5. Copy the **OAuth client ID** and **OAuth client secret** into `.env`:
   ```
   WOLFPAW_NOTION_CLIENT_ID=<client ID>
   WOLFPAW_NOTION_CLIENT_SECRET=<client secret>
   ```

After connecting in the Wolfpaw web app, the user separately shares specific pages with the Wolfpaw integration in Notion's page-share menu. Pages not shared this way are invisible to the agent.

---

## Microsoft Calendar (step 32)

Two tools: `outlook_calendar_list_events`, `outlook_calendar_create_event`.

1. Go to [portal.azure.com](https://portal.azure.com) → **Microsoft Entra ID** → **App registrations** → **New registration**.
2. Settings:
   - **Name**: Wolfpaw
   - **Supported account types**: "Accounts in any organizational directory and personal Microsoft accounts" (for `common` tenant) — or pin to your tenant
   - **Redirect URI**: select **Web** + paste:
     ```
     https://ec2.wolfpaw.ai/integrations/microsoft/oauth/callback
     ```
3. After creation, copy the **Application (client) ID** — this is your client_id.
4. Go to **Certificates & secrets** → **New client secret**. Copy the **Value** (not the Secret ID).
5. Go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions**. Add:
   - `Calendars.ReadWrite`
   - `offline_access`
   - `User.Read`
6. (Optional but recommended) click **Grant admin consent** so users don't see a consent prompt on first connect.
7. Paste into `.env`:
   ```
   WOLFPAW_MICROSOFT_CLIENT_ID=<Application (client) ID>
   WOLFPAW_MICROSOFT_CLIENT_SECRET=<secret Value>
   WOLFPAW_MICROSOFT_TENANT=common   # or your specific tenant id
   ```

---

## Verifying a setup

After restart, hit the install-URL endpoint as a logged-in user:

    curl -b session.txt https://ec2.wolfpaw.ai/integrations/dropbox/install-url

A successful response is `{"url": "https://www.dropbox.com/oauth2/authorize?..."}`. A 503 means `.env` is blank or didn't reload.

Open that URL in a browser, grant access, and you'll land back at a "Connected" page. From then on the agent's `dropbox_*` tools work for that user.

To disconnect:

    curl -X DELETE -b session.txt https://ec2.wolfpaw.ai/integrations/dropbox

---

## Deferred (steps 31, 33)

**Google Calendar** + **Gmail readonly** are deferred. Google's sensitive-tier verification (Calendar) takes weeks; Gmail's restricted-tier scope plus the CASA audit (~$15-75k/year) is gated on revenue. Start the Google review *before* writing more code — see `v2_implementation_plan.md` steps 31 + 33 for the gating notes.
