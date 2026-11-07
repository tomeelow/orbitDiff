import type { AoiDetail, Progress, Scene } from "../api";
import type { Load } from "../hooks";
import { formatArea, formatBytes, formatPercent, formatUtc } from "../lib/format";
import { monthKey, monthLabel } from "../lib/months";
import type { TimelineMonth } from "./Timeline";

export function StatsCard({
  area,
  onUpdate,
  updating,
}: {
  area: AoiDetail;
  onUpdate?: () => void;
  updating?: boolean;
}) {
  const first = area.first_month ? monthLabel(monthKey(area.first_month)) : null;
  const last = area.last_month ? monthLabel(monthKey(area.last_month)) : null;
  return (
    <section className="card" aria-labelledby="stats-title">
      <h2 id="stats-title">This area</h2>
      <dl className="rows">
        <div>
          <dt>Area</dt>
          <dd className="fig">{formatArea(area.area_ha)}</dd>
        </div>
        <div>
          <dt>Archive</dt>
          <dd>
            {first && last
              ? `${first} to ${last}`
              : `from ${monthLabel(monthKey(area.archive_start))}`}
          </dd>
        </div>
        <div>
          <dt>Passes indexed</dt>
          <dd className="fig">{area.scenes}</dd>
        </div>
        <div>
          <dt>Clear enough to use</dt>
          <dd>
            <span className="fig">{area.clear_scenes}</span>
            {area.scenes > 0 && (
              <span className="muted"> ({formatPercent(area.clear_scenes / area.scenes)})</span>
            )}
          </dd>
        </div>
      </dl>
      {area.scenes > 0 ? (
        <div className="data-read">
          <p className="data-read-figure fig">{formatBytes(area.bytes_read)}</p>
          <p className="muted">
            read from orbit for this area. Downloading the same passes whole would take{" "}
            <span className="fig">{formatBytes(area.whole_files_bytes)}</span>.
          </p>
        </div>
      ) : (
        <p className="muted">Nothing read from orbit yet.</p>
      )}
      {onUpdate && (
        <button type="button" className="btn" onClick={onUpdate} disabled={updating}>
          {updating ? "Checking…" : "Check for new passes"}
        </button>
      )}
    </section>
  );
}

const STATUS: Record<Scene["read_status"], string> = {
  full: "read",
  scl: "cloudy over the area",
  tile_cloud: "tile too cloudy, skipped",
};

const platform = (name: string) => name.replace(/^sentinel-/i, "").toUpperCase();

export function MonthCard({
  month,
  passes,
  selected,
  onSelect,
  onRetry,
}: {
  month: TimelineMonth;
  passes: Load<Scene[]>;
  selected: string | null;
  onSelect: (pass: Scene) => void;
  onRetry: () => void;
}) {
  return (
    <section className="card" aria-labelledby="month-title">
      <h2 id="month-title">
        <b className="cursor-b">B</b> {monthLabel(month.key)}
      </h2>
      <dl className="rows">
        <div>
          <dt>Passes</dt>
          <dd>
            <span className="fig">{month.clear_scenes}</span> clear of{" "}
            <span className="fig">{month.passes}</span>
          </dd>
        </div>
        <div>
          <dt>Area seen clear</dt>
          <dd className="fig">{formatPercent(month.observed_frac)}</dd>
        </div>
        {month.filled_frac > 0 && (
          <div>
            <dt>Filled for display</dt>
            <dd className="fig warn-text">{formatPercent(month.filled_frac)}</dd>
          </div>
        )}
      </dl>
      <h3 className="card-sub">Passes this month</h3>
      {passes.state === "loading" && <p className="muted">Loading passes…</p>}
      {passes.state === "error" && (
        <div className="state-inline">
          <p className="error-text">{passes.message}</p>
          <button type="button" className="btn" onClick={onRetry}>
            Try again
          </button>
        </div>
      )}
      {passes.state === "done" && passes.data.length === 0 && (
        <p className="muted">No passes over this area this month.</p>
      )}
      {passes.state === "done" && passes.data.length > 0 && (
        <ul className="passes">
          {passes.data.map((pass) => {
            const viewable = pass.images !== null;
            const body = (
              <>
                <span className="fig">{formatUtc(pass.acquired_utc)}</span>
                <span className="muted">{platform(pass.platform)}</span>
                <span className="pass-status">
                  {pass.offset_suspect
                    ? "left out: offset flag contradicts pixels"
                    : pass.read_status === "full" && pass.aoi_clear_frac !== null
                      ? `${formatPercent(pass.aoi_clear_frac)} clear`
                      : STATUS[pass.read_status]}
                </span>
              </>
            );
            return (
              <li key={pass.item_id}>
                {viewable ? (
                  <button
                    type="button"
                    className="pass"
                    aria-pressed={pass.item_id === selected}
                    title={pass.item_id}
                    onClick={() => onSelect(pass)}
                  >
                    {body}
                  </button>
                ) : (
                  <div className="pass pass-static" title={pass.item_id}>
                    {body}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
      <p className="muted small">Times in UTC. Passes with bands read can be shown on their own.</p>
    </section>
  );
}

const STAGE_TEXT: Record<Progress["stage"], string> = {
  queued: "Waiting for a worker",
  reading: "Reading passes over the area",
  masking: "Masking clouds and shadows",
  compositing: "Building monthly composites",
  detecting: "Finding changes",
  ready: "Ready",
  failed: "Backfill failed",
};

export function BackfillCard({
  progress,
  onRetry,
  update = false,
}: {
  progress: Progress | null;
  onRetry: () => void;
  update?: boolean; // the archive exists; this brings it up to date
}) {
  const stage = progress?.stage ?? "queued";
  const fraction = progress && progress.total > 0 ? progress.done / progress.total : null;
  return (
    <section className="card" aria-labelledby="backfill-title" aria-live="polite">
      <h2 id="backfill-title">
        {stage === "failed"
          ? update
            ? "Update failed"
            : "Backfill failed"
          : update
            ? "Updating the archive"
            : "Building the archive"}
      </h2>
      {stage === "failed" ? (
        <div className="state-inline">
          <p className="error-text">{progress?.error ?? "The backfill stopped."}</p>
          <button type="button" className="btn" onClick={onRetry}>
            {update ? "Retry update" : "Retry backfill"}
          </button>
        </div>
      ) : (
        <>
          <p>{STAGE_TEXT[stage]}</p>
          <div
            className="progress"
            role="progressbar"
            aria-label={STAGE_TEXT[stage]}
            aria-valuemin={0}
            aria-valuemax={progress?.total || undefined}
            aria-valuenow={fraction === null ? undefined : progress?.done}
            data-indeterminate={fraction === null || undefined}
          >
            <span style={{ width: fraction === null ? undefined : `${fraction * 100}%` }} />
          </div>
          {progress && progress.total > 0 && (
            <p className="muted small">
              <span className="fig">{progress.done}</span> of{" "}
              <span className="fig">{progress.total}</span>
            </p>
          )}
          <p className="muted small">
            {update
              ? "The archive below stays as it was until the update finishes."
              : "Only the area's window of each pass is read. The page updates when the archive is ready."}
          </p>
        </>
      )}
    </section>
  );
}
