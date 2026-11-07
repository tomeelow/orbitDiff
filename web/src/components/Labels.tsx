import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api, type LabelCategory, type LabelChange, type LabelSet, type Labels } from "../api";
import { type Load, useLoad } from "../hooks";
import { CATEGORY } from "../lib/changes";
import type { Polygon } from "../lib/geo";
import { monthLabel, shiftMonth } from "../lib/months";

const CATEGORIES: LabelCategory[] = [
  "buildings_roads",
  "vegetation_loss",
  "solar_industry",
  "water",
];
const HARD_CASES: Record<string, string> = {
  cropland: "Cropland",
  floodplain: "Floodplain",
  snow: "Snow",
  deciduous_forest: "Deciduous forest",
  airport: "Airport",
};

type Change = LabelChange & { key: number }; // a stable React key for each drawn footprint
type Draft = Omit<LabelSet, "changes"> & { changes: Change[] };

let nextKey = 0;
const keyed = (change: LabelChange): Change => ({ ...change, key: nextKey++ });

function draftOf({ name: _name, saved: _saved, ...set }: Labels): Draft {
  return { ...set, changes: set.changes.map(keyed) };
}

function setOf(draft: Draft): LabelSet {
  return { ...draft, changes: draft.changes.map(({ key: _key, ...change }) => change) };
}

export interface Labelling {
  labels: Load<Labels | null>;
  reload: () => void;
  draft: Draft | null;
  edit: (change: Partial<Draft>) => void;
  dirty: boolean;
  save: () => Promise<void>;
  on: boolean; // labelling: the map shows the labels instead of the detected changes
  setOn: (on: boolean) => void;
  drawing: boolean;
  setDrawing: (on: boolean) => void;
  add: (footprint: Polygon, month: string) => void;
}

/** An area's evaluation labels, as a draft the map and the card share. Admins only: they go
to the repository's eval/ folder, which every detection method is scored on. */
export function useLabelling(areaId: number, admin: boolean): Labelling {
  const [labels, reload] = useLoad(
    () => (admin ? api.labels(areaId) : Promise.resolve(null)),
    [areaId, admin],
  );
  const [draft, setDraft] = useState<Draft | null>(null);
  const [on, setOn] = useState(false);
  const [drawing, setDrawing] = useState(false);
  const saved = labels.state === "done" ? labels.data : null;

  useEffect(() => {
    if (saved) setDraft(draftOf(saved));
  }, [saved]);

  const edit = useCallback(
    (change: Partial<Draft>) => setDraft((d) => (d ? { ...d, ...change } : d)),
    [],
  );
  const add = useCallback((footprint: Polygon, month: string) => {
    const change = keyed({ category: "buildings_roads", month, geometry: footprint });
    setDraft((d) => (d ? { ...d, changes: [...d.changes, change] } : d));
    setDrawing(false);
  }, []);
  const save = useCallback(async () => {
    if (!draft) return;
    await api.saveLabels(areaId, setOf(draft));
    reload();
  }, [areaId, draft, reload]);

  return {
    labels,
    reload,
    draft,
    edit,
    dirty:
      saved !== null &&
      draft !== null &&
      JSON.stringify(setOf(draft)) !== JSON.stringify(setOf(draftOf(saved))),
    save,
    on,
    setOn: (value) => {
      setOn(value);
      setDrawing(false);
      if (!value && saved) setDraft(draftOf(saved)); // closing discards what wasn't saved
    },
    drawing,
    setDrawing,
    add,
  };
}

