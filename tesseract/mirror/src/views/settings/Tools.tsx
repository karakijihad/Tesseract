import { Select, type SelectTone } from "../../components/common/Select";
import { Note } from '../../components/common/Note';
import { useEffect, useMemo, useState } from 'react';

import { applyToolPermission, useToolsStore, type Posture } from '../../stores/tools';
import { postResetDefaults } from '../../lib/api';
import { ResetDefaults } from '../../components/common/ResetDefaults';
import { Hint } from '../../components/ui/Hint';

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
    <Select
      value={defaultPosture}
      options={POSTURE_OPTIONS}
      onChange={(v) => onChange(v as Posture)}
      disabled={saving}
      tone={POSTURE_TONE[defaultPosture]}
      ariaLabel={`${name} default posture`}
    />
  );
}

const POSTURE_OPTIONS = [
  { value: "auto", label: "AUTO" },
  { value: "ask", label: "ASK" },
  { value: "deny", label: "DENY" },
];

// The posture IS the state: green runs, amber asks, red refuses.
const POSTURE_TONE: Record<Posture, SelectTone> = {
  auto: "ok",
  ask: "warn",
  deny: "bad",
};

export function ToolsSection() {
  const tools = useToolsStore((s) => s.tools);
  const mode = useToolsStore((s) => s.mode);
  const error = useToolsStore((s) => s.error);
  const refreshTick = useToolsStore((s) => s.refreshTick);
  const load = useToolsStore((s) => s.load);
  const [savingName, setSavingName] = useState<string | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  // The section only mounts when its rail row is chosen, so the deferred
  // load the collapse used to provide is now the mount itself.
  useEffect(() => {
    void load();
  }, [load, refreshTick]);

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
      <Note>
        {tools ? `${tools.length} tools` : '…'}{mode ? ` — mode: ${mode}` : ''}
      </Note>
      <>
          <Note>
            Edit the <strong>default</strong> posture (writes to `permissions.yaml.tools`). The
            <strong> effective</strong> badge is what runs right now — when it differs, the current
            mode is overriding the default. Path-sensitive tools resolve per-call against
            `path_overrides`. Hard DENY in `bash_security.py` always wins.
          </Note>
          {displayedError && <Note tone="bad">{displayedError}</Note>}
          <div className="cost-row cost-row--actions">
            <ResetDefaults
              run={() => postResetDefaults("tools")}
              reach="every default posture below — mode and path overrides are separate blocks and are not touched"
              onDone={() => void load(true)}
            />
          </div>
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
                      <Hint label="actual posture varies per call by path">
                        <span className="tool-table__tag t-meta">
                          path
                        </span>
                      </Hint>
                    )}
                    {t.mode_override && (
                      <Hint label="overridden by current mode">
                        <span className="tool-table__tag t-meta">
                          mode
                        </span>
                      </Hint>
                    )}
                  </span>
                  <Hint label={t.description}>
                    <span className="tool-table__desc">{t.description}</span>
                  </Hint>
                  <span className="tool-table__posture">
                    <ToolPermissionSelect
                      name={t.name}
                      defaultPosture={def}
                      saving={savingName === t.name}
                      onChange={(posture) => onChange(t.name, posture)}
                    />
                    {differs && (
                      <Hint label={`current mode resolves this tool to ${effective.toUpperCase()}`}>
                        <span
                          className={`tool-row__effective tool-row__effective--${effective}`}
                        >
                          → {effective.toUpperCase()}
                        </span>
                      </Hint>
                    )}
                  </span>
                </div>
              );
            })}
          </div>
      </>
    </section>
  );
}
