import { Note } from "../../components/common/Note";
import { useEffect, useState } from "react";

import { isTauri } from "../../lib/endpoints";
import { appInfo, type AppInfo, type Divergence } from "../../lib/update";
import { useUpdateStore } from "../../stores/update";
import { Block } from "../../components/common/Block";
import { DocumentsSection } from "./Documents";
import { ModeSection } from "./Mode";
import { RuntimeSection } from "./Runtime";
import { Button } from "../../components/common/Button";
import { Modal } from "../../components/common/Modal";

// Always-rendered version block (2026-07-29). The old version row lived
// inside SystemSection, whose entire render waited on the backend's
// capability snapshot — backend down meant no version and no update
// button, precisely when they matter most. Everything here comes from the
// Rust shell (app_info IPC + update store), no Python involved.

function fmtDate(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000);
  if (Number.isNaN(d.getTime())) return "unknown";
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

// Full detail for the inline banner: what diverged and the (capped) names.
function describeDivergence(d: Divergence): string {
  const parts: string[] = [];
  if (d.ahead > 0) {
    parts.push(
      `${d.ahead} local commit${d.ahead === 1 ? "" : "s"} (${d.ahead_summaries.join(", ")})`,
    );
  }
  if (d.dirty_total > 0) {
    const more = d.dirty_total - d.dirty.length;
    parts.push(
      `${d.dirty_total} uncommitted change${d.dirty_total === 1 ? "" : "s"} (${d.dirty.join(", ")}${more > 0 ? `, +${more} more` : ""})`,
    );
  }
  return parts.join("; ");
}

// Short summary for the discard-confirmation dialog — counts only, the
// banner above it already spelled out the names.
function divergenceSummary(d: Divergence): string {
  const parts: string[] = [];
  if (d.ahead > 0)
    parts.push(`${d.ahead} local commit${d.ahead === 1 ? "" : "s"}`);
  if (d.dirty_total > 0) {
    parts.push(
      `${d.dirty_total} uncommitted change${d.dirty_total === 1 ? "" : "s"}`,
    );
  }
  return parts.join(" and ");
}

// Where the licence lives, and what the app is built from. The AGPL asks that
// people who use the software can get its source; the app said nothing about
// its own terms until this block existed. The repository is the canonical
// place for both files, so these link out rather than bundling a copy that
// could fall behind.
//
// The copyright line is one line beginning "Copyright" on purpose, link and
// all: `test_no_pii_in_production_tree.py` exempts exactly that shape, so the
// author's name and profile ride inside the notice rather than needing the
// guard widened for them. Do not let a formatter wrap this line. Splitting the
// anchor onto its own line puts the handle outside the exemption and fails
// that test, which is the intended outcome rather than a nuisance.
const REPO = "https://github.com/karakijihad/Tesseract/blob/main";

export function LicenceBlock() {
  return (
    <Block title="Licence">
      <div className="system-grid">
        <div className="system-row">
          <span className="system-label t-meta">licence</span>
          <span className="system-value">GNU Affero General Public License, version 3 or later</span>
        </div>
        <div className="system-row">
          <span className="system-label t-meta">copyright</span>
          <span className="system-value">
            Copyright (C) 2026 TESSERACT. Developed and owned by <a href="https://github.com/karakijihad" target="_blank" rel="noopener noreferrer">Jihad Karaki</a>.
          </span>
        </div>
        <div className="system-row">
          <span className="system-label t-meta">source</span>
          <span className="system-value">
            <span className="system-update-note t-meta">
              You can read, change and share every line that runs on your
              machine. If you run a modified copy as a service other people
              reach over a network, they are entitled to your source.{" "}
            </span>
            <a href={`${REPO}/LICENSE`} target="_blank" rel="noopener noreferrer">
              Read the licence
            </a>
          </span>
        </div>
        <div className="system-row">
          <span className="system-label t-meta">built from</span>
          <span className="system-value">
            <span className="system-update-note t-meta">
              The speech models, voices, wake word and software libraries each
              carry their own terms. One of them, the wake word model, has an
              unsettled licence worth reading before you rely on it
              commercially.{" "}
            </span>
            <a href={`${REPO}/NOTICE.md`} target="_blank" rel="noopener noreferrer">
              Read the notices
            </a>
          </span>
        </div>
      </div>
    </Block>
  );
}

