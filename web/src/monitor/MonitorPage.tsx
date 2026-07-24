// Monitoring dashboard over `model_call_logs`. Three stacked views, ordered
// the way you'd use them when something is wrong: health at the top, then a
// list to scan, then a drill-down into one trace.
//
// A "trace" is everything one inbound prompt caused — triage, planner,
// executor, sub-agents. That's the unit worth reasoning about, which is why
// the list is grouped by trace rather than showing raw calls.

import { Fragment, useCallback, useEffect, useState } from "react";
import { ApiError, monitor as monitorApi } from "../api/client";
import type {
  MonitorCall,
  MonitorSummary,
  MonitorTrace,
  MonitorWindow,
} from "../api/types";
import { formatDateTime, formatDollars, formatInt, shortenId } from "../lib/format";

const WINDOWS: { key: MonitorWindow; label: string }[] = [
  { key: "1h", label: "Last hour" },
  { key: "24h", label: "24 hours" },
  { key: "7d", label: "7 days" },
  { key: "30d", label: "30 days" },
];

function formatMs(ms: number | null): string {
  if (ms === null) return "—";
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

function formatPct(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

/** Pretty-print whatever JSON the payload columns hold. */
function json(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function MonitorPage() {
  const [window, setWindow] = useState<MonitorWindow>("24h");
  const [onlyErrors, setOnlyErrors] = useState(false);
  const [summary, setSummary] = useState<MonitorSummary | null>(null);
  const [traces, setTraces] = useState<MonitorTrace[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [calls, setCalls] = useState<MonitorCall[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (w: MonitorWindow, errorsOnly: boolean) => {
    setError(null);
    try {
      const [s, t] = await Promise.all([
        monitorApi.summary(w),
        monitorApi.traces(w, { errors: errorsOnly }),
      ]);
      setSummary(s);
      setTraces(t);
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }, []);

  useEffect(() => { load(window, onlyErrors); }, [window, onlyErrors, load]);

  // Collapse the open trace when the filters change — its calls belong to a
  // list that may no longer include it.
  useEffect(() => { setSelected(null); setCalls(null); }, [window, onlyErrors]);

  const openTrace = useCallback(async (traceId: string) => {
    if (selected === traceId) {
      setSelected(null);
      setCalls(null);
      return;
    }
    setSelected(traceId);
    setCalls(null);
    try {
      setCalls(await monitorApi.trace(traceId));
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }, [selected]);

  return (
    <main className="page">
      <h1>Monitoring</h1>

      <div className="tabs">
        {WINDOWS.map((w) => (
          <button
            key={w.key}
            className={w.key === window ? "active" : ""}
            onClick={() => setWindow(w.key)}
          >
            {w.label}
          </button>
        ))}
        <label className="monitor-filter">
          <input
            type="checkbox"
            checked={onlyErrors}
            onChange={(e) => setOnlyErrors(e.target.checked)}
          />
          Failures only
        </label>
        <button onClick={() => load(window, onlyErrors)}>Refresh</button>
      </div>

      {error && <p className="error">{error}</p>}
      {summary === null && !error && <p>Loading…</p>}

      {summary && (
        <>
          <section className="monitor-stats">
            <div className="stat">
              <span className="stat-label">Calls</span>
              <span className="stat-value">{formatInt(summary.calls)}</span>
            </div>
            <div className={summary.errors > 0 ? "stat stat-bad" : "stat"}>
              <span className="stat-label">Failures</span>
              <span className="stat-value">
                {formatInt(summary.errors)}
                {summary.calls > 0 && ` (${formatPct(summary.error_rate)})`}
              </span>
            </div>
            <div className="stat">
              <span className="stat-label">p50 latency</span>
              <span className="stat-value">{formatMs(summary.p50_latency_ms)}</span>
            </div>
            <div className="stat">
              <span className="stat-label">p95 latency</span>
              <span className="stat-value">{formatMs(summary.p95_latency_ms)}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Spend</span>
              <span className="stat-value">{formatDollars(summary.cost_cents)}</span>
            </div>
          </section>

          {summary.top_errors.length > 0 && (
            <section>
              <h2>Most common failures</h2>
              <table className="data">
                <thead>
                  <tr>
                    <th>Count</th>
                    <th>Agent</th>
                    <th>Type</th>
                    <th>Message</th>
                    <th>Last seen</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.top_errors.map((e, i) => (
                    <tr key={i}>
                      <td>{formatInt(e.count)}</td>
                      <td>{e.agent}</td>
                      <td>{e.error_type}</td>
                      <td className="monitor-message">{e.error_message}</td>
                      <td>{formatDateTime(e.last_seen)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}

          {summary.by_agent.length > 0 && (
            <section>
              <h2>By agent</h2>
              <table className="data">
                <thead>
                  <tr>
                    <th>Agent</th>
                    <th>Calls</th>
                    <th>Failures</th>
                    <th>p50</th>
                    <th>p95</th>
                    <th>Spend</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.by_agent.map((a) => (
                    <tr key={a.agent} className={a.errors > 0 ? "row-bad" : ""}>
                      <td>{a.agent}</td>
                      <td>{formatInt(a.calls)}</td>
                      <td>{formatInt(a.errors)}</td>
                      <td>{formatMs(a.p50_latency_ms)}</td>
                      <td>{formatMs(a.p95_latency_ms)}</td>
                      <td>{formatDollars(a.cost_cents)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}
        </>
      )}

      <section>
        <h2>Traces</h2>
        {traces !== null && traces.length === 0 && (
          <p>
            {onlyErrors
              ? "(no failures in this window)"
              : "(no model calls in this window)"}
          </p>
        )}
        {traces !== null && traces.length > 0 && (
          <table className="data">
            <thead>
              <tr>
                <th>Started</th>
                <th>Trace</th>
                <th>Agents</th>
                <th>Calls</th>
                <th>Failures</th>
                <th>Duration</th>
                <th>Spend</th>
              </tr>
            </thead>
            <tbody>
              {traces.map((t) => (
                // Keyed Fragment, not <>: each trace renders two sibling rows
                // (summary + expanded detail) and the key has to live on the
                // wrapper.
                <Fragment key={t.trace_id}>
                  <tr
                    className={
                      (t.errors > 0 ? "row-bad " : "") +
                      (selected === t.trace_id ? "row-open" : "")
                    }
                    onClick={() => openTrace(t.trace_id)}
                  >
                    <td>{formatDateTime(t.started_at)}</td>
                    <td><code>{shortenId(t.trace_id)}</code></td>
                    <td>{t.agents.join(", ")}</td>
                    <td>{t.calls}</td>
                    <td>{t.errors || "—"}</td>
                    <td>{formatMs(t.total_latency_ms)}</td>
                    <td>{formatDollars(t.cost_cents)}</td>
                  </tr>
                  {selected === t.trace_id && (
                    <tr className="row-detail">
                      <td colSpan={7}>
                        {calls === null ? (
                          <p>Loading trace…</p>
                        ) : (
                          <TraceDetail calls={calls} />
                        )}
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}

/** The calls inside one trace, in order, each expandable to its payloads. */
function TraceDetail({ calls }: { calls: MonitorCall[] }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <ol className="trace-calls">
      {calls.map((c) => (
        <li
          key={c.id}
          className={c.status === "error" ? "trace-call trace-call-bad" : "trace-call"}
        >
          <button
            className="link-button"
            onClick={() => setOpen(open === c.id ? null : c.id)}
          >
            <strong>{c.agent}</strong> · {c.model}
            {c.attempt > 1 && <> · attempt {c.attempt}</>}
            {" · "}{formatMs(c.latency_ms)}
            {" · "}{formatInt(c.input_tokens)}→{formatInt(c.output_tokens)} tok
            {c.status === "error" && (
              <> · <span className="error">{c.error_type}</span></>
            )}
          </button>
          {c.status === "error" && c.error_message && (
            <p className="error monitor-message">{c.error_message}</p>
          )}
          {open === c.id && (
            <div className="trace-payloads">
              {c.truncated && (
                <p className="notice">
                  Payloads were clipped to the configured size limit.
                </p>
              )}
              <h4>System prompt</h4>
              <pre>{c.system_prompt || "—"}</pre>
              <h4>Messages in</h4>
              <pre>{json(c.request_messages)}</pre>
              <h4>Params</h4>
              <pre>{json(c.request_params)}</pre>
              <h4>Response</h4>
              <pre>{c.response_text || json(c.response_content)}</pre>
              {c.stop_reason && <p>Stop reason: <code>{c.stop_reason}</code></p>}
            </div>
          )}
        </li>
      ))}
    </ol>
  );
}
