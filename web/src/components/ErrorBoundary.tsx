import { Component, type ReactNode } from "react";

/** A screen that throws shows what went wrong and a way back, never a blank page. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <main className="screen screen-state">
        <div className="empty-state" role="alert">
          <h2>This screen stopped working</h2>
          <p className="error-text">{error.message}</p>
          <button type="button" className="btn" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      </main>
    );
  }
}
