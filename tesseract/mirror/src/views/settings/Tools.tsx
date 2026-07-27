import { useEffect, useMemo, useState } from 'react';

import { useSettingsStore } from '../../stores/settings';
import { applyToolPermission, useToolsStore, type Posture } from '../../stores/tools';

function ToolPermissionSelect({
  name,
  defaultPosture,
  saving,
  onChange,
}: {
  name: string;
  defaultPosture: Posture;
  saving: boolean;
  onChange: (posture: Posture) => void;
}) {
  return (
    <select
      className={`tool-row__select tool-row__select--${defaultPosture}`}
      value={defaultPosture}
      onChange={(e) => onChange(e.target.value as Posture)}
      disabled={saving}
      aria-label={`${name} default posture`}
    >
      <option value="auto">AUTO</option>
      <option value="ask">ASK</option>
      <option value="deny">DENY</option>
    </select>
  );
}

export function ToolsSection() {
  const collapsed = useSettingsStore((s) => s.collapsedSections['tools']);
  const toggleCollapsed = useSettingsStore((s) => s.toggleCollapsed);
  const tools = useToolsStore((s) => s.tools);
  const mode = useToolsStore((s) => s.mode);
  const error = useToolsStore((s) => s.error);
  const refreshTick = useToolsStore((s) => s.refreshTick);
  const load = useToolsStore((s) => s.load);
  const [savingName, setSavingName] = useState<string | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  useEffect(() => {
    if (collapsed) return;
    void load();
  }, [collapsed, load, refreshTick]);

  const sorted = useMemo(() => {
    if (!tools) return [];
    const rank: Record<string, number> = { deny: 0, ask: 1, auto: 2 };
    return [...tools].sort((a, b) => {
      // Sort by effective posture so DENY/ASK/AUTO grouping reflects what
      // actually happens at execution time (not the editable default).
      const pa = rank[a.permission] ?? 3;
      const pb = rank[b.permission] ?? 3;
      if (pa !== pb) return pa - pb;
      return a.name.localeCompare(b.name);
    });
  }, [tools]);

  const onChange = async (name: string, posture: Posture) => {
    setSavingName(name);
    setLocalError(null);
    try {
      await applyToolPermission(name, posture);
      // Refetch so `permission` (mode-aware effective) reconciles. The route
      // only writes `tools.<name>` (the default); a mode override is still
      // layered on top of that, so we can't compute the new effective value
      // client-side without re-running the policy.
      await load(true);
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : 'tool-permission update failed');
    } finally {
      setSavingName(null);
    }
  };

  const displayedError = localError || error;

  return (
    <section className="settings-section">
      <button
        type="button"
        className="settings-section__title settings-section__toggle t-meta"
        onClick={() => toggleCollapsed('tools')}
        aria-expanded={!collapsed}
      >
        <span className="settings-section__caret">{collapsed ? '▸' : '▾'}</span>
        Tools ({tools ? tools.length : '…'}){mode ? ` — mode: ${mode}` : ''}
      </button>
      {!collapsed && (
        <>
          <div className="settings-hint t-meta">
            Edit the <strong>default</strong> posture (writes to `permissions.yaml.tools`). The
            <strong> effective</strong> badge is what runs right now — when it differs, the current
            mode is overriding the default. Path-sensitive tools resolve per-call against
            `path_overrides`. Hard DENY in `bash_security.py` always wins.
          </div>
          {displayedError && <div className="settings-error">{displayedError}</div>}
          <div className="tool-table">
            <div className="tool-table__head t-meta">
              <span>tool</span>
              <span>description</span>
              <span>default · effective</span>
            </div>
            {sorted.map((t) => {
              const effective = t.permission as Posture;
              const def = t.default_posture as Posture;
              const differs = effective !== def;
              return (
                <div key={t.name} className="tool-table__row">
                  <span className="tool-table__name">
                    {t.name}
                    {t.path_sensitive && (
                      <span className="tool-table__tag t-meta" title="actual posture varies per call by path">
                        path
                      </span>
                    )}
                    {t.mode_override && (
                      <span className="tool-table__tag t-meta" title="overridden by current mode">
                        mode
                      </span>
                    )}
                  </span>
                  <span className="tool-table__desc" title={t.description}>{t.description}</span>
                  <span className="tool-table__posture">
                    <ToolPermissionSelect
                      name={t.name}
                      defaultPosture={def}
                      saving={savingName === t.name}
                      onChange={(posture) => onChange(t.name, posture)}
                    />
                    {differs && (
                      <span
                        className={`tool-row__effective tool-row__effective--${effective}`}
                        title={`current mode resolves this tool to ${effective.toUpperCase()}`}
                      >
                        → {effective.toUpperCase()}
                      </span>
                    )}
                  </span>
                </div>
              );
            })}
          </div>
        </>
      )}
    </section>
  );
}
