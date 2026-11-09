import { type FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import { useLoad } from "../hooks";

type Sent =
  | { state: "idle" }
  | { state: "sending" }
  | { state: "sent"; email: string; minutes: number };

/** Sign-in by emailed link: no password, and the address that signs in gets the alerts. */
export function SignInPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState<Sent>({ state: "idle" });
  const [error, setError] = useState<string | null>(null);
  const [limits] = useLoad(api.limits, []);
  const inviteOnly = limits.state === "done" && limits.data.invite_only;
  useEffect(() => {
    document.title = "Sign in — Orbit Diff";
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSent({ state: "sending" });
    try {
      const { minutes } = await api.signIn(email);
      setSent({ state: "sent", email: email.trim(), minutes });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSent({ state: "idle" });
    }
  }

  return (
    <main className="screen screen-narrow">
      <div className="signin">
        <h1>Sign in to Orbit Diff</h1>
        {sent.state === "sent" ? (
          <div role="status">
            <p>
              We sent a sign-in link to <strong>{sent.email}</strong>. Open it in this browser to
              sign in here. It works for {sent.minutes} minutes.
            </p>
            {inviteOnly && (
              <p className="muted small">
                This Orbit Diff is by invitation: the link only arrives if its owner has added your
                address.
              </p>
            )}
            {import.meta.env.DEV && (
              <p className="muted small">
                Development: every email lands in Mailpit at{" "}
                <a href="http://localhost:8025" target="_blank" rel="noreferrer">
                  localhost:8025
                </a>
                .
              </p>
            )}
            <div className="actions">
              <button type="button" className="btn" onClick={() => setSent({ state: "idle" })}>
                Use another address
              </button>
            </div>
          </div>
        ) : (
          <form onSubmit={submit}>
            <p className="muted">
              We email you a link to sign in: there's no password. Alerts about the areas you watch
              go to the same address.
              {inviteOnly &&
                " This Orbit Diff is by invitation: ask its owner to add your address."}
            </p>
            <label htmlFor="email">Email</label>
            <div className="field-row">
              <input
                id="email"
                className="input"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                aria-describedby={error ? "email-error" : undefined}
              />
              <button type="submit" className="btn btn-primary" disabled={sent.state === "sending"}>
                {sent.state === "sending" ? "Sending…" : "Email me a link"}
              </button>
            </div>
            {error && (
              <p id="email-error" className="error-text small" role="alert">
                {error}
              </p>
            )}
          </form>
        )}
      </div>
    </main>
  );
}
