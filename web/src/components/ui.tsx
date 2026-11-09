/** Small shared pieces: a segmented control, a delete that asks first, and the loading and
error states. */

import { useState } from "react";

export function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: [T, string][];
  onChange: (value: T) => void;
}) {
  return (
    <fieldset className="seg">
      <legend className="visually-hidden">{label}</legend>
      {options.map(([option, text]) => (
        <button
          key={option}
          type="button"
          aria-pressed={option === value}
          onClick={() => onChange(option)}
        >
          {text}
        </button>
      ))}
    </fieldset>
  );
}

export function LoadingState({ label }: { label: string }) {
  return (
    <main className="screen screen-state">
      <p className="loading" aria-live="polite">
        {label}…
      </p>
    </main>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <main className="screen screen-state">
      <div className="empty-state" role="alert">
        <h2>Something went wrong</h2>
        <p className="error-text">{message}</p>
        <button type="button" className="btn" onClick={onRetry}>
          Try again
        </button>
      </div>
    </main>
  );
}

/** Something that can't be undone, behind a second click that says what goes with it. */
export function ConfirmDelete({
  label,
  warning,
  onDelete,
}: {
  label: string;
  warning: string;
  onDelete: () => Promise<void>;
}) {
  const [state, setState] = useState<"idle" | "asking" | "deleting">("idle");
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    setState("deleting");
    setError(null);
    try {
      await onDelete();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setState("asking");
    }
  }

  if (state === "idle") {
    return (
      <button type="button" className="btn btn-quiet" onClick={() => setState("asking")}>
        {label}
      </button>
    );
  }
  return (
    <div className="confirm" role="alertdialog" aria-label={label}>
      <p>{warning}</p>
      {error && (
        <p className="error-text small" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button
          type="button"
          className="btn btn-danger"
          onClick={confirm}
          disabled={state === "deleting"}
        >
          {state === "deleting" ? "Deleting…" : label}
        </button>
        <button
          type="button"
          className="btn"
          onClick={() => setState("idle")}
          disabled={state === "deleting"}
        >
          Keep it
        </button>
      </div>
    </div>
  );
}
