import { describe, expect, it } from "vitest";
import type { AlertSettings, EventStatus } from "../api";
import { parseRoute } from "../hooks";
import { alertText, historyLine } from "./changes";
import { formatArea, formatBytes, formatLonLat, formatUtc } from "./format";
import { boundsOf, type Polygon, polygonAreaKm2 } from "./geo";
import { compareNote, defaultPair, type MonthFacts, monthLabel, shiftMonth } from "./months";
import { archiveYears, perYear, sameSettings } from "./rules";
import { intersect, tilesCovering, tileUrl } from "./tiles";

// The test AOI near Kyiv; the API measures it at 2.456 km² in UTM zone 36N.
const KYIV: Polygon = {
  type: "Polygon",
  coordinates: [
    [
      [30.674, 50.3845],
      [30.701, 50.3845],
      [30.701, 50.396],
      [30.674, 50.396],
      [30.674, 50.3845],
    ],
  ],
};

describe("geo", () => {
  it("measures an area close to the API's UTM figure", () => {
    expect(polygonAreaKm2(KYIV)).toBeCloseTo(2.456, 1);
  });

  it("subtracts holes and ignores rings too short to enclose anything", () => {
    const holed: Polygon = {
      type: "Polygon",
      coordinates: [...KYIV.coordinates, ...KYIV.coordinates],
    };
    expect(polygonAreaKm2(holed)).toBeCloseTo(0, 6);
    expect(
      polygonAreaKm2({
        type: "Polygon",
        coordinates: [
          [
            [0, 0],
            [1, 1],
          ],
        ],
      }),
    ).toBe(0);
  });

  it("finds bounds", () => {
    expect(boundsOf(KYIV.coordinates[0] ?? [])).toEqual([30.674, 50.3845, 30.701, 50.396]);
  });
});

const month = (key: string, observedFrac = 1, filledFrac = 0): MonthFacts => ({
  key,
  observedFrac,
  filledFrac,
});

describe("months", () => {
  it("labels and shifts months across year boundaries", () => {
    expect(monthLabel("2026-08")).toBe("Aug 2026");
    expect(shiftMonth("2026-01", -1)).toBe("2025-12");
    expect(shiftMonth("2025-12", 1)).toBe("2026-01");
    expect(shiftMonth("2026-08", -12)).toBe("2025-08");
  });

  it("defaults to the latest clear month against the same month a year earlier", () => {
    const months = ["2025-07", "2025-08", "2025-09", "2026-07", "2026-08"].map((k) => month(k));
    months.push(month("2026-09", 0, 1)); // no clear pass yet
    expect(defaultPair(months)).toEqual({ a: "2025-08", b: "2026-08" });
  });

  it("falls back to the first month when there is no anniversary", () => {
    expect(defaultPair(["2026-07", "2026-08"].map((k) => month(k)))).toEqual({
      a: "2026-07",
      b: "2026-08",
    });
    expect(defaultPair([])).toBeNull();
  });

  it("warns about filled months before anything else", () => {
    const note = compareNote(month("2025-08"), month("2026-11", 0, 1));
    expect(note.tone).toBe("warn");
    expect(note.text).toContain("never counts as change");
    expect(compareNote(month("2025-08", 1, 0.25), month("2026-08")).text).toMatch(
      /^25% of Aug 2025/,
    );
  });

  it("warns when A and B are different seasons, and not for anniversaries", () => {
    expect(compareNote(month("2026-01"), month("2026-07")).tone).toBe("warn");
    expect(compareNote(month("2021-08"), month("2026-08"))).toEqual({
      tone: "ok",
      text: "Same month, 5 years apart. Drag the divider to swipe between them.",
    });
  });
});

describe("format", () => {
  it("formats bytes, areas, UTC times and coordinates", () => {
    expect(formatBytes(248_253_735)).toBe("248 MB");
    expect(formatBytes(35_552_985_454)).toBe("35.6 GB");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatArea(245.59)).toBe("2.46 km²");
    expect(formatArea(42.04)).toBe("42.0 ha");
    expect(formatUtc("2026-11-05T09:16:17.01Z")).toBe("5 Nov 09:16");
    expect(formatLonLat(30.69, -50.39)).toBe("50.39000° S, 30.69000° E");
  });
});

