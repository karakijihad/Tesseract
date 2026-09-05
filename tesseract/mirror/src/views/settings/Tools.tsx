import { Select, type SelectTone } from "../../components/common/Select";
import { Note } from '../../components/common/Note';
import { useEffect, useMemo, useState } from 'react';

import { applyToolPermission, useToolsStore, type Posture } from '../../stores/tools';
import { postResetDefaults } from '../../lib/api';
import { ResetDefaults } from '../../components/common/ResetDefaults';
import { Hint } from '../../components/ui/Hint';
import { Tabs, type TabItem } from '../../components/common/Tabs';
import { DataTable } from '../../components/common/DataTable';
import { BashRulesSection } from './BashRules';

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

type Pane = "glossary" | "bash";

/** What the assistant may reach for, and the one list nothing can talk its
 *  way past. Both are "tools", and they answer opposite questions, so they
 *  are two tabs rather than one scroll. */
const PANES: readonly TabItem<Pane>[] = [
  { key: "glossary", label: "Glossary" },
  { key: "bash", label: "Bash" },
];

const TOOL_COLUMNS = [
  { label: "tool", width: "200px" },
  { label: "description", width: "minmax(0, 1fr)" },
  { label: "posture · without the mode", width: "200px" },
];

export function ToolsSection() {
  const [pane, setPane] = useState<Pane>("glossary");
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
      // Refetch rather than assume. The route writes to whichever layer the
      // active mode decides in, and path rules sit above all of them, so what
      // this tool now resolves to is the policy's answer and not one this
      // component can compute.
      await load(true);
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : 'tool-permission update failed');
    } finally {
      setSavingName(null);
    }
  };

  const displayedError = localError || error;

  if (pane === "bash") {
    return (
      <section className="settings-section">
        <Tabs items={PANES} active={pane} onSelect={setPane} label="Tool panes" />
        <BashRulesSection />
      </section>
    );
  }

  return (
    <section className="settings-section">
      <Tabs items={PANES} active={pane} onSelect={setPane} label="Tool panes" />
      <Note>
        {tools ? `${tools.length} tools` : '…'}{mode ? `, mode: ${mode}` : ''}
      </Note>
      <>
          <Note>
            Set what each tool does before it runs. The choice is saved wherever
            the current mode decides it: in the mode's own list when the mode
            has an opinion, in the main list otherwise, and for your own tools
            beside them in your tools folder. The badge beside a control is
            what that tool would be with the current mode taken out of it.
            Path-sensitive tools are decided per call by the path they are given,
            and the checks in `bash_security.py` refuse some commands whatever is
            set here.
          </Note>
          {displayedError && <Note tone="bad">{displayedError}</Note>}
          <div className="cost-row cost-row--actions">
            <ResetDefaults
              run={() => postResetDefaults("tools")}
              reach="the main list, back to the values your version came with. A choice saved under the current mode stays, and so do the path rules."
              onDone={() => void load(true)}
            />
          </div>
          <DataTable
            label="Tools and their postures"
            columns={TOOL_COLUMNS}
            rows={sorted.map((t) => {
              // `permission` is what the runtime resolves this tool to under
              // the current mode, and it is also the value a save now changes,
              // because the writer edits whichever layer decides. The selector
              // has to show THAT, or an operator's own choice disappears from
              // the control the moment the panel refetches.
              //
              // The other value is this tool's answer with the mode taken out
              // of it. NOT what it shipped as: it reads `permissions.yaml`'s
              // own `tools:` block, which is also where a save lands under a
              // mode that states nothing, so once the tool has been set by
              // hand there is no record left of what it arrived as. Calling it
              // `shipped` said something the runtime cannot know.
              const inForce = t.permission as Posture;
              const outsideMode = t.default_posture as Posture;
              // Suppressed when the mode chip is already on the row: two marks
              // for one fact was the complaint that the panel says a thing
              // twice, and the chip names the cause where this names only the
              // value.
              const differs = inForce !== outsideMode && !t.mode_override;
              return {
                key: t.name,
                cells: [
                  <span className="tool-table__name">
                    {t.name}
                    {t.origin === 'custom' && (
                      <Hint label="you wrote this one. It lives in your tools folder, it is not part of TESSERACT, and deleting the file removes it. Its posture is recorded alongside it rather than in the shipped configuration, and a security mode never relaxes it for you">
                        <span className="tool-table__tag t-meta">custom</span>
                      </Hint>
                    )}
                    {t.path_sensitive && (
                      <Hint label="what this is allowed to do changes with the path it is given">
                        <span className="tool-table__tag t-meta">path</span>
                      </Hint>
                    )}
                    {t.mode_override && (
                      <Hint label="the current mode is overriding this">
                        <span className="tool-table__tag t-meta">mode</span>
                      </Hint>
                    )}
                  </span>,
                  <Hint label={t.description}>
                    <span className="tool-table__desc">{t.description}</span>
                  </Hint>,
                  <span className="tool-table__posture">
                    {/* Custom tools are editable here too. Their answer is
                        recorded in the operator's own tools folder rather
                        than in the shipped configuration, so a name that
                        exists on one computer still never reaches the file
                        every install receives. */}
                    <ToolPermissionSelect
                      name={t.name}
                      defaultPosture={inForce}
                      saving={savingName === t.name}
                      onChange={(posture) => onChange(t.name, posture)}
                    />
                    {differs && (
                      <Hint label={`without the current mode this tool would be ${outsideMode.toUpperCase()}`}>
                        <span
                          className={`tool-row__effective tool-row__effective--${outsideMode}`}
                        >
                          {outsideMode.toUpperCase()}
                        </span>
                      </Hint>
                    )}
                  </span>,
                ],
              };
            })}
          />
      </>
    </section>
  );
}
