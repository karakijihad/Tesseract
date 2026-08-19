import { Select } from "../../components/common/Select";
import { Note } from "../../components/common/Note";
import { Chip } from "../../components/common/Chip";
import { useMemo, useState } from "react";

import {
  fetchCatalog,
  fetchChains,
  postModelRef,
  postRoleChain,
  postRoleModels,
} from "../../lib/api";
import type {
  CatalogEntry,
  CatalogResponse,
  CatalogTargetMeta,
  Chain,
  ChainsResponse,
  IdentityRoleStatus,
  ModelRefTarget,
  ProviderModelKind,
  RoleMode,
} from "../../lib/types";
import { useIdentityStore } from "../../stores/identity";
import { useCachedFetch } from "../../lib/useCachedFetch";
import { Hint } from '../../components/ui/Hint';

function ctxLabel(ctx: number | undefined): string {
  if (!ctx) return "—";
  return `${Math.round(ctx / 1000)}k`;
}

function optionLabel(entry: CatalogEntry): string {
  // Format: "<model>  ·  <tier>.<provider>  ·  <ctx>  ·  <what it is for>"
  // The tags ride the option rather than a line beneath the select, because
  // the moment they answer a question is while the list is open — a CLI ref
  // that cannot call tools is worth seeing before it is picked, not after.
  const ctx = entry.context_window
    ? `  ${Math.round(entry.context_window / 1000)}k`
    : "";
  const goodFor = entry.good_for?.length
    ? `  ·  ${entry.good_for.join(" ")}`
    : "";
  return `${entry.model}  ·  ${entry.tier}.${entry.provider}${ctx}${goodFor}`;
}

function chainLabel(chain: Chain): string {
  // Deliberately not naming the model it serves: the very next column already
  // shows that, and repeating it is what pushed the label out of the cell.
  const depth =
    chain.entries.length === 1
      ? "no fallback"
      : `+${chain.entries.length - 1} fallback${chain.entries.length === 2 ? "" : "s"}`;
  return `${chain.name}  ·  ${depth}`;
}

