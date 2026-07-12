// Chat surface — the most heavily-used screen. Streams SSE events as
// they arrive and gives the user an inline reply box for `ask_user`
// pauses.
//
// History: on mount we load the newest page of the user's most-recent
// thread (so a refresh shows the ongoing conversation, not a blank
// canvas) and remember its thread_id. Scrolling to the top pages
// backwards through older messages via keyset cursor, prepending them
// while holding the viewport in place.

import {
  FormEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { askUser, chat } from "../api/client";
import type { ChatMessage } from "../api/types";
import { streamChat, SseEvent } from "./sseClient";

// Render user-supplied links in a new tab, never leaking the opener.
const MD_COMPONENTS = {
  a: (props: JSX.IntrinsicElements["a"]) => (
    <a {...props} target="_blank" rel="noopener noreferrer" />
  ),
};

interface Turn {
  id: number;
  role: "user" | "assistant" | "system";
  text: string;
  events: TimelineEntry[];
  // Wall-clock time the assistant turn finished, e.g. "10:42:23 p.m."
  // Only set on assistant turns.
  time?: string;
}

// Format a clock time like "10:42:23 p.m." — 12-hour, seconds, lowercase
// meridiem with periods. Hour is not zero-padded; minutes/seconds are.
function formatClockTime(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  const meridiem = d.getHours() < 12 ? "a.m." : "p.m.";
  const hour12 = d.getHours() % 12 || 12;
  return `${hour12}:${pad(d.getMinutes())}:${pad(d.getSeconds())} ${meridiem}`;
}

interface TimelineEntry {
  kind: string;
  data: string;
}

interface PendingQuestion {
  question_id: string;
  question: string;
}

export function ChatPage() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [pendingQuestion, setPendingQuestion] =
    useState<PendingQuestion | null>(null);
  const [answerDraft, setAnswerDraft] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const turnCounter = useRef(0);
  // History turns get negative ids counting down, so they never collide
  // with the positive ids `send()` mints for live turns.
  const historyCounter = useRef(-1);
  // Keyset cursor: the oldest message currently loaded. Fed back to the
  // history endpoint to page further back.
  const oldestCursor = useRef<{ before: string; beforeId: string } | null>(
    null,
  );
  // Set right before a prepend so the layout effect restores scroll
  // position instead of yanking to the bottom.
  const preserveScroll = useRef<{ height: number; top: number } | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  useLayoutEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const preserve = preserveScroll.current;
    if (preserve) {
      // Older messages were just prepended — keep whatever the user was
      // looking at in place by offsetting for the newly-added height.
      el.scrollTop = el.scrollHeight - preserve.height + preserve.top;
      preserveScroll.current = null;
      return;
    }
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [turns]);

  // Map a persisted message row to a display turn. Historical turns carry
  // no timeline events (tool/step events aren't persisted); assistant
  // rows get a completion time from their stored timestamp.
  const msgToTurn = useCallback((m: ChatMessage): Turn => {
    return {
      id: historyCounter.current--,
      role: m.role,
      text: m.content,
      events: [],
      time:
        m.role === "assistant"
          ? formatClockTime(new Date(m.created_at))
          : undefined,
    };
  }, []);

  // On mount: load the newest page of the user's most-recent thread.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const page = await chat.history({ limit: 30 });
        if (cancelled) return;
        if (page.thread_id) setThreadId(page.thread_id);
        if (page.messages.length > 0) {
          setTurns(page.messages.map(msgToTurn));
          const oldest = page.messages[0];
          oldestCursor.current = {
            before: oldest.created_at,
            beforeId: oldest.id,
          };
        }
        setHasMore(page.has_more);
      } catch {
        // Blank-canvas fallback — the backend still continues the thread
        // on the next send.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [msgToTurn]);

  const loadOlder = useCallback(async () => {
    const el = scrollerRef.current;
    const cursor = oldestCursor.current;
    if (!el || loadingHistory || !hasMore || !cursor) return;
    setLoadingHistory(true);
    preserveScroll.current = { height: el.scrollHeight, top: el.scrollTop };
    try {
      const page = await chat.history({
        threadId,
        before: cursor.before,
        beforeId: cursor.beforeId,
        limit: 30,
      });
      if (page.messages.length > 0) {
        const older = page.messages.map(msgToTurn);
        setTurns((prev) => [...older, ...prev]);
        const oldest = page.messages[0];
        oldestCursor.current = {
          before: oldest.created_at,
          beforeId: oldest.id,
        };
      }
      setHasMore(page.has_more);
    } catch {
      preserveScroll.current = null;
    } finally {
      setLoadingHistory(false);
    }
  }, [loadingHistory, hasMore, threadId, msgToTurn]);

  const onScroll = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    if (el.scrollTop < 80 && hasMore && !loadingHistory) {
      loadOlder();
    }
  }, [hasMore, loadingHistory, loadOlder]);

  // Keep the message textarea focused so the user can type → enter →
  // type → enter without clicking back into the field. Fires on mount
  // (busy=false, pendingQuestion=null), after a turn completes (busy
  // → false), and after an ask_user reply (pendingQuestion → null).
  useEffect(() => {
    if (!busy && !pendingQuestion) {
      inputRef.current?.focus();
    }
  }, [busy, pendingQuestion]);

  const send = useCallback(
    async (text: string) => {
      if (!text.trim() || busy) return;
      const id = turnCounter.current++;
      setTurns((prev) => [
        ...prev,
        { id, role: "user", text, events: [] },
      ]);
      const assistantId = turnCounter.current++;
      setTurns((prev) => [
        ...prev,
        { id: assistantId, role: "assistant", text: "", events: [] },
      ]);
      setBusy(true);
      try {
        await streamChat({
          content: text,
          thread_id: threadId,
          onEvent: (ev: SseEvent) => handleEvent(ev, assistantId),
        });
      } catch (e) {
        setTurns((prev) =>
          prev.map((t) =>
            t.id === assistantId
              ? {
                  ...t,
                  text:
                    t.text ||
                    `(stream failed: ${e instanceof Error ? e.message : String(e)})`,
                }
              : t,
          ),
        );
      } finally {
        setBusy(false);
        // Stamp the assistant turn with its completion time.
        const finishedAt = formatClockTime(new Date());
        setTurns((prev) =>
          prev.map((t) =>
            t.id === assistantId ? { ...t, time: finishedAt } : t,
          ),
        );
      }
    },
    [busy, threadId],
  );

  function handleEvent(ev: SseEvent, assistantId: number) {
    if (ev.event === "thread") {
      setThreadId(ev.data);
      return;
    }
    if (ev.event === "reset") {
      // `/reset` command — drop the thread_id so the next send creates
      // a fresh thread on the server.
      setThreadId(null);
      return;
    }
    if (ev.event === "delta") {
      setTurns((prev) =>
        prev.map((t) =>
          t.id === assistantId ? { ...t, text: t.text + ev.data } : t,
        ),
      );
      return;
    }
    if (ev.event === "command") {
      // Slash command result — show as a system row, not the assistant.
      setTurns((prev) =>
        prev.map((t) =>
          t.id === assistantId ? { ...t, text: ev.data } : t,
        ),
      );
      return;
    }
    if (ev.event === "ask_user") {
      try {
        const parsed = JSON.parse(ev.data) as PendingQuestion;
        setPendingQuestion(parsed);
      } catch {
        // Fall through to timeline-only.
      }
    }
    if (ev.event === "error") {
      setTurns((prev) =>
        prev.map((t) =>
          t.id === assistantId
            ? { ...t, text: t.text + `\n\n[error] ${ev.data}` }
            : t,
        ),
      );
      return;
    }
    // Any other event (triage / plan / tool / step.* / task / score) → timeline entry.
    setTurns((prev) =>
      prev.map((t) =>
        t.id === assistantId
          ? {
              ...t,
              events: [...t.events, { kind: ev.event, data: ev.data }],
            }
          : t,
      ),
    );
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const text = draft;
    setDraft("");
    await send(text);
  }

  async function onSubmitAnswer(e: FormEvent) {
    e.preventDefault();
    if (!pendingQuestion || !answerDraft.trim()) return;
    try {
      await askUser.submit(pendingQuestion.question_id, answerDraft);
      setPendingQuestion(null);
      setAnswerDraft("");
    } catch (e) {
      alert(`Couldn't submit answer: ${e instanceof Error ? e.message : e}`);
    }
  }

  return (
    <div className="chat">
      <div className="chat-scroller" ref={scrollerRef} onScroll={onScroll}>
        {loadingHistory && (
          <div className="chat-loading-older">Loading earlier messages…</div>
        )}
        {turns.length === 0 && (
          <div className="chat-empty">
            <p>Talk to Wolfpaw. Try <code>/help</code> to see commands.</p>
          </div>
        )}
        {turns.map((t) => (
          <article key={t.id} className={`turn turn-${t.role}`}>
            <header className="turn-role">
              <span>{t.role}</span>
              {t.time && <span className="turn-time">{t.time}</span>}
            </header>
            {t.events.length > 0 && (
              <details className="turn-events" open>
                <summary>{t.events.length} event(s)</summary>
                <ul>
                  {t.events.map((e, i) => (
                    <li key={i}>
                      <code>{e.kind}</code> {e.data}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            {t.role === "user" ? (
              // User text is plain — preserve their line breaks, but don't
              // interpret stray markdown characters they typed.
              <div className="turn-text turn-plain">{t.text}</div>
            ) : (
              <div className="turn-text turn-md">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={MD_COMPONENTS}
                >
                  {t.text}
                </ReactMarkdown>
              </div>
            )}
          </article>
        ))}
      </div>

      {pendingQuestion && (
        <form className="chat-ask-user" onSubmit={onSubmitAnswer}>
          <p>
            <strong>Wolfpaw asks:</strong> {pendingQuestion.question}
          </p>
          <input
            value={answerDraft}
            onChange={(e) => setAnswerDraft(e.target.value)}
            autoFocus
            placeholder="Your answer…"
          />
          <button type="submit">Reply</button>
        </form>
      )}

      <form className="chat-input" onSubmit={onSubmit}>
        <textarea
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={busy ? "Wolfpaw is working…" : "Type a message…"}
          disabled={busy}
          rows={2}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              onSubmit(e as unknown as FormEvent);
            }
          }}
        />
        <button type="submit" disabled={busy || !draft.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}
