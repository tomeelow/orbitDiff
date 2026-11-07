import { useState } from "react";
import { type ChangeEvent, eventImageUrl } from "../api";
import type { Load } from "../hooks";
import { alertText, CATEGORY, historyLine, STATUS } from "../lib/changes";
import { formatArea } from "../lib/format";
import { monthKey, monthLabel } from "../lib/months";
import { Segmented } from "./ui";

export type Shown = "alerts" | "all";

/** The changes found here, newest first. "Would alert" lists what the alert rule's settings
would have emailed about, false alarms a later pass took back included; "All" adds what it
held back (seasonal from the start, unwatched, too small). */
export function ChangesCard({
  areaId,
  events,
  shown,
  onShow,
  selected,
  onSelect,
  onRetry,
  onNotReal,
}: {
  areaId: number;
  events: Load<ChangeEvent[]>;
  shown: Shown;
  onShow: (shown: Shown) => void;
  selected: number | null;
  onSelect: (event: ChangeEvent | null) => void;
  onRetry: () => void;
  onNotReal: (event: ChangeEvent) => Promise<void>;
}) {
  const all = events.state === "done" ? events.data : [];
  const alerting = all.filter((e) => e.alerts);
  const list = shown === "alerts" ? alerting : all;
  return (
    <section className="card" aria-labelledby="changes-title">
      <div className="card-head">
        <h2 id="changes-title">Changes</h2>
        {events.state === "done" && all.length > 0 && (
          <Segmented
            label="Changes shown"
            value={shown}
            onChange={onShow}
            options={[
              ["alerts", `Would alert ${alerting.length}`],
              ["all", `All ${all.length}`],
            ]}
          />
        )}
      </div>
      {events.state === "loading" && <p className="muted">Loading changes…</p>}
      {events.state === "error" && (
        <div className="state-inline">
          <p className="error-text">{events.message}</p>
          <button type="button" className="btn" onClick={onRetry}>
            Try again
          </button>
        </div>
      )}
      {events.state === "done" && all.length === 0 && (
        <p className="muted">
          No changes found yet. A change needs a clear pass to show it and the same month a year
          earlier to compare with.
        </p>
      )}
      {events.state === "done" && all.length > 0 && list.length === 0 && (
        <p className="muted">
          None of the {all.length} changes found here would have sent an alert: each was seasonal
          from the start, too small, or in a category not watched.
        </p>
      )}
      {list.length > 0 && (
        <ol className="changes">
          {list.map((e) => (
            <li key={e.id}>
              <ChangeRow
                areaId={areaId}
                event={e}
                open={e.id === selected}
                onToggle={() => onSelect(e.id === selected ? null : e)}
                onNotReal={() => onNotReal(e)}
              />
            </li>
          ))}
        </ol>
      )}
      <p className="muted small">
        Before and after are the monthly composites compared: the same month a year apart.
        Unobserved pixels stay empty.
      </p>
    </section>
  );
}

function ChangeRow({
  areaId,
  event,
  open,
  onToggle,
  onNotReal,
}: {
  areaId: number;
  event: ChangeEvent;
  open: boolean;
  onToggle: () => void;
  onNotReal: () => Promise<void>;
}) {
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function notReal() {
    setSending(true);
    setError(null);
    try {
      await onNotReal();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  }
  const category = CATEGORY[event.category];
  const ndvi =
    event.ndvi_before !== null && event.ndvi_after !== null
      ? `NDVI ${event.ndvi_before.toFixed(2)} → ${event.ndvi_after.toFixed(2)}`
      : null;
  const quiet = event.status === "dismissed" || event.status === "seasonal";
  return (
    <div className={quiet ? "change change-quiet" : "change"}>
      <button type="button" className="change-btn" aria-expanded={open} onClick={onToggle}>
        <span className="change-thumbs">
          <img
            src={eventImageUrl(areaId, event.id, "before")}
            alt={`Before: ${monthLabel(monthKey(event.reference_month))}`}
            loading="lazy"
          />
          <img
            src={eventImageUrl(areaId, event.id, "after")}
            alt={`After: ${monthLabel(monthKey(event.month))}`}
            loading="lazy"
          />
        </span>
        <span className="change-body">
          <span className="change-title">
            <i className="change-dot" style={{ background: category.color }} />
            {category.title}
          </span>
          <span className="change-meta">
            <span className="fig">{monthLabel(monthKey(event.month))}</span>
            <span className="fig">{formatArea(event.area_ha)}</span>
            {ndvi && <span className="fig">{ndvi}</span>}
          </span>
          <span className="change-tags">
            <span className={`tag tag-${event.status}`}>{STATUS[event.status]}</span>
            <span className={event.alerts ? "tag tag-alert" : "tag"}>{alertText(event)}</span>
          </span>
        </span>
      </button>
      {open && (
        <div className="change-detail">
          <ol className="change-history">
            {event.history.map((h, i) => (
              <li key={`${h.status}-${h.at}`}>{historyLine(h, i === 0)}</li>
            ))}
          </ol>
          <p className="muted small">
            {monthLabel(monthKey(event.month))} compared with{" "}
            {monthLabel(monthKey(event.reference_month))}, from{" "}
            <span className="fig">{event.items.length}</span> clear passes.{" "}
            <span className="fig">{event.method_version}</span>
          </p>
          <div className="change-feedback">
            {event.not_real ? (
              <p className="muted small">You marked this as not a real change. Thanks.</p>
            ) : (
              <button type="button" className="btn" onClick={notReal} disabled={sending}>
                Not a real change
              </button>
            )}
            {error && (
              <p className="error-text small" role="alert">
                {error}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
