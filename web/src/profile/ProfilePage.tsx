// User File editor + Telegram channel linking. The User File flows
// into every agent's system prompt via persona.builder (step 17), so
// edits here change the next turn's behavior.

import { FormEvent, useCallback, useEffect, useState } from "react";
import { ApiError, profile as profileApi, telegram } from "../api/client";
import type { UserProfile } from "../api/types";

export function ProfilePage() {
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [draftMd, setDraftMd] = useState("");
  const [draftTz, setDraftTz] = useState("UTC");
  const [draftPrefs, setDraftPrefs] = useState("{}");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const p = await profileApi.get();
      setProfile(p);
      setDraftMd(p.persona_md);
      setDraftTz(p.timezone);
      setDraftPrefs(JSON.stringify(p.preferences, null, 2));
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    let prefs: Record<string, unknown>;
    try {
      prefs = JSON.parse(draftPrefs);
    } catch {
      setError("Preferences must be valid JSON.");
      setSaving(false);
      return;
    }
    try {
      const updated = await profileApi.patch({
        persona_md: draftMd,
        timezone: draftTz,
        preferences: prefs,
      });
      setProfile(updated);
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setSaving(false);
    }
  }

  if (error && profile === null) {
    return <main className="page"><p className="error">{error}</p></main>;
  }
  if (profile === null) {
    return <main className="page"><p>Loading…</p></main>;
  }

  return (
    <main className="page">
      <h1>Your profile</h1>
      <p className="hint">
        These details are loaded into every agent's system prompt. The
        agent uses them for tone, routing, and grounding. Version bumps
        whenever you save.
      </p>
      <form onSubmit={onSubmit}>
        <label>
          About you (persona, free-form Markdown)
          <textarea
            value={draftMd}
            onChange={(e) => setDraftMd(e.target.value)}
            rows={10}
            placeholder="e.g. I'm Alice, a product manager based in NYC. I prefer concise answers and metric units."
          />
        </label>
        <label>
          Timezone (IANA)
          <input
            value={draftTz}
            onChange={(e) => setDraftTz(e.target.value)}
            placeholder="UTC"
          />
        </label>
        <label>
          Structured preferences (JSON object)
          <textarea
            value={draftPrefs}
            onChange={(e) => setDraftPrefs(e.target.value)}
            rows={6}
            spellCheck={false}
          />
        </label>
        <button type="submit" disabled={saving}>
          {saving ? "Saving…" : "Save profile (bumps to v" + (profile.version + 1) + ")"}
        </button>
        {error && <p className="error">{error}</p>}
      </form>

      <h2>Channels</h2>
      <TelegramLinkButton />
    </main>
  );
}


function TelegramLinkButton() {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onMint() {
    setBusy(true);
    setError(null);
    try {
      const r = await telegram.mintLink();
      setUrl(r.url);
    } catch (e) {
      setError(
        e instanceof ApiError ? String(e.detail) : String(e),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <p>
        Link Telegram by tapping a freshly-minted deep link. The link is
        single-use and expires in 15 minutes.
      </p>
      <button onClick={onMint} disabled={busy}>
        {busy ? "Minting…" : "Mint Telegram link"}
      </button>
      {url && (
        <p>
          <a href={url} target="_blank" rel="noopener noreferrer">
            Open in Telegram →
          </a>
          <br />
          <small>{url}</small>
        </p>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
