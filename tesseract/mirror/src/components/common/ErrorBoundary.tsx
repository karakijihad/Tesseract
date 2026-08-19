import { Component, type ErrorInfo, type ReactNode } from "react";

import { Note } from "./Note";

interface Props {
  children: ReactNode;
  /** Names what failed, so the message says which pane went dark. */
  what: string;
  /** Told what threw, for callers who owe the answer to someone who is not
   * looking at the screen. A canvas surface uses it to report `errored` back
   * to the backend, so `surface_list` can say the card did not draw instead of
   * only that it exists. Runs alongside the console log, never instead of it. */
  onError?: (error: Error) => void;
}

interface State {
  error: Error | null;
}

/** Contains one pane's throw to that pane.
 *
 * React unmounts the WHOLE tree when a render throws with no boundary above
 * it, and the app had none: a settings section reading a field the backend had
 * not shipped yet took the entire window to blank — no orb, no rail, nothing to
 * click back from. The app self-updates, so frontend-ahead-of-backend is a
 * recurring state, not an accident, and a panel that cannot render its payload
 * has to fail as a panel.
 *
 * The message names the error rather than apologising: what threw is the one
 * thing the operator can act on, and a stale backend is the usual answer.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`${this.props.what} failed to render`, error, info);
    this.props.onError?.(error);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <Note tone="bad">
        {this.props.what} could not render — {error.message}. The rest of the
        app is unaffected; if TESSERACT updated recently, restart it so the
        backend and this screen agree.
      </Note>
    );
  }
}