/** Label the real changes in an area for the evaluation set (eval/README.md). The map hides
the detected changes meanwhile, so the labels don't just copy what a detector found. */
export function LabelsCard({
  labelling,
  month,
  onShow,
}: {
  labelling: Labelling;
  month: string; // cursor B, where a new footprint is drawn
  onShow: (month: string) => void;
}) {
  const { labels, draft, edit, dirty, on, setOn, drawing, setDrawing } = labelling;
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);
  const saved = labels.state === "done" ? labels.data : null;

  if (!saved || !draft) {
    return (
      <section className="card" aria-labelledby="labels-title">
        <h2 id="labels-title">Evaluation labels</h2>
        {labels.state === "error" ? (
          <div className="state-inline">
            <p className="error-text">{labels.message}</p>
            <button type="button" className="btn" onClick={labelling.reload}>
              Try again
            </button>
          </div>
        ) : (
          <p className="muted">Loading the labels…</p>
        )}
      </section>
    );
  }

  const file = `eval/${saved.name}.geojson`;
  const tag = !saved.saved ? null : saved.verified_by ? "Verified" : "Candidate";
  const head = (
    <div className="card-head">
      <h2 id="labels-title">Evaluation labels</h2>
      {tag && <span className={tag === "Verified" ? "tag tag-alert" : "tag"}>{tag}</span>}
    </div>
  );

  if (!on) {
    const n = saved.changes.length;
    return (
      <section className="card" aria-labelledby="labels-title">
        {head}
        <p className="muted small">
          {saved.saved
            ? `${n === 0 ? "A quiet area, no changes" : `${n} ${n === 1 ? "change" : "changes"}`}, in ${file}.`
            : `Label the real changes here, and every detection method is scored on them. They're saved to ${file}.`}
        </p>
        <div className="actions">
          <button type="button" className="btn" onClick={() => setOn(true)}>
            {saved.saved ? "Edit labels" : "Label this area"}
          </button>
        </div>
      </section>
    );
  }

  const scoredFrom = shiftMonth(draft.first_month, 12);
  const hardCases = [...new Set([...Object.keys(HARD_CASES), ...draft.hard_cases])];
  const setChange = (key: number, change: Partial<LabelChange>) => {
    edit({ changes: draft.changes.map((c) => (c.key === key ? { ...c, ...change } : c)) });
    setMessage(null);
  };
  const change = (fields: Partial<Draft>) => {
    edit(fields);
    setMessage(null);
  };

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      await labelling.save();
      setMessage({ text: `Saved to ${file}.`, error: false });
    } catch (error) {
      setMessage({ text: error instanceof Error ? error.message : String(error), error: true });
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="card" aria-labelledby="labels-title">
      {head}
      <form onSubmit={submit} className="labels">
        <p className="muted small">
          The map shows these labels instead of the detected changes, so they don't copy a detector.
        </p>

        <fieldset>
          <legend>Period of imagery</legend>
          <div className="field-row">
            <input
              className="input"
              type="month"
              aria-label="First month"
              required
              value={draft.first_month}
              onChange={(e) => change({ first_month: e.target.value })}
            />
            <input
              className="input"
              type="month"
              aria-label="Last month"
              required
              value={draft.last_month}
              onChange={(e) => change({ last_month: e.target.value })}
            />
          </div>
          <p className="muted small">
            Changes count from {monthLabel(scoredFrom)}: the first year has no anniversary to
            compare with.
          </p>
        </fieldset>

        <h3 className="card-sub">Changes</h3>
        {draft.changes.length === 0 ? (
          <p className="muted small">None: a quiet area. Every alert here counts as false.</p>
        ) : (
          <ol className="label-list">
            {draft.changes.map((c, i) => (
              <li key={c.key}>
                <i className="change-dot" style={{ background: CATEGORY[c.category].color }} />
                <select
                  className="input"
                  aria-label={`Change ${i + 1}: what changed`}
                  value={c.category}
                  onChange={(e) => setChange(c.key, { category: e.target.value as LabelCategory })}
                >
                  {CATEGORIES.map((category) => (
                    <option key={category} value={category}>
                      {CATEGORY[category].title}
                    </option>
                  ))}
                </select>
                <input
                  className="input"
                  type="month"
                  aria-label={`Change ${i + 1}: first month visible`}
                  required
                  value={c.month}
                  onChange={(e) => setChange(c.key, { month: e.target.value })}
                />
                <span className="label-tools">
                  <button type="button" className="btn" onClick={() => onShow(c.month)}>
                    Show
                  </button>
                  <button
                    type="button"
                    className="btn"
                    onClick={() =>
                      change({ changes: draft.changes.filter((other) => other.key !== c.key) })
                    }
                  >
                    Remove
                  </button>
                </span>
              </li>
            ))}
          </ol>
        )}
        <div className="actions">
          <button
            type="button"
            className="btn"
            aria-pressed={drawing}
            onClick={() => setDrawing(!drawing)}
          >
            {drawing ? "Cancel drawing" : `Draw a change in ${monthLabel(month)}`}
          </button>
        </div>
        <p className="muted small" role="status">
          {drawing
            ? "Click each corner on the map, then the first corner again to close it."
            : "Step B to the first month the change is visible, then draw it as it shows there."}
        </p>

        <fieldset className="checks">
          <legend>What makes it hard</legend>
          {hardCases.map((hard) => (
            <label key={hard} className="check">
              <input
                type="checkbox"
                checked={draft.hard_cases.includes(hard)}
                onChange={(e) =>
                  change({
                    hard_cases: e.target.checked
                      ? [...draft.hard_cases, hard]
                      : draft.hard_cases.filter((h) => h !== hard),
                  })
                }
              />
              {HARD_CASES[hard] ?? hard}
            </label>
          ))}
        </fieldset>

        <fieldset>
          <legend>Verified</legend>
          <p className="muted small">
            Only once every change, and the quiet rest of the area, is checked against
            high-resolution imagery. Until then the file is a candidate, and its scores are never
            reported as a benchmark.
          </p>
          <label htmlFor="verified-by">By</label>
          <input
            id="verified-by"
            className="input"
            value={draft.verified_by ?? ""}
            onChange={(e) => change({ verified_by: e.target.value || null })}
          />
          <label htmlFor="verified-on">On</label>
          <input
            id="verified-on"
            className="input"
            type="date"
            value={draft.verified_on ?? ""}
            onChange={(e) => change({ verified_on: e.target.value || null })}
          />
          <label htmlFor="verified-with">Against</label>
          <input
            id="verified-with"
            className="input"
            placeholder="The imagery and its dates"
            value={draft.verified_with ?? ""}
            onChange={(e) => change({ verified_with: e.target.value || null })}
          />
        </fieldset>

        <div className="actions">
          <button type="submit" className="btn btn-primary" disabled={!dirty || saving}>
            {saving ? "Saving…" : "Save"}
          </button>
          <button type="button" className="btn" onClick={() => setOn(false)}>
            {dirty ? "Discard and close" : "Close"}
          </button>
        </div>
        {message && (
          <p className={message.error ? "error-text small" : "muted small"} role="status">
            {message.text}
          </p>
        )}
      </form>
    </section>
  );
}
