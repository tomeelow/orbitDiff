import { useEffect } from "react";
import { api, type Stage } from "../api";
import { useLoad } from "../hooks";
import { formatArea } from "../lib/format";
import { ErrorState, LoadingState } from "./ui";

const STAGE: Record<Stage, string> = {
  queued: "waiting to start",
  reading: "reading passes",
  masking: "masking clouds",
  compositing: "building composites",
  ready: "ready",
  failed: "backfill failed",
};

export function AreasPage() {
  const [areas, reload] = useLoad(api.areas, []);
  useEffect(() => {
    document.title = "Areas — Orbit Diff";
  }, []);

  if (areas.state === "loading") return <LoadingState label="Loading your areas" />;
  if (areas.state === "error") return <ErrorState message={areas.message} onRetry={reload} />;

  return (
    <main className="screen screen-narrow">
      <header className="screen-head">
        <h1>Areas</h1>
      </header>
      {areas.data.length === 0 ? (
        <div className="empty-state">
          <h2>No areas yet</h2>
          <p>
            Draw a polygon around a place you care about. Orbit Diff reads only that window of every
            Sentinel-2 pass over it and builds a cloud-free picture for each month.
          </p>
          <a className="btn btn-primary" href="#/new">
            Draw an area
          </a>
        </div>
      ) : (
        <ul className="area-list">
          {areas.data.map((area) => (
            <li key={area.id}>
              <a href={`#/areas/${area.id}`}>
                <span className="area-name">{area.name}</span>
                <span className="fig muted">{formatArea(area.area_ha)}</span>
                <span
                  className={
                    area.stage === "failed" ? "error-text" : area.stage === "ready" ? "muted" : ""
                  }
                >
                  {STAGE[area.stage]}
                </span>
              </a>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
