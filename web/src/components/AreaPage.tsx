import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { type AoiDetail, api, type ChangeEvent, type Progress, type Scene } from "../api";
import { type Theme, useLoad } from "../hooks";
import { CATEGORY } from "../lib/changes";
import { formatArea } from "../lib/format";
import { compareNote, defaultPair, monthKey, monthLabel } from "../lib/months";
import { archiveYears } from "../lib/rules";
import { AlertsCard } from "./Alerts";
import { ChangesCard, type Shown } from "./Changes";
import { CompareMap, type Layer } from "./CompareMap";
import { LabelsCard, useLabelling } from "./Labels";
import { BackfillCard, MonthCard, StatsCard } from "./Panels";
import { ndviRange, Timeline, type TimelineMonth } from "./Timeline";
import { ErrorState, LoadingState, Segmented } from "./ui";

const POLL_MS = 2000;
const FRAME_MS = 1000; // timelapse: one month per second

export function AreaPage({
  id,
  change,
  theme,
  admin,
}: {
  id: number;
  change: number | null; // a change to open on, from an alert email's link
  theme: Theme;
  admin: boolean; // may label the area for the evaluation set
}) {
  const [detail, reload] = useLoad(() => api.area(id), [id]);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [updating, setUpdating] = useState(false);
  const area = detail.state === "done" ? detail.data : null;
  const running = area !== null && area.stage !== "ready"; // a backfill or an update

  useEffect(() => {
    if (area) document.title = `${area.name} — Orbit Diff`;
  }, [area]);

  // Poll the backfill until it settles, then reload the area with its archive.
  useEffect(() => {
    if (!running) return;
    let live = true;
    const tick = () =>
      api.progress(id).then(
        (p) => {
          if (!live) return;
          setProgress(p);
          if (p.stage === "ready") reload();
        },
        () => {},
      );
    tick();
    const timer = window.setInterval(tick, POLL_MS);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [running, id, reload]);

  const startBackfill = useCallback(async () => {
    setUpdating(true);
    try {
      setProgress(await api.backfill(id));
      reload();
    } catch (error) {
      setProgress({
        stage: "failed",
        done: 0,
        total: 0,
        error: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setUpdating(false);
    }
  }, [id, reload]);

  if (detail.state === "loading") return <LoadingState label="Loading the area" />;
  if (detail.state === "error") return <ErrorState message={detail.message} onRetry={reload} />;
  const data = detail.data;
  const status = running
    ? (progress ?? { stage: data.stage, done: 0, total: 0, error: null })
    : null;
  // Until the first months exist there is nothing to browse; after that, updates run
  // alongside the archive.
  if (status && data.first_month === null) {
    return (
      <AreaLayout area={data}>
        <div className="stage">
          <CompareMap
            area={data}
            a={null}
            b={null}
            layer="true_color"
            pass={null}
            next={null}
            theme={theme}
          />
        </div>
        <aside className="panel">
          <BackfillCard progress={status} onRetry={startBackfill} />
          <StatsCard area={data} />
        </aside>
      </AreaLayout>
    );
  }
  return (
    <Archive
      area={data}
      change={change}
      theme={theme}
      admin={admin}
      onUpdate={startBackfill}
      updating={updating}
      status={status}
    />
  );
}

function AreaLayout({
  area,
  controls,
  children,
}: {
  area: AoiDetail;
  controls?: React.ReactNode;
  children: React.ReactNode;
}) {
  const first = area.first_month ? monthLabel(monthKey(area.first_month)) : null;
  const last = area.last_month ? monthLabel(monthKey(area.last_month)) : null;
  return (
    <main className="screen">
      <header className="screen-head">
        <div>
          <h1>{area.name}</h1>
          <p className="meta">
            <span className="fig">{formatArea(area.area_ha)}</span>
            {first && last && (
              <span>
                {first} to {last}
              </span>
            )}
            <span>Sentinel-2 L2A</span>
          </p>
        </div>
        {controls && <div className="tools">{controls}</div>}
      </header>
      {children}
    </main>
  );
}

type Slot = "A" | "B";

function Archive({
  area,
  change,
  theme,
  admin,
  onUpdate,
  updating,
  status,
}: {
  area: AoiDetail;
  change: number | null;
  theme: Theme;
  admin: boolean;
  onUpdate: () => void;
  updating: boolean;
  status: Progress | null; // an update in progress
}) {
  const [archive, reloadArchive] = useLoad(
    () => Promise.all([api.timeline(area.id), api.ndvi(area.id)]),
    [area.id, area.last_month, area.scenes, area.clear_scenes], // what an update changes
  );
  const months: TimelineMonth[] = useMemo(() => {
    if (archive.state !== "done") return [];
    const [timeline, ndvi] = archive.data;
    const byMonth = new Map(ndvi.map((p) => [p.month, p.ndvi]));
    return timeline.map((m) => ({
      ...m,
      key: monthKey(m.month),
      ndvi: byMonth.get(m.month) ?? null,
    }));
  }, [archive]);

  const [pair, setPair] = useState<{ a: string; b: string } | null>(null);
  const [slot, setSlot] = useState<Slot>("B");
  const [layer, setLayer] = useState<Layer>("true_color");
  const [source, setSource] = useState<"composite" | "pass">("composite");
  const [passId, setPassId] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);

  const [events, reloadEvents] = useLoad(
    () => api.events(area.id),
    [area.id, area.last_month, area.scenes, area.clear_scenes],
  );
  const [shown, setShown] = useState<Shown>("alerts");
  const [selected, setSelected] = useState<number | null>(null);
  const changes = useMemo(() => {
    const all = events.state === "done" ? events.data : [];
    return shown === "alerts" ? all.filter((e) => e.alerts) : all;
  }, [events, shown]);
  // Labelling shows the labels in place of the detected changes, so they don't copy them.
  const labelling = useLabelling(area.id, admin);
  const labels = labelling.on ? labelling.draft?.changes : undefined;

  // Months with a change on show, in the colour of the latest one's category.
  const marks = useMemo(
    () =>
      new Map(
        labels
          ? labels.map((c) => [c.month, CATEGORY[c.category].color])
          : [...changes].reverse().map((e) => [monthKey(e.month), CATEGORY[e.category].color]),
      ),
    [changes, labels],
  );

  // Default cursors once the months arrive, and again if they no longer exist.
  useEffect(() => {
    const keys = new Set(months.map((m) => m.key));
    if (pair && keys.has(pair.a) && keys.has(pair.b)) return;
    setPair(
      defaultPair(
        months.map((m) => ({
          key: m.key,
          observedFrac: m.observed_frac,
          filledFrac: m.filled_frac,
        })),
      ),
    );
  }, [months, pair]);

  const index = useMemo(() => new Map(months.map((m, i) => [m.key, i])), [months]);
  const a = pair ? months[index.get(pair.a) ?? -1] : undefined;
  const b = pair ? months[index.get(pair.b) ?? -1] : undefined;

  const [passes, reloadPasses] = useLoad<Scene[]>(
    () => (b ? api.scenes(area.id, b.key) : Promise.resolve([])),
    [area.id, b?.key],
  );
  const viewable = passes.state === "done" ? passes.data.filter((p) => p.images) : [];
  const chosenPass =
    source === "pass"
      ? (viewable.find((p) => p.item_id === passId) ??
        [...viewable].sort((x, y) => (y.aoi_clear_frac ?? 0) - (x.aoi_clear_frac ?? 0))[0] ??
        null)
      : null;

  const move = useCallback(
    (which: Slot, key: string) => {
      if (!index.has(key)) return;
      setPair((p) => (p ? (which === "A" ? { ...p, a: key } : { ...p, b: key }) : p));
      if (which === "B") setPassId(null);
    },
    [index],
  );

  const step = useCallback(
    (which: Slot, delta: number) => {
      const current = which === "A" ? pair?.a : pair?.b;
      const i = current === undefined ? undefined : index.get(current);
      const target = i === undefined ? undefined : months[i + delta];
      if (target) move(which, target.key);
    },
    [pair, index, months, move],
  );

  // Timelapse: B walks forward a month at a time; it starts over from the first month.
  useEffect(() => {
    if (!playing || !pair) return;
    const timer = window.setTimeout(() => {
      const i = index.get(pair.b) ?? 0;
      const next = months[i + 1];
      if (next) move("B", next.key);
      else setPlaying(false);
    }, FRAME_MS);
    return () => window.clearTimeout(timer);
  }, [playing, pair, index, months, move]);

  // A change opens on its comparison: A the month a year before, B the month it showed.
  const selectChange = useCallback(
    (event: ChangeEvent | null) => {
      setSelected(event?.id ?? null);
      if (!event) return;
      setPlaying(false);
      setSource("composite");
      const before = monthKey(event.reference_month);
      const after = monthKey(event.month);
      setPair((p) => ({ a: index.has(before) ? before : (p?.a ?? before), b: after }));
    },
    [index],
  );

  // An alert email's link opens on its change, once the archive and the changes are here.
  const opened = useRef<number | null>(null);
  useEffect(() => {
    if (change === null || opened.current === change) return;
    if (events.state !== "done" || months.length === 0) return;
    const event = events.data.find((e) => e.id === change);
    opened.current = change;
    if (!event) return;
    if (!event.alerts) setShown("all");
    selectChange(event);
  }, [change, events, months, selectChange]);

  const notReal = useCallback(
    async (event: ChangeEvent) => {
      await api.notReal(area.id, event.id);
      reloadEvents();
    },
    [area.id, reloadEvents],
  );

  const togglePlay = useCallback(() => {
    if (!playing && pair && index.get(pair.b) === months.length - 1 && months[0])
      move("B", months[0].key);
    setPlaying((p) => !p);
  }, [playing, pair, index, months, move]);

  // Keys: arrows step the active cursor, Shift jumps a year, Space plays, A and B pick the cursor.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement;
      if (target.closest("input, select, textarea, [role='slider'], .maplibregl-canvas")) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        setPlaying(false);
        step(slot, (event.key === "ArrowLeft" ? -1 : 1) * (event.shiftKey ? 12 : 1));
      } else if (event.key === " " && !target.closest("button")) {
        event.preventDefault();
        togglePlay();
      } else if (event.key === "a" || event.key === "b") {
        setSlot(event.key === "a" ? "A" : "B");
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [slot, step, togglePlay]);

  const controls = (
    <>
      <Segmented
        label="Layer"
        value={layer}
        onChange={setLayer}
        options={[
          ["true_color", "True colour"],
          ["ndvi", "NDVI"],
        ]}
      />
      <Segmented
        label="Month B shows"
        value={source}
        onChange={(value) => {
          setSource(value);
          setPassId(null);
        }}
        options={[
          ["composite", "Monthly composite"],
          ["pass", "One pass"],
        ]}
      />
    </>
  );

  if (archive.state === "loading") return <LoadingState label="Loading the archive" />;
  if (archive.state === "error")
    return <ErrorState message={archive.message} onRetry={reloadArchive} />;
  if (months.length === 0 || !a || !b) {
    return (
      <AreaLayout area={area}>
        <div className="empty-state">
          <h2>No months yet</h2>
          <p>
            No passes over this area have been composited. New passes appear here after the next
            check.
          </p>
          <button type="button" className="btn" onClick={onUpdate} disabled={updating}>
            Check for new passes
          </button>
        </div>
      </AreaLayout>
    );
  }

  const note = compareNote(
    { key: a.key, observedFrac: a.observed_frac, filledFrac: a.filled_frac },
    { key: b.key, observedFrac: b.observed_frac, filledFrac: b.filled_frac },
  );
  const noPass = source === "pass" && passes.state === "done" && !chosenPass;
  const range = ndviRange(months);
  const next = playing ? (months[(index.get(b.key) ?? 0) + 1] ?? null) : null;

  return (
    <AreaLayout area={area} controls={controls}>
      <div className="stage">
        <CompareMap
          area={area}
          a={a}
          b={b}
          layer={layer}
          pass={chosenPass}
          next={next}
          theme={theme}
          changes={labels ? undefined : changes}
          selected={labels ? null : selected}
          onPick={(id) => selectChange(changes.find((e) => e.id === id) ?? null)}
          labels={labels}
          drawing={labelling.drawing}
          onDrawn={(footprint) => labelling.add(footprint, b.key)}
        />
        <p className={note.tone === "warn" || noPass ? "caption caption-warn" : "caption"}>
          {noPass
            ? `No pass in ${monthLabel(b.key)} had its bands read, so B shows the monthly composite.`
            : note.text}
        </p>
      </div>

      <section className="timeline" aria-labelledby="timeline-title">
        <div className="timeline-head">
          <h2 id="timeline-title">Archive</h2>
          <div className="legend">
            <span>
              <i className="key key-clear" />
              clear passes
            </span>
            <span>
              <i className="key key-all" />
              all passes
            </span>
            <span>
              <i className="key key-gap" />
              no clear pass, filled for display
            </span>
            {range && (
              <span>
                <i className="key key-ndvi" />
                mean NDVI <span className="fig">{range[0].toFixed(2)}</span> to{" "}
                <span className="fig">{range[1].toFixed(2)}</span>
              </span>
            )}
          </div>
          <div className="timeline-tools">
            <Segmented
              label="Clicking a month moves"
              value={slot}
              onChange={setSlot}
              options={[
                ["A", "Cursor A"],
                ["B", "Cursor B"],
              ]}
            />
            <button type="button" className="btn" aria-pressed={playing} onClick={togglePlay}>
              {playing ? "Pause" : "Play"}
            </button>
          </div>
        </div>
        <Timeline
          months={months}
          a={a.key}
          b={b.key}
          marks={marks}
          onPick={(key) => move(slot, key)}
        />
        <p className="keys muted small">
          <kbd>←</kbd> <kbd>→</kbd> step cursor {slot}, <kbd>Shift</kbd> jumps a year, <kbd>A</kbd>{" "}
          <kbd>B</kbd> pick the cursor, <kbd>Space</kbd> plays.
        </p>
      </section>

      <aside className="panel">
        {status && <BackfillCard progress={status} onRetry={onUpdate} update />}
        {labels && <LabelsCard labelling={labelling} month={b.key} onShow={(k) => move("B", k)} />}
        <StatsCard area={area} onUpdate={status ? undefined : onUpdate} updating={updating} />
        {!labels && (
          <ChangesCard
            areaId={area.id}
            events={events}
            shown={shown}
            onShow={setShown}
            selected={selected}
            onSelect={selectChange}
            onRetry={reloadEvents}
            onNotReal={notReal}
          />
        )}
        {!labels && (
          <AlertsCard
            areaId={area.id}
            years={archiveYears(area.first_month, area.last_month)}
            onSaved={reloadEvents}
          />
        )}
        {admin && !labels && (
          <LabelsCard labelling={labelling} month={b.key} onShow={(k) => move("B", k)} />
        )}
        <MonthCard
          month={b}
          passes={passes}
          selected={chosenPass?.item_id ?? null}
          onSelect={(pass) => {
            setSource("pass");
            setPassId(pass.item_id);
          }}
          onRetry={reloadPasses}
        />
      </aside>
    </AreaLayout>
  );
}
