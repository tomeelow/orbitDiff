/** Small shared pieces: a segmented control and the loading and error states. */

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
