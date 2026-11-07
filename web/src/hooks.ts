import { useCallback, useEffect, useRef, useState } from "react";

/** Routes are hash paths: "#/", "#/new", "#/areas/3". */
export type Route = { page: "areas" } | { page: "new" } | { page: "area"; id: number };

function parseRoute(hash: string): Route {
  if (hash === "#/new") return { page: "new" };
  const match = /^#\/areas\/(\d+)$/.exec(hash);
  return match ? { page: "area", id: Number(match[1]) } : { page: "areas" };
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

export const navigate = (hash: string) => {
  window.location.hash = hash;
};

export type Theme = "dark" | "light";
const THEME_KEY = "orbitdiff.theme";

function storedTheme(): Theme | null {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return value === "dark" || value === "light" ? value : null;
  } catch {
    return null;
  }
}

const systemTheme = (): Theme =>
  window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";

/** The system's theme until the user picks one; the pick is remembered on this device. */
export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => storedTheme() ?? systemTheme());
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: light)");
    const follow = () => {
      if (!storedTheme()) setTheme(systemTheme());
    };
    media.addEventListener("change", follow);
    return () => media.removeEventListener("change", follow);
  }, []);
  const toggle = useCallback(() => {
    setTheme((current) => {
      const next = current === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch {
        // storage blocked: the choice lasts for this visit
      }
      return next;
    });
  }, []);
  return [theme, toggle];
}

export type Load<T> =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "done"; data: T };

/** Runs `load` whenever `deps` change; `reload` runs it again. Stale answers are dropped. */
export function useLoad<T>(load: () => Promise<T>, deps: unknown[]): [Load<T>, () => void] {
  const [result, setResult] = useState<Load<T>>({ state: "loading" });
  const [attempt, setAttempt] = useState(0);
  const reloading = useRef(false); // a reload keeps showing the old answer until the new one
  // biome-ignore lint/correctness/useExhaustiveDependencies: deps are the caller's
  useEffect(() => {
    let current = true;
    if (!reloading.current) setResult({ state: "loading" });
    reloading.current = false;
    load().then(
      (data) => current && setResult({ state: "done", data }),
      (error: unknown) =>
        current &&
        setResult({
          state: "error",
          message: error instanceof Error ? error.message : String(error),
        }),
    );
    return () => {
      current = false;
    };
  }, [...deps, attempt]);
  const reload = useCallback(() => {
    reloading.current = true;
    setAttempt((n) => n + 1);
  }, []);
  return [result, reload];
}
