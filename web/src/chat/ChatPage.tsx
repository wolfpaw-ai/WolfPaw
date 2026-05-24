// Chat surface — the most heavily-used screen. Maintains a single
// in-memory history (per page load), streams SSE events as they
// arrive, and gives the user an inline reply box for `ask_user`
// pauses.
//
// Persisted state: the backend stores the user/assistant turns; the
// `thread` event from the SSE stream gives us the thread_id to
// remember across turns. We don't load thread history on mount in v1
// — refreshing the page starts a visually blank canvas but the
// backend continues the same thread.

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { askUser } from "../api/client";
import { streamChat, SseEvent } from "./sseClient";

interface Turn {
  id: number;
  role: "user" | "assistant" | "system";
  text: string;
  events: TimelineEntry[];
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
  const turnCounter = useRef(0);
  const scrollerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    scrollerRef.current?.scrollTo({
      top: scrollerRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [turns]);

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
      }
    },
    [busy, threadId],
  );

  function handleEvent(ev: SseEvent, assistantId: number) {
    if (ev.event === "thread") {
      setThreadId(ev.data);
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
      <div className="chat-scroller" ref={scrollerRef}>
        {turns.length === 0 && (
          <div className="chat-empty">
            <p>Talk to Wolfpaw. Try <code>/help</code> to see commands.</p>
          </div>
        )}
        {turns.map((t) => (
          <article key={t.id} className={`turn turn-${t.role}`}>
            <header className="turn-role">{t.role}</header>
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
            <pre className="turn-text">{t.text}</pre>
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
