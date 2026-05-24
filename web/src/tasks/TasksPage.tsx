import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { tasks as tasksApi } from "../api/client";
import type { Task } from "../api/types";
import { formatDateTime } from "../lib/format";

export function TasksPage() {
  const [items, setItems] = useState<Task[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    tasksApi.list().then(setItems).catch((e) => setError(String(e)));
  }, []);

  if (error) return <main><p className="error">{error}</p></main>;
  if (items === null) return <main><p>Loading…</p></main>;

  return (
    <main className="page">
      <h1>Tasks</h1>
      {items.length === 0 && <p>No tasks yet.</p>}
      <table className="data">
        <thead>
          <tr>
            <th>Status</th>
            <th>Title</th>
            <th>Last active</th>
          </tr>
        </thead>
        <tbody>
          {items.map((t) => (
            <tr key={t.id}>
              <td><span className={`status status-${t.status}`}>{t.status}</span></td>
              <td><Link to={`/tasks/${t.id}`}>{t.title}</Link></td>
              <td>{formatDateTime(t.last_active_at ?? t.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </main>
  );
}
