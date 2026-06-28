import { useEffect, useState } from "react";
import { schedules as schedulesApi } from "../api/client";
import type { Schedule } from "../api/types";
import { formatDateTime } from "../lib/format";

export function SchedulesPage() {
  const [items, setItems] = useState<Schedule[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    schedulesApi.list().then(setItems).catch((e) => setError(String(e)));
  }, []);

  if (error) return <main><p className="error">{error}</p></main>;
  if (items === null) return <main><p>Loading…</p></main>;

  return (
    <main className="page">
      <h1>Scheduled</h1>
      {items.length === 0 && (
        <p>Nothing scheduled. Ask in chat to set up a reminder or recurring task.</p>
      )}
      {items.length > 0 && (
        <table className="data">
          <thead>
            <tr>
              <th>Status</th>
              <th>Title</th>
              <th>Cadence</th>
              <th>Next run</th>
              <th>Runs</th>
            </tr>
          </thead>
          <tbody>
            {items.map((s) => (
              <tr key={s.id}>
                <td><span className={`status status-${s.status}`}>{s.status}</span></td>
                <td title={s.instruction}>{s.title ?? s.instruction}</td>
                <td>{s.cadence}</td>
                <td>{formatDateTime(s.next_run_at)}</td>
                <td>{s.run_count}{s.max_runs != null ? ` / ${s.max_runs}` : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}
