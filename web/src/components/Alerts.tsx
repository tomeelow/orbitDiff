import { type FormEvent, useEffect, useState } from "react";
import { type AlertSettings, api, type Category, type RulePreview } from "../api";
import { useLoad } from "../hooks";
import { CATEGORY } from "../lib/changes";
import { formatUtcDate } from "../lib/format";
import { perYear, sameSettings, settingsOf } from "../lib/rules";

const PREVIEW_DELAY_MS = 250; // wait for the slider to settle before asking the API
const CATEGORIES = Object.keys(CATEGORY) as Category[];

/** The area's alert rule, edited as a draft with its "N of M past changes" preview. The API
evaluates the rule, for the preview as for sending: nothing here decides what alerts. */
export function AlertsCard({
  areaId,
  years,
  onSaved,
}: {
  areaId: number;
  years: number; // the archive's length, for "about N a year"
  onSaved: () => void;
}) {
  const [rule, reload] = useLoad(() => api.alertRule(areaId), [areaId]);
  const [draft, setDraft] = useState<AlertSettings | null>(null);
  // With the settings it was counted for: while the next count is on its way, it shows dimmed.
  const [preview, setPreview] = useState<{ settings: AlertSettings; result: RulePreview } | null>(
    null,
  );
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);

  useEffect(() => {
    if (rule.state === "done") setDraft(settingsOf(rule.data));
  }, [rule]);

  useEffect(() => {
    if (!draft) return;
    const abort = new AbortController();
    const timer = window.setTimeout(() => {
      api.previewAlertRule(areaId, draft, abort.signal).then(
        (result) => setPreview({ settings: draft, result }),
        () => {},
      );
    }, PREVIEW_DELAY_MS);
    return () => {
      window.clearTimeout(timer);
      abort.abort();
    };
  }, [areaId, draft]);

  if (rule.state === "loading" || !draft) {
    return (
      <section className="card" aria-labelledby="alerts-title">
        <h2 id="alerts-title">Alerts</h2>
        {rule.state === "error" ? (
          <div className="state-inline">
            <p className="error-text">{rule.message}</p>
            <button type="button" className="btn" onClick={reload}>
              Try again
            </button>
          </div>
        ) : (
          <p className="muted">Loading the alert rule…</p>
        )}
      </section>
    );
  }
  const saved = rule.state === "done" ? rule.data : null;
  const [low, high] = saved?.min_ha_range ?? [draft.min_ha, draft.min_ha];
  const dirty = saved !== null && !sameSettings(draft, settingsOf(saved));
  const edit = (change: Partial<AlertSettings>) => {
    setDraft({ ...draft, ...change });
    setMessage(null);
  };
  const toggle = (category: Category, on: boolean) =>
    edit({
      categories: on
        ? CATEGORIES.filter((c) => c === category || draft.categories.includes(c))
        : draft.categories.filter((c) => c !== category),
    });

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!draft) return;
    setSaving(true);
    try {
      await api.saveAlertRule(areaId, draft);
      reload();
      onSaved();
      setMessage({ text: draft.enabled ? "Saved. Alerts are on." : "Saved.", error: false });
    } catch (error) {
      setMessage({ text: error instanceof Error ? error.message : String(error), error: true });
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="card" aria-labelledby="alerts-title">
      <div className="card-head">
        <h2 id="alerts-title">Alerts</h2>
        {saved?.enabled && <span className="tag tag-alert">On</span>}
      </div>
      <form onSubmit={save} className="rule">
        <label className="check check-lead">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(e) => edit({ enabled: e.target.checked })}
          />
          Email me about changes here
        </label>
        {saved?.enabled && saved.watching_since && (
          <p className="muted small rule-note">
            Watching passes after {formatUtcDate(saved.watching_since)}, to the address you signed
            in with.
          </p>
        )}

        <fieldset className="checks">
          <legend>Changes to hear about</legend>
          {CATEGORIES.map((category) => (
            <label key={category} className="check">
              <input
                type="checkbox"
                checked={draft.categories.includes(category)}
                onChange={(e) => toggle(category, e.target.checked)}
              />
              <i className="change-dot" style={{ background: CATEGORY[category].color }} />
              {CATEGORY[category].title}
            </label>
          ))}
        </fieldset>

        <label className="check">
          <input
            type="checkbox"
            checked={draft.skip_seasonal}
            onChange={(e) => edit({ skip_seasonal: e.target.checked })}
          />
          Skip seasonal changes
        </label>
        <p className="muted small rule-note">Snow, floods and fields that come back each year.</p>

        <div className="slider">
          <div className="slider-head">
            <label htmlFor="min-ha">Smallest change</label>
            <output htmlFor="min-ha" className="fig">
              {draft.min_ha.toFixed(1)} ha
            </output>
          </div>
          <input
            id="min-ha"
            type="range"
            min={low}
            max={high}
            step={0.1}
            value={draft.min_ha}
            onChange={(e) => edit({ min_ha: Math.round(e.target.valueAsNumber * 10) / 10 })}
          />
          <div className="slider-ends muted small">
            <span className="fig">{low} ha</span>
            <span className="fig">{high} ha</span>
          </div>
        </div>

        <p
          className="rule-preview"
          aria-live="polite"
          aria-busy={preview?.settings !== draft}
          data-stale={preview && preview.settings !== draft ? "" : undefined}
        >
          {preview ? previewText(preview.result, years) : "Counting past changes…"}
        </p>

        <div className="actions">
          <button type="submit" className="btn btn-primary" disabled={!dirty || saving}>
            {saving ? "Saving…" : "Save"}
          </button>
          {message && (
            <p className={message.error ? "error-text small" : "muted small"} role="status">
              {message.text}
            </p>
          )}
        </div>
      </form>
    </section>
  );
}

function previewText({ changes, would_alert, retracted }: RulePreview, years: number): string {
  if (changes === 0) return "No changes found here yet, so there is nothing to preview.";
  const rate = perYear(would_alert, years);
  const head = `${would_alert.toLocaleString("en")} of ${changes.toLocaleString("en")} past changes would have emailed you${rate ? `, ${rate}` : ""}.`;
  if (retracted === 0) return head;
  return `${head} ${retracted.toLocaleString("en")} of them were later dismissed or found seasonal.`;
}
