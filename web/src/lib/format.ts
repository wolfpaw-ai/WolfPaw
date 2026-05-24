/** Render an ISO timestamp (or null) for the UI. Falls back to "—" so
 *  table cells don't shift. */
export function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

/** "$1.23" for whole-cent values. */
export function formatDollars(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}

/** "1,234,567" — same shape as the /usage slash command's text output. */
export function formatInt(n: number): string {
  return n.toLocaleString("en-US");
}

/** Truncate a string in the middle for short displays (uuids etc.). */
export function shortenId(id: string, head = 8, tail = 4): string {
  if (id.length <= head + tail + 1) return id;
  return `${id.slice(0, head)}…${id.slice(-tail)}`;
}