describe("tiles", () => {
  it("covers bounds with the tiles a map would request", () => {
    // Kyiv AOI at z12: one tile column, one row.
    expect(tilesCovering([30.674, 50.3845, 30.701, 50.396], 12)).toEqual([
      { z: 12, x: 2397, y: 1382 },
    ]);
    expect(tileUrl("https://t/{z}/{x}/{y}.png?v=1", { z: 1, x: 2, y: 3 })).toBe(
      "https://t/1/2/3.png?v=1",
    );
  });

  it("intersects bounds", () => {
    expect(intersect([0, 0, 2, 2], [1, 1, 3, 3])).toEqual([1, 1, 2, 2]);
    expect(intersect([0, 0, 1, 1], [2, 2, 3, 3])).toBeNull();
  });
});

describe("changes", () => {
  it("words the API's alert verdict without deciding it", () => {
    const verdict = (alerts: boolean, alert_reason: string, status: EventStatus = "pending") =>
      alertText({ alerts, alert_reason, status });
    expect(verdict(true, "alert")).toBe("Would alert");
    expect(verdict(true, "alert", "dismissed")).toBe("Would alert, then retract");
    expect(verdict(false, "under 0.5 ha")).toBe("Under 0.5 ha");
    expect(verdict(false, "category off")).toBe("Category not watched");
    expect(verdict(false, "seasonal", "seasonal")).toBe("No alert");
  });

  it("tells a status history by pass and by month", () => {
    const pass = "S2A_33UVU_20200401_1_L2A";
    expect(historyLine({ status: "pending", cause: pass, at: "2020-04-01T10:26:01Z" }, true)).toBe(
      `Seen 1 Apr 2020 on ${pass}`,
    );
    expect(
      historyLine({ status: "confirmed", cause: pass, at: "2020-04-08T10:26:01Z" }, false),
    ).toBe(`Confirmed 8 Apr 2020 by ${pass}`);
    expect(
      historyLine({ status: "seasonal", cause: "snow in 2021-01", at: "2021-01-26T10:26Z" }, true),
    ).toBe("Seasonal: snow in Jan 2021");
    expect(
      historyLine(
        { status: "dismissed", cause: "no longer detected", at: "2026-11-07T18:00Z" },
        false,
      ),
    ).toBe("Dismissed: no longer detected");
  });
});

describe("rules", () => {
  const rule: AlertSettings = {
    enabled: true,
    categories: ["buildings_roads", "vegetation_loss"],
    skip_seasonal: true,
    min_ha: 0.5,
  };
  it("compares settings whatever the category order", () => {
    const reordered = { ...rule, categories: ["vegetation_loss", "buildings_roads"] as const };
    expect(sameSettings(rule, { ...reordered, categories: [...reordered.categories] })).toBe(true);
    expect(sameSettings(rule, { ...rule, min_ha: 0.6 })).toBe(false);
    expect(sameSettings(rule, { ...rule, categories: ["vegetation_loss"] })).toBe(false);
  });
  it("puts a count over the archive's years", () => {
    expect(archiveYears("2017-01-01", "2026-12-01")).toBe(10);
    expect(archiveYears(null, "2026-12-01")).toBe(0);
    expect(perYear(67, 8.9)).toBe("about 7.5 a year");
    expect(perYear(150, 10)).toBe("about 15 a year");
    expect(perYear(3, 0.5)).toBeNull();
  });
});

describe("routes", () => {
  it("opens an area on one change from an alert email's link", () => {
    expect(parseRoute("#/areas/3/changes/17")).toEqual({ page: "area", id: 3, change: 17 });
    expect(parseRoute("#/areas/3")).toEqual({ page: "area", id: 3, change: null });
    expect(parseRoute("#/new")).toEqual({ page: "new" });
    expect(parseRoute("#/admin")).toEqual({ page: "admin" });
    expect(parseRoute("#/areas/x")).toEqual({ page: "areas" });
  });
});
