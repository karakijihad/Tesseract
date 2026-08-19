import { Note } from "../../components/common/Note";
import { Disclosure } from "../../components/common/Disclosure";
import { useState } from "react";

import { fetchConfigFiles } from "../../lib/api";
import type { ConfigFileEntry, ConfigFilesResponse } from "../../lib/types";
import { useCachedFetch } from "../../lib/useCachedFetch";

function bytesLabel(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
}

export function RawConfigSection() {
  const {
    data: report,
    error,
  } = useCachedFetch<ConfigFilesResponse>(
    "settings.config-files",
    fetchConfigFiles,
  );
  const files: ConfigFileEntry[] | null = report ? report.files : null;
  const [openFiles, setOpenFiles] = useState<Record<string, boolean>>({});


  const toggleFile = (name: string) =>
    setOpenFiles((prev) => ({ ...prev, [name]: !prev[name] }));

  return (
    <section className="settings-section">
      <Note>
        Read-only. Safe-list only — no secrets, no .env, no workspace files.
        {files ? ` ${files.length} files.` : " …"}
      </Note>
      {error && <Note tone="bad">{error}</Note>}
      <div className="raw-config-list">
        {(files ?? []).map((f) => (
          <div key={f.name} className="raw-config-row">
            <Disclosure
              variant="row"
              className="raw-config-row__head"
              onToggle={() => toggleFile(f.name)}
              open={!!openFiles[f.name]}
              disabled={f.missing || f.content === null}
            >
              <span className="raw-config-row__caret">
                {openFiles[f.name] ? "▾" : "▸"}
              </span>
              <span className="raw-config-row__name">{f.path}</span>
              <span className="t-meta">
                {f.missing
                  ? "(missing)"
                  : `${f.lines} lines · ${bytesLabel(f.bytes)}${f.truncated ? " · truncated" : ""}`}
              </span>
            </Disclosure>
            {openFiles[f.name] && f.content !== null && (
              <pre className="raw-config-row__body">{f.content}</pre>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
