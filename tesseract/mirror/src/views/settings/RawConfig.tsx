import { useEffect, useState } from 'react';

import { fetchConfigFiles } from '../../lib/api';
import type { ConfigFileEntry } from '../../lib/types';
import { useSettingsStore } from '../../stores/settings';

function bytesLabel(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
}

export function RawConfigSection() {
  const collapsed = useSettingsStore((s) => s.collapsedSections['raw-config'] ?? true);
  const toggleCollapsed = useSettingsStore((s) => s.toggleCollapsed);
  const [files, setFiles] = useState<ConfigFileEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openFiles, setOpenFiles] = useState<Record<string, boolean>>({});

  useEffect(() => {
    if (collapsed || files) return;
    fetchConfigFiles()
      .then((res) => setFiles(res.files))
      .catch((err) => setError(err instanceof Error ? err.message : 'config-files fetch failed'));
  }, [collapsed, files]);

  const toggleFile = (name: string) =>
    setOpenFiles((prev) => ({ ...prev, [name]: !prev[name] }));

  return (
    <section className="settings-section">
      <button
        type="button"
        className="settings-section__title settings-section__toggle t-meta"
        onClick={() => toggleCollapsed('raw-config')}
        aria-expanded={!collapsed}
      >
        <span className="settings-section__caret">{collapsed ? '▸' : '▾'}</span>
        Raw config ({files ? files.length : '…'})
      </button>
      {!collapsed && (
        <>
          <div className="settings-hint t-meta">Read-only. Safe-list only — no secrets, no .env, no workspace files.</div>
          {error && <div className="settings-error">{error}</div>}
          <div className="raw-config-list">
            {(files ?? []).map((f) => (
              <div key={f.name} className="raw-config-row">
                <button
                  type="button"
                  className="raw-config-row__head"
                  onClick={() => toggleFile(f.name)}
                  aria-expanded={!!openFiles[f.name]}
                  disabled={f.missing || f.content === null}
                >
                  <span className="raw-config-row__caret">{openFiles[f.name] ? '▾' : '▸'}</span>
                  <span className="raw-config-row__name">{f.path}</span>
                  <span className="t-meta">
                    {f.missing
                      ? '(missing)'
                      : `${f.lines} lines · ${bytesLabel(f.bytes)}${f.truncated ? ' · truncated' : ''}`}
                  </span>
                </button>
                {openFiles[f.name] && f.content !== null && (
                  <pre className="raw-config-row__body">{f.content}</pre>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
