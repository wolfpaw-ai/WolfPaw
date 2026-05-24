import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, tasks as tasksApi } from "../api/client";
import type { TaskDetail } from "../api/types";
import { formatDateTime } from "../lib/format";

const TERMINAL_STATUSES = new Set([
  "completed", "failed", "cancelled",
]);

export function TaskDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => {
    if (!id) return;
    tasksApi.get(id).then(setDetail).catch((e) =>
      setError(e instanceof ApiError ? String(e.detail) : String(e)),
    );
  }, [id]);

  useEffect(reload, [reload]);

  async function onCancel() {
    if (!id || !detail) return;
    if (!confirm(`Cancel task ${detail.task.title}?`)) return;
    try {
      await tasksApi.cancel(id);
      reload();
    } catch (e) {
      alert(
        e instanceof ApiError
          ? `Couldn't cancel: ${e.detail}`
          : `Couldn't cancel: ${e}`,
      );
    }
  }

  if (error) return <main className="page"><p className="error">{error}</p></main>;
  if (detail === null) return <main className="page"><p>Loading…</p></main>;

  const t = detail.task;
  const cancellable = !TERMINAL_STATUSES.has(t.status);

  return (
    <main className="page">
      <p><Link to="/tasks">← All tasks</Link></p>
      <h1>{t.title}</h1>
      <dl className="metadata">
        <dt>Status</dt>
        <dd><span className={`status status-${t.status}`}>{t.status}</span></dd>
        <dt>Created</dt>
        <dd>{formatDateTime(t.created_at)}</dd>
        <dt>Last active</dt>
        <dd>{formatDateTime(t.last_active_at)}</dd>
        <dt>Spent</dt>
        <dd>{(t.spent_cents / 100).toFixed(2)} ¢</dd>
        {t.blocking_reason && (
          <>
            <dt>Blocking</dt>
            <dd>{t.blocking_reason}</dd>
          </>
        )}
      </dl>
      {cancellable && (
        <button onClick={onCancel} className="danger">
          Cancel task
        </button>
      )}
      <h2>Events</h2>
      {detail.events.length === 0 && <p>No events recorded.</p>}
      <ol className="events">
        {detail.events.map((e) => (
          <li key={e.id}>
            <code>{e.event_type}</code>
            <span className="event-time">{formatDateTime(e.created_at)}</span>
            <pre>{JSON.stringify(e.content, null, 2)}</pre>
          </li>
        ))}
      </ol>
    </main>
  );
}
