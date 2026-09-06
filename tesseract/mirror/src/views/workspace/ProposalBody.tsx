// What the runtime proposed to change about itself, and once it has been
// approved, what actually landed. One body for every proposal card: the
// working set, and the tuning kinds beside it.
//
// **This component authors no sentence about a proposal.** Every title and
// every line comes from the job that did the arithmetic
// (`working_set_review.py::_explain`, `runtime_tuning.py::_explain`) or from
// the route that applied it (`routes/workspace.py::_applied_lines`). A threshold lives in
// config, so a wording here describing one would be describing whatever it
// used to be. The only strings this file owns are the two headings for the
// applied block, which are about the decision rather than about the proposal.

import { Note } from '../../components/common/Note';

interface ExplainSection {
  title: string;
  lines: string[];
}

export function ProposalBody({ payload }: { payload: Record<string, unknown> }) {
  const explain = Array.isArray(payload.explain)
    ? (payload.explain as ExplainSection[])
    : [];
  const appliedLines = Array.isArray(payload.applied_lines)
    ? (payload.applied_lines as string[])
    : [];

  if (!explain.length && !appliedLines.length) {
    // The card exists and carries nothing the pane can read. Saying so beats a
    // blank body, which reads as a panel that failed to load.
    return <Note tone="warn">This proposal carries no detail to show.</Note>;
  }

  return (
    <div className="ws-proposal">
      {appliedLines.length > 0 && (
        <section className="ws-proposal__section">
          <h4 className="ws-proposal__title t-meta t-label">What changed</h4>
          <ul className="ws-proposal__lines">
            {appliedLines.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </section>
      )}
      {explain.map((section) => (
        <section key={section.title} className="ws-proposal__section">
          <h4 className="ws-proposal__title t-meta t-label">{section.title}</h4>
          <ul className="ws-proposal__lines">
            {(section.lines || []).map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
