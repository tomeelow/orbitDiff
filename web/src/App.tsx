import { AreaPage } from "./components/AreaPage";
import { AreasPage } from "./components/AreasPage";
import { DrawPage } from "./components/DrawPage";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { type Route, type Theme, useRoute, useTheme } from "./hooks";

export function App() {
  const route = useRoute();
  const [theme, toggleTheme] = useTheme();
  return (
    <>
      <TopBar route={route} theme={theme} onToggleTheme={toggleTheme} />
      {/* Keyed by route: going to another screen clears the error. */}
      <ErrorBoundary key={route.page === "area" ? `area-${route.id}` : route.page}>
        {route.page === "areas" && <AreasPage />}
        {route.page === "new" && <DrawPage theme={theme} />}
        {route.page === "area" && <AreaPage id={route.id} theme={theme} />}
      </ErrorBoundary>
    </>
  );
}

function TopBar({
  route,
  theme,
  onToggleTheme,
}: {
  route: Route;
  theme: Theme;
  onToggleTheme: () => void;
}) {
  const next = theme === "dark" ? "light" : "dark";
  return (
    <header className="bar">
      <a className="brand" href="#/">
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="3.2" />
          <ellipse cx="12" cy="12" rx="10" ry="4.2" transform="rotate(-28 12 12)" />
        </svg>
        <span>Orbit Diff</span>
      </a>
      <nav aria-label="Sections">
        <a href="#/" aria-current={route.page !== "new" ? "page" : undefined}>
          Areas
        </a>
      </nav>
      <div className="bar-end">
        <button
          type="button"
          className="icon-btn"
          onClick={onToggleTheme}
          aria-label={`Switch to ${next} theme`}
        >
          {theme === "dark" ? (
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              aria-hidden="true"
            >
              <circle cx="12" cy="12" r="4" />
              <path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
            </svg>
          ) : (
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              aria-hidden="true"
            >
              <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z" />
            </svg>
          )}
        </button>
        {route.page !== "new" && (
          <a className="btn btn-primary" href="#/new">
            New area
          </a>
        )}
      </div>
    </header>
  );
}
