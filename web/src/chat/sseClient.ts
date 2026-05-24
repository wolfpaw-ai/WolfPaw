// POST-based SSE client. Native EventSource is GET-only, so we run
// fetch + a tiny SSE frame parser over the response body's
// ReadableStream. Each line `event: X\ndata: Y\n\n` becomes one
// SseEvent emitted via the consumer callback.

export interface SseEvent {
  event: string;
  data: string;
}

export interface ChatStreamOptions {
  content: string;
  thread_id?: string | null;
  onEvent: (event: SseEvent) => void;
  signal?: AbortSignal;
}

/**
 * Open a chat stream against POST /channels/web/chat. Resolves when the
 * stream ends naturally (server sent `event: done` or closed). Rejects
 * on network failure or non-200 status.
 */
export async function streamChat({
  content, thread_id = null, onEvent, signal,
}: ChatStreamOptions): Promise<void> {
  const resp = await fetch("/channels/web/chat", {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ content, thread_id }),
    signal,
  });
  if (!resp.ok) {
    throw new Error(`chat stream failed: HTTP ${resp.status}`);
  }
  if (resp.body === null) {
    throw new Error("chat stream returned no body");
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    // SSE frames separated by blank line. Iterate complete frames.
    for (;;) {
      const sep = buf.indexOf("\n\n");
      if (sep === -1) break;
      const frame = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      const ev = parseFrame(frame);
      if (ev !== null) onEvent(ev);
    }
  }
  // Flush any trailing complete frame missing the final \n\n.
  if (buf.trim().length > 0) {
    const ev = parseFrame(buf);
    if (ev !== null) onEvent(ev);
  }
}

function parseFrame(frame: string): SseEvent | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event: ")) {
      event = line.slice("event: ".length).trim();
    } else if (line.startsWith("data: ")) {
      dataLines.push(line.slice("data: ".length));
    } else if (line === "data:") {
      dataLines.push("");
    }
  }
  if (dataLines.length === 0) return null;
  return { event, data: dataLines.join("\n") };
}
