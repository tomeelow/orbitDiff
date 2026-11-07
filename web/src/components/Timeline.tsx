import { useEffect, useRef, useState } from "react";
import type { Month } from "../api";
import { formatPercent } from "../lib/format";
import { monthLabel, monthName, yearOf } from "../lib/months";

export interface TimelineMonth extends Month {
  key: string; // "2026-08"
  ndvi: number | null; // AOI mean over observed pixels
}

interface Props {
  months: TimelineMonth[];
  a: string | null;
  b: string;
  onPick: (key: string) => void;
}

const DOT_PAD = 0.12; // keep NDVI dots off the row's edges

/** Months as columns: all passes as an outline, clear passes filled green, gaps hatched,
mean NDVI as a line underneath. Clicking a month moves the active cursor. */
export function Timeline({ months, a, b, onPick }: Props) {
  const scroller = useRef<HTMLDivElement>(null);
  const [tip, setTip] = useState<{ month: TimelineMonth; x: number; y: number } | null>(null);
  const maxPasses = Math.max(1, ...months.map((m) => m.passes));
  const values = months.flatMap((m) => (m.ndvi === null ? [] : [m.ndvi]));
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 0.1;
  const y = (ndvi: number) => DOT_PAD + (1 - DOT_PAD * 2) * ((ndvi - lo) / span); // 0 bottom, 1 top
  const wide = months.length <= 24;

  // Keep cursor B in view as it moves (keyboard stepping, timelapse).
  // Only the strip scrolls: scrollIntoView would also move the page.
  useEffect(() => {
    const strip = scroller.current;
    const column = strip?.querySelector<HTMLElement>(`[data-month="${b}"]`);
    if (!strip || !column) return;
    const left = column.offsetLeft;
    const right = left + column.offsetWidth;
    if (left < strip.scrollLeft) strip.scrollLeft = left;
    else if (right > strip.scrollLeft + strip.clientWidth)
      strip.scrollLeft = right - strip.clientWidth;
  }, [b]);

  // The NDVI line, broken where a month has no clear pixels.
  const segments: string[] = [];
  let run: string[] = [];
  months.forEach((m, i) => {
    if (m.ndvi === null) {
      if (run.length > 1) segments.push(run.join(" "));
      run = [];
    } else {
      run.push(`${i + 0.5},${(1 - y(m.ndvi)) * 100}`);
    }
  });
  if (run.length > 1) segments.push(run.join(" "));

  function showTip(month: TimelineMonth, element: HTMLElement) {
    const rect = element.getBoundingClientRect();
    setTip({ month, x: rect.left + rect.width / 2, y: rect.top });
  }

  return (
    <div className="strip-scroll" ref={scroller}>
      <div
        className="strip"
        style={{ "--n": months.length, "--col-min": wide ? "44px" : "14px" } as React.CSSProperties}
      >
        {values.length > 0 && (
          <svg
            className="ndvi-line"
            viewBox={`0 0 ${months.length} 100`}
            preserveAspectRatio="none"
            aria-hidden="true"
          >
            {segments.map((points) => (
              <polyline key={points} points={points} vectorEffect="non-scaling-stroke" />
            ))}
          </svg>
        )}
        {months.map((m, i) => {
          const isA = m.key === a;
          const isB = m.key === b;
          const showYear = i === 0 || m.key.endsWith("-01");
          return (
            <button
              key={m.key}
              type="button"
              className="col"
              data-month={m.key}
              data-selected={isA || isB || undefined}
              aria-label={`${monthLabel(m.key)}: ${m.clear_scenes} clear of ${m.passes} passes${
                m.ndvi === null ? ", no clear pixels" : `, mean NDVI ${m.ndvi.toFixed(2)}`
              }${isA ? ", cursor A" : ""}${isB ? ", cursor B" : ""}`}
              onClick={() => onPick(m.key)}
              onMouseEnter={(e) => showTip(m, e.currentTarget)}
              onFocus={(e) => showTip(m, e.currentTarget)}
              onMouseLeave={() => setTip(null)}
              onBlur={() => setTip(null)}
            >
              <span className="col-cursor">
                {isA && <b className="cursor-a">A</b>}
                {isB && <b className="cursor-b">B</b>}
              </span>
              <span className={m.gap ? "col-bar col-bar-gap" : "col-bar"}>
                <span className="bar-all" style={{ height: `${(m.passes / maxPasses) * 100}%` }} />
                <span
                  className="bar-clear"
                  style={{ height: `${(m.clear_scenes / maxPasses) * 100}%` }}
                />
              </span>
              <span className="col-ndvi">
                {m.ndvi !== null && <i style={{ bottom: `${y(m.ndvi) * 100}%` }} />}
              </span>
              <span className="col-tick">
                {wide && monthName(m.key)}
                {showYear && <small>{yearOf(m.key)}</small>}
              </span>
            </button>
          );
        })}
      </div>
      {tip && (
        <div className="tip" role="tooltip" style={{ left: tip.x, top: tip.y }}>
          <b>{monthLabel(tip.month.key)}</b>
          <span>
            {tip.month.clear_scenes} clear of {tip.month.passes} passes
          </span>
          <span>
            {tip.month.ndvi === null ? "No clear pixels" : `Mean NDVI ${tip.month.ndvi.toFixed(2)}`}
          </span>
          {tip.month.filled_frac > 0 && (
            <span>{formatPercent(tip.month.filled_frac)} filled for display</span>
          )}
        </div>
      )}
    </div>
  );
}

/** The NDVI range the line spans, for the legend. */
export function ndviRange(months: TimelineMonth[]): [number, number] | null {
  const values = months.flatMap((m) => (m.ndvi === null ? [] : [m.ndvi]));
  return values.length ? [Math.min(...values), Math.max(...values)] : null;
}
