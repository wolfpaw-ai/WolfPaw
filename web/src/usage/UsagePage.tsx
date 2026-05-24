// Usage dashboard — renders the JSON the backend builds for /usage
// (the slash-command alternative). Charts are deferred; v1 is tables.

import { useCallback, useEffect, useState } from "react";
import { ApiError, usage as usageApi } from "../api/client";
import type { UsageReport, UsageScope } from "../api/types";
import { formatDollars, formatInt } from "../lib/format";

const SCOPES: { key: UsageScope; label: string }[] = [
  { key: "default", label: "Default" },
  { key: "today", label: "Today" },
  { key: "month", label: "Month + by-agent" },
  { key: "all", label: "All time" },
];

export function UsagePage() {
  const [scope, setScope] = useState<UsageScope>("default");
  const [report, setReport] = useState<UsageReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (next: UsageScope) => {
    setError(null);
    try {
      const r = await usageApi.get(next);
      setReport(r);
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }, []);

  useEffect(() => { load(scope); }, [scope, load]);

  return (
    <main className="page">
      <h1>Usage</h1>
      <div className="tabs">
        {SCOPES.map((s) => (
          <button
            key={s.key}
            className={s.key === scope ? "active" : ""}
            onClick={() => setScope(s.key)}
          >
            {s.label}
          </button>
        ))}
      </div>
      {error && <p className="error">{error}</p>}
      {report === null && !error && <p>Loading…</p>}
      {report?.periods.map((p) => (
        <section key={p.label} className="usage-period">
          <h2>{p.label}</h2>
          {p.models.length === 0 && p.compute_cost_cents === 0 ? (
            <p>(no usage yet)</p>
          ) : (
            <>
              <table className="data">
                <thead>
                  <tr>
                    <th>Model</th>
                    <th>Input tokens</th>
                    <th>Output tokens</th>
                    <th>Input $</th>
                    <th>Output $</th>
                    <th>Cache $</th>
                    <th>Total $</th>
                  </tr>
                </thead>
                <tbody>
                  {p.models.map((m) => (
                    <tr key={m.model}>
                      <td>{m.model}</td>
                      <td>{formatInt(m.input_tokens)}</td>
                      <td>{formatInt(m.output_tokens)}</td>
                      <td>{formatDollars(m.input_cost_cents)}</td>
                      <td>{formatDollars(m.output_cost_cents)}</td>
                      <td>{formatDollars(m.cache_cost_cents)}</td>
                      <td>{formatDollars(m.total_cost_cents)}</td>
                    </tr>
                  ))}
                  {p.compute_cost_cents > 0 && (
                    <tr>
                      <td>sandbox compute</td>
                      <td colSpan={5}></td>
                      <td>{formatDollars(p.compute_cost_cents)}</td>
                    </tr>
                  )}
                  <tr className="totals">
                    <td colSpan={6}>Total</td>
                    <td>{formatDollars(p.total_cost_cents)}</td>
                  </tr>
                </tbody>
              </table>
              {p.by_agent.length > 0 && (
                <>
                  <h3>By agent</h3>
                  <ul className="by-agent">
                    {p.by_agent.map((a) => (
                      <li key={a.agent}>
                        {a.agent}: {formatDollars(a.cost_cents)}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </>
          )}
        </section>
      ))}
    </main>
  );
}
