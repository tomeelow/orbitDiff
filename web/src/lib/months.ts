/** Month keys ("2026-08"), labels and the A/B comparison rules. Months are UTC calendar
months, exactly as the pipeline buckets passes; no Date objects, so no time zone can move one. */

const NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-08-01" (an API date) or "2026-08" -> "2026-08". */
export const monthKey = (date: string): string => date.slice(0, 7);

const parts = (key: string): [year: number, month: number] => [
  Number(key.slice(0, 4)),
  Number(key.slice(5, 7)),
];

export function monthLabel(key: string): string {
  const [year, month] = parts(key);
  return `${NAMES[month - 1]} ${year}`;
}

export const monthName = (key: string): string => NAMES[parts(key)[1] - 1] ?? "";
export const yearOf = (key: string): number => parts(key)[0];

export function shiftMonth(key: string, delta: number): string {
  const [year, month] = parts(key);
  const index = year * 12 + (month - 1) + delta;
  return `${Math.floor(index / 12)}-${String((index % 12) + 1).padStart(2, "0")}`;
}

/** What the comparison needs to know about a month. */
export interface MonthFacts {
  key: string;
  observedFrac: number; // share of the AOI with a clear observation
  filledFrac: number; // share filled from nearby months, for display only
}

/** B: the latest month with clear pixels. A: the same month a year earlier when the archive
has it (like with like), else the first month. */
export function defaultPair(months: MonthFacts[]): { a: string; b: string } | null {
  const observed = months.filter((m) => m.observedFrac > 0);
  const last = observed.at(-1) ?? months.at(-1);
  const first = months[0];
  if (!last || !first) return null;
  const anniversary = shiftMonth(last.key, -12);
  const a = months.some((m) => m.key === anniversary) ? anniversary : first.key;
  return { a, b: last.key };
}

export interface CompareNote {
  tone: "ok" | "warn";
  text: string;
}

const pct = (frac: number) => `${Math.round(frac * 100)}%`;

/** The caption under the map. Filled pixels and season mismatches come first: both make a
picture that looks like change and isn't. */
export function compareNote(a: MonthFacts, b: MonthFacts): CompareNote {
  for (const m of [a, b]) {
    if (m.observedFrac === 0) {
      return {
        tone: "warn",
        text: `No clear pass in ${monthLabel(m.key)}. Its picture is filled from nearby months for display only, and never counts as change.`,
      };
    }
  }
  for (const m of [a, b]) {
    if (m.filledFrac > 0) {
      return {
        tone: "warn",
        text: `${pct(m.filledFrac)} of ${monthLabel(m.key)} is filled from nearby months, for display only.`,
      };
    }
  }
  if (a.key === b.key) return { tone: "ok", text: "A and B are the same month." };
  if (a.key.slice(5) !== b.key.slice(5)) {
    return {
      tone: "warn",
      text: `${monthLabel(a.key)} against ${monthLabel(b.key)} compares different seasons, so most of the difference is the season. Shift + arrow keys jump a year to the same month.`,
    };
  }
  const years = Math.abs(yearOf(b.key) - yearOf(a.key));
  return {
    tone: "ok",
    text: `Same month, ${years} ${years === 1 ? "year" : "years"} apart. Drag the divider to swipe between them.`,
  };
}