export function ModelRolesSection() {
  const roles = useIdentityStore((s) => s.roles);
  const fetchIdentity = useIdentityStore((s) => s.fetchIdentity);

  const {
    data: catalog,
    error,
    setError,
    set: setCatalog,
  } = useCachedFetch<CatalogResponse>("settings.catalog", fetchCatalog);
  const { data: chainData, set: setChains } = useCachedFetch<ChainsResponse>(
    "settings.chains",
    fetchChains,
  );
  const [savingTarget, setSavingTarget] = useState<ModelRefTarget | null>(null);

  const reloadCatalog = async () => {
    try {
      setCatalog(await fetchCatalog());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load catalog");
    }
  };

  const targetRows: CatalogTargetMeta[] = useMemo(
    () => catalog?.targets ?? [],
    [catalog],
  );

  // Which chain each role follows, derived from the chains payload rather than
  // asked for per role — `used_by` is already the authoritative direction and
  // inverting it here keeps one source.
  const chainOfRole = useMemo(() => {
    const out = new Map<string, Chain>();
    for (const chain of chainData?.chains ?? []) {
      for (const role of chain.used_by) out.set(role, chain);
    }
    return out;
  }, [chainData]);

  const optionsByTarget = useMemo(() => {
    const out = new Map<ModelRefTarget, CatalogEntry[]>();
    if (!catalog?.entries) return out;
    for (const row of targetRows) {
      const allowed = new Set<ProviderModelKind>(row.allowed_kinds);
      // No allowed kinds means "no current primary, accept anything" — the
      // backend mirrors this behavior in set_model_ref. Show all entries
      // so the operator can configure the role for the first time.
      const entries = catalog.entries
        .filter((e) => allowed.size === 0 || allowed.has(e.kind))
        .sort((a, b) => {
          if (a.tier !== b.tier) return a.tier.localeCompare(b.tier);
          if (a.provider !== b.provider)
            return a.provider.localeCompare(b.provider);
          return a.model.localeCompare(b.model);
        });
      out.set(row.target, entries);
    }
    return out;
  }, [catalog, targetRows]);

  if (!catalog?.entries || !catalog?.targets) {
    return (
      <section className="settings-section">
        <div className="t-meta">(loading…)</div>
        {error && <Note tone="bad">{error}</Note>}
      </section>
    );
  }

  const swapRef = async (target: ModelRefTarget, ref: string) => {
    if (catalog.current[target] === ref) return;
    setSavingTarget(target);
    setError(null);
    try {
      await postModelRef({ target, ref });
      await reloadCatalog();
      await fetchIdentity();
    } catch (err) {
      setError(err instanceof Error ? err.message : "model-ref update failed");
    } finally {
      setSavingTarget(null);
    }
  };

  const swapChain = async (target: ModelRefTarget, chain: string) => {
    if (chainOfRole.get(target)?.name === chain) return;
    setSavingTarget(target);
    setError(null);
    try {
      await postRoleChain({ role: target, chain });
      setChains(await fetchChains());
      await reloadCatalog();
      await fetchIdentity();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not move the role");
    } finally {
      setSavingTarget(null);
    }
  };

  const toggleMode = async (target: ModelRefTarget, currentMode: RoleMode) => {
    setSavingTarget(target);
    setError(null);
    try {
      await postRoleModels({
        role: target,
        mode: currentMode === "active" ? "inactive" : "active",
      });
      await fetchIdentity();
      await reloadCatalog();
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "role-models update failed",
      );
    } finally {
      setSavingTarget(null);
    }
  };

  return (
    <section className="settings-section">
      <Note tone="warn">
        Takes effect immediately — live sessions hot-swap to the new adapter on
        save.
      </Note>
      <Note>
        A role follows a chain, and the chain decides which model serves it and
        what it falls back to. Changing a role here moves that role alone; to
        change the models themselves — or move every role that shares a chain —
        edit the chain in Chains.
      </Note>
      {error && <Note tone="bad">{error}</Note>}
      <div className="role-table">
        <div className="role-table__head t-meta">
          <span>target</span>
          <span>chain</span>
          <span>model</span>
          <span>provider</span>
          <span>context</span>
          <span>status</span>
        </div>
        {targetRows.map(
          ({ target, allow_toggle, load_bearing, mode: serverMode, allowed_kinds }) => {
            const label = target;
            const currentRef = catalog.current[target];
            const options = optionsByTarget.get(target) ?? [];
            const head = options.find((e) => e.ref === currentRef);
            const saving = savingTarget === target;
            const chain = chainOfRole.get(target);

            // Only roles follow chains. `embeddings` and the voice lanes live
            // under their own keys in roles.yaml and still name a ref directly,
            // so they keep the model picker.
            const compatible = (chainData?.chains ?? []).filter(
              (c) =>
                c.kind !== null &&
                (allowed_kinds.length === 0 ||
                  allowed_kinds.includes(c.kind as ProviderModelKind)),
            );

            // Identity store carries the live mode for chat-style roles; for
            // newly-surfaced roles (vision_agent, image_generator, etc.) we
            // fall back to the server-provided mode from the catalog payload.
            const status: IdentityRoleStatus | undefined = allow_toggle
              ? roles?.[target]
              : undefined;
            const mode = (status?.mode || serverMode || "active") as RoleMode;
            const isLoadBearing = load_bearing;

            return (
              <div key={target} className="role-table__row">
                <span className="role-table__role">{label}</span>
                <span className="role-table__chain">
                  {chain ? (
                    <Hint
                      label={
                        chain.used_by.length > 1
                          ? `${chain.name} is shared with ${chain.used_by
                              .filter((r) => r !== target)
                              .join(", ")} — moving this role leaves them on it`
                          : `${chain.name} serves this role alone`
                      }
                    >
                      <Select
                        value={chain.name}
                        disabled={saving || compatible.length === 0}
                        onChange={(v) => void swapChain(target, v)}
                        ariaLabel={`${label} chain`}
                        options={compatible.map((c) => ({
                          value: c.name,
                          label: chainLabel(c),
                        }))}
                      />
                    </Hint>
                  ) : (
                    <span className="t-meta">—</span>
                  )}
                </span>
                <span className="role-table__model">
                  {chain ? (
                    <span className="role-table__resolved">
                      {chain.entries[0]?.model ?? chain.entries[0]?.ref ?? "—"}
                    </span>
                  ) : (
                    <Select
                      value={currentRef ?? ""}
                      disabled={saving || options.length === 0}
                      onChange={(v) => void swapRef(target, v)}
                      ariaLabel={`${label} model`}
                      options={[
                        ...(currentRef && !head
                          ? [{ value: currentRef, label: `${currentRef} (not in catalog)` }]
                          : []),
                        ...(!currentRef ? [{ value: "", label: "(not configured)" }] : []),
                        ...options.map((opt) => ({
                          value: opt.ref,
                          label: optionLabel(opt),
                        })),
                      ]}
                    />
                  )}
                </span>
                <span className="role-table__provider t-meta">
                  {head ? `${head.tier}.${head.provider}` : "—"}
                </span>
                <span className="role-table__ctx t-meta">
                  {ctxLabel(head?.context_window)}
                </span>
                <span className="role-table__status">
                  {allow_toggle ? (
                    <Hint label={isLoadBearing
                          ? "chat_brain is load-bearing — cannot be set inactive"
                          : mode === "active"
                            ? "click to set inactive"
                            : "click to set active"}>
                      <Chip
                        className="role-toggle"
                        tone={mode === "active" ? "good" : "bad"}
                        onClick={() => void toggleMode(target, mode)}
                        disabled={saving || (isLoadBearing && mode === "active")}
                      >
                        {mode}
                      </Chip>
                    </Hint>
                  ) : (
                    // Lanes (embeddings / voice_stt / voice_tts) have no
                    // toggle here — show a read-only "active" pill so the
                    // status column stays consistent across all rows.
                    // role="img" + aria-label makes screen readers announce
                    // it as informational rather than a non-functional control.
                    <Hint label="always active — addressed directly by the runtime">
                      <span
                        className="chip chip--outline chip--good role-toggle"
                        role="img"
                        aria-label="always active"
                      >
                        active
                      </span>
                    </Hint>
                  )}
                </span>
              </div>
            );
          },
        )}
      </div>
    </section>
  );
}
