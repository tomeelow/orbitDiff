import { useEffect } from "react";
import { type AdminSummary, api } from "../api";
import { useLoad } from "../hooks";
import { CATEGORY } from "../lib/changes";
import { formatBytes, formatPercent, formatUtc } from "../lib/format";
import { ErrorState, LoadingState } from "./ui";

/** Is the pipeline healthy, and can people still trust its alerts? Aggregate only: area ids
at most, never names, places or addresses. */
export function AdminPage({ admin }: { admin: boolean }) {
  useEffect(() => {
    document.title = "Admin — Orbit Diff";
  }, []);
  if (!admin) {
    return (
      <main className="screen screen-narrow">
        <div className="empty-state">
          <h2>Admins only</h2>
          <p>This page shows how the pipeline is doing across every area.</p>
        </div>
      </main>
    );
  }
  return <Summary />;
}

function Summary() {
  const [summary, reload] = useLoad(api.adminSummary, []);
  if (summary.state === "loading") return <LoadingState label="Loading the admin view" />;
  if (summary.state === "error") return <ErrorState message={summary.message} onRetry={reload} />;
  const data = summary.data;
  return (
    <main className="screen screen-narrow admin">
      <header className="screen-head">
        <div>
          <h1>Admin</h1>
          <p className="meta">
            <span>
              Production method <span className="fig">{data.production_version}</span>
            </span>
          </p>
        </div>
        <div className="tools">
          <button type="button" className="btn" onClick={reload}>
            Refresh
          </button>
        </div>
      </header>
      <PrecisionSection data={data} />
      <HealthSection data={data} />
      <FailuresSection data={data} />
    </main>
  );
}

function PrecisionSection({ data }: { data: AdminSummary }) {
  return (
    <section className="admin-section" aria-labelledby="precision-title">
      <h2 id="precision-title">Alert precision, last {data.precision_days} days</h2>
      <p className="muted small">
        From “Not a real change” on alerts that were sent. People only see what alerted, so this
        says nothing about missed changes: the evaluation set measures those. A category under{" "}
        {formatPercent(data.min_precision)} with enough alerts is flagged for review; nothing
        changes on its own.
      </p>
      {data.precision.length === 0 ? (
        <p className="muted">No alerts sent in the last {data.precision_days} days.</p>
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Method</th>
                <th scope="col">Category</th>
                <th scope="col" className="num">
                  Alerts
                </th>
                <th scope="col" className="num">
                  Not real
                </th>
                <th scope="col" className="num">
                  Precision
                </th>
                <th scope="col">Review</th>
              </tr>
            </thead>
            <tbody>
              {data.precision.map((row) => (
                <tr key={`${row.method_version}-${row.category}`}>
                  <td className="fig">{row.method_version}</td>
                  <td>
                    <i
                      className="change-dot"
                      style={{ background: CATEGORY[row.category].color }}
                    />
                    {CATEGORY[row.category].title}
                  </td>
                  <td className="num fig">{row.alerts}</td>
                  <td className="num fig">{row.not_real}</td>
                  <td className="num fig">{formatPercent(row.precision)}</td>
                  <td>
                    {row.flagged ? (
                      <span className="tag tag-pending">⚠ Review</span>
                    ) : (
                      <span className="muted small">
                        {row.alerts < data.min_alerts ? "Too few to say" : "OK"}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function HealthSection({ data }: { data: AdminSummary }) {
  const h = data.health;
  const rows: [string, string, boolean?][] = [
    ["Runs", `${h.runs} (${h.failed_runs} failed)`, h.failed_runs > 0],
    ["Passes read", String(h.passes_read)],
    ["Passes left to retry", String(h.passes_failed), h.passes_failed > 0],
    ["Read from the fallback catalog", String(h.fallback_passes), h.fallback_passes > 0],
    ["Data read", formatBytes(h.bytes_read)],
    ["Median run", h.median_run_minutes === null ? "—" : `${h.median_run_minutes} min`],
    ["Emails sent", String(h.emails_sent)],
    ["Emails failed", String(h.emails_failed), h.emails_failed > 0],
    ["Emails that may be lost", String(h.emails_stuck), h.emails_stuck > 0],
    ["Median pass to alert", h.median_alert_hours === null ? "—" : `${h.median_alert_hours} h`],
    ["“Not a real change”", String(h.feedback)],
  ];
  return (
    <section className="admin-section" aria-labelledby="health-title">
      <h2 id="health-title">Pipeline, last {data.health_days} days</h2>
      <dl className="rows">
        {rows.map(([label, value, warn]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd className={warn ? "fig warn-text" : "fig"}>{value}</dd>
          </div>
        ))}
      </dl>
      <p className="muted small">
        Emails that may be lost were claimed but never finished: the process died while sending.
        They are never retried, so nobody gets one twice.
      </p>
    </section>
  );
}

function FailuresSection({ data }: { data: AdminSummary }) {
  return (
    <section className="admin-section" aria-labelledby="failures-title">
      <h2 id="failures-title">Recent failed runs</h2>
      {data.failures.length === 0 ? (
        <p className="muted">None in the last {data.health_days} days.</p>
      ) : (
        <ul className="failures">
          {data.failures.map((f) => (
            <li key={`${f.aoi_id}-${f.finished_utc}`}>
              <span className="fig muted">{formatUtc(f.finished_utc)} UTC</span>
              <span className="fig">area {f.aoi_id}</span>
              <span className="error-text">{f.error}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