export function AboutSection() {
  const [info, setInfo] = useState<AppInfo | null>(null);
  const behind = useUpdateStore((s) => s.behind);
  const checking = useUpdateStore((s) => s.checking);
  const applying = useUpdateStore((s) => s.applying);
  const error = useUpdateStore((s) => s.error);
  const check = useUpdateStore((s) => s.check);
  const forceApply = useUpdateStore((s) => s.forceApply);
  const divergence = useUpdateStore((s) => s.divergence);
  const storeVersion = useUpdateStore((s) => s.version);
  const exeAvailable = useUpdateStore((s) => s.exeAvailable);
  const exeVersion = useUpdateStore((s) => s.exeVersion);
  const exeApplying = useUpdateStore((s) => s.exeApplying);
  const exeApply = useUpdateStore((s) => s.exeApply);
  const [confirmingDiscard, setConfirmingDiscard] = useState(false);

  // Mount-time re-check (the old System UpdateRow had this; its loss let a
  // provisioning-window error latch forever, 2026-07-30). App.tsx owns the
  // launch check + 6h cadence — this just refreshes state when the panel
  // is opened.
  useEffect(() => {
    void check();
  }, [check]);

  useEffect(() => {
    if (!isTauri()) return;
    let cancelled = false;
    appInfo()
      .then((i) => {
        if (!cancelled) setInfo(i);
      })
      .catch(() => {
        // Shell IPC failed (should not happen in the app) — the row falls
        // back to the update store's version string once a check runs.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const version = info
    ? `${info.semver ?? "?"} (${info.sha})`
    : (storeVersion ?? "—");

  return (
    <section className="settings-section">
      <Block title="Version">
      <div className="system-grid">
        <div className="system-row">
          <span className="system-label t-meta">version</span>
          <span className="system-value">
            <span className="system-version">TESSERACT {version}</span>
          </span>
        </div>
        <div className="system-row">
          <span className="system-label t-meta">released</span>
          <span className="system-value">
            {info ? fmtDate(info.commit_epoch) : "—"}
          </span>
        </div>
        <div className="system-row">
          <span className="system-label t-meta">updates</span>
          <span className="system-value">
            {isTauri() ? (
              <>
                {behind > 0 && !applying && (
                  <span className="system-update-note t-meta">
                    {behind} commit{behind === 1 ? "" : "s"} behind. Apply it from
                    the update chip in the top HUD{" "}
                  </span>
                )}
                {applying && (
                  <span className="system-update-note t-meta">
                    applying update, TESSERACT will restart shortly…{" "}
                  </span>
                )}
                {behind === 0 &&
                  !exeAvailable &&
                  !checking &&
                  !applying &&
                  !error && (
                    <span className="system-update-note t-meta">
                      up to date{" "}
                    </span>
                  )}
                <Button
                  onClick={() => void check()}
                  disabled={checking || applying || exeApplying}
                >
                  {checking ? "checking…" : "check for updates"}
                </Button>
                {error && <Note tone="bad">{error}</Note>}
              </>
            ) : (
              <span className="system-update-note t-meta">
                available in the installed app
              </span>
            )}
          </span>
        </div>
        {exeAvailable && (
          <div className="system-row">
            <span className="system-label t-meta">new version</span>
            <span className="system-value">
              <span className="system-update-note t-meta">
                TESSERACT {exeVersion} is available. It downloads, verifies, and
                restarts the app{" "}
              </span>
              <Button
                onClick={() => void exeApply()}
                disabled={exeApplying}
              >
                {exeApplying ? "downloading…" : "download & restart"}
              </Button>
            </span>
          </div>
        )}
        {divergence && !applying && (
          <div className="system-row">
            <span className="system-label t-meta">divergence</span>
            <span className="system-value">
              <span className="system-update-note t-meta">
                local history has moved away from origin/main.{" "}
                {describeDivergence(divergence)}
              </span>{" "}
              <Button
                onClick={() => setConfirmingDiscard(true)}
              >
                discard local changes & update
              </Button>
            </span>
          </div>
        )}
      </div>
      </Block>
      <LicenceBlock />
      <ModeSection />
      <DocumentsSection />
      <RuntimeSection />
      {confirmingDiscard && divergence && (
        <Modal
          onClose={() => setConfirmingDiscard(false)}
          ariaLabel="discard local changes"
          ariaLabelledBy="update-discard-title"
          className="confirm-modal"
        >
          <h4 id="update-discard-title" className="confirm-modal__title">
            Discard local changes?
          </h4>
          <p className="confirm-modal__body">
            This will permanently discard {divergenceSummary(divergence)} and
            reset to origin/main, then update TESSERACT to the latest version.
            Untracked files on disk are left in place.
          </p>
          <div className="confirm-modal__actions">
            <Button onClick={() => setConfirmingDiscard(false)}>Cancel</Button>
            <Button
              tone="primary"
              onClick={() => {
                setConfirmingDiscard(false);
                void forceApply();
              }}
            >
              Discard &amp; update
            </Button>
          </div>
        </Modal>
      )}
    </section>
  );
}
