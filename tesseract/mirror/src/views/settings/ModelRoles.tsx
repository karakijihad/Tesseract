import { useEffect, useMemo, useState } from "react";

import { fetchCatalog, postModelRef, postRoleModels } from "../../lib/api";
import type {
  CatalogEntry,
  CatalogResponse,
  CatalogTargetMeta,
  IdentityRoleStatus,
  ModelRefTarget,
  ProviderModelKind,
} from "../../lib/types";
import { useIdentityStore } from "../../stores/identity";
import { useWebSocketStore } from "../../stores/websocket";

function ctxLabel(ctx: number | undefined): string {
  if (!ctx) return "—";
  return `${Math.round(ctx / 1000)}k`;
}

function optionLabel(entry: CatalogEntry): string {
  // Format: "<model>  ·  <tier>.<provider>  ·  <ctx>"
  const ctx = entry.context_window
    ? `  ${Math.round(entry.context_window / 1000)}k`
    : "";
  return `${entry.model}  ·  ${entry.tier}.${entry.provider}${ctx}`;
}

export function ModelRolesSection() {
  const roles = useIdentityStore((s) => s.roles);
  const fetchIdentity = useIdentityStore((s) => s.fetchIdentity);

  const [catalog, setCatalog] = useState<CatalogResponse | null>(null);
  const [savingTarget, setSavingTarget] = useState<ModelRefTarget | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reloadCatalog = async () => {
    try {
      const c = await fetchCatalog();
      setCatalog(c);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load catalog");
    }
  };

  // Re-runs on every WS (re)connection: a backend restart must replace a
  // pre-restart "Failed to fetch" with fresh data (2026-07-30).
  const wsGeneration = useWebSocketStore((s) => s.generation);
  useEffect(() => {
    void reloadCatalog();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsGeneration]);

  const targetRows: CatalogTargetMeta[] = useMemo(
    () => catalog?.targets ?? [],
    [catalog],
  );

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
        <h3 className="settings-section__title">Model roles</h3>
        <div className="t-meta">(loading…)</div>
        {error && <div className="settings-error">{error}</div>}
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

  const toggleMode = async (
    target: ModelRefTarget,
    currentMode: "active" | "disabled",
  ) => {
    setSavingTarget(target);
    setError(null);
    try {
      await postRoleModels({
        role: target,
        mode: currentMode === "active" ? "disabled" : "active",
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
      <h3 className="settings-section__title">Model roles</h3>
      <div className="role-banner" role="status">
        Takes effect immediately — live sessions hot-swap to the new adapter on
        save.
      </div>
      <div className="settings-hint t-meta">
        Each row writes a single ref into roles.yaml; catalog is sourced from
        providers.yaml. Edit either file on disk and the picker reflects it.
      </div>
      {error && <div className="settings-error">{error}</div>}
      <div className="role-table">
        <div className="role-table__head t-meta">
          <span>target</span>
          <span>model</span>
          <span>provider</span>
          <span>context</span>
          <span>status</span>
        </div>
        {targetRows.map(
          ({ target, allow_toggle, load_bearing, mode: serverMode }) => {
            const label = target;
            const currentRef = catalog.current[target];
            const options = optionsByTarget.get(target) ?? [];
            const head = options.find((e) => e.ref === currentRef);
            const saving = savingTarget === target;

            // Identity store carries the live mode for chat-style roles; for
            // newly-surfaced roles (vision_agent, image_generator, etc.) we
            // fall back to the server-provided mode from the catalog payload.
            const status: IdentityRoleStatus | undefined = allow_toggle
              ? roles?.[target]
              : undefined;
            const mode = (status?.mode || serverMode || "active") as
              | "active"
              | "disabled";
            const isLoadBearing = load_bearing;

            return (
              <div key={target} className="role-table__row">
                <span className="role-table__role">{label}</span>
                <span className="role-table__model">
                  <select
                    className="role-table__select"
                    value={currentRef ?? ""}
                    disabled={saving || options.length === 0}
                    onChange={(e) => void swapRef(target, e.target.value)}
                    aria-label={`${label} model`}
                  >
                    {currentRef && !head && (
                      <option value={currentRef}>
                        {currentRef} (not in catalog)
                      </option>
                    )}
                    {!currentRef && <option value="">(not configured)</option>}
                    {options.map((opt) => (
                      <option key={opt.ref} value={opt.ref}>
                        {optionLabel(opt)}
                      </option>
                    ))}
                  </select>
                </span>
                <span className="role-table__provider t-meta">
                  {head ? `${head.tier}.${head.provider}` : "—"}
                </span>
                <span className="role-table__ctx t-meta">
                  {ctxLabel(head?.context_window)}
                </span>
                <span className="role-table__status">
                  {allow_toggle ? (
                    <button
                      type="button"
                      className={`role-toggle role-toggle--${mode}`}
                      onClick={() => void toggleMode(target, mode)}
                      disabled={saving || (isLoadBearing && mode === "active")}
                      title={
                        isLoadBearing
                          ? "chat_brain is load-bearing — cannot be disabled"
                          : mode === "active"
                            ? "click to disable"
                            : "click to re-enable"
                      }
                    >
                      {mode}
                    </button>
                  ) : (
                    // Lanes (embeddings / voice_stt / voice_tts) have no
                    // disabled state — show a read-only "active" pill so the
                    // status column stays consistent across all rows.
                    // role="img" + aria-label makes screen readers announce
                    // it as informational rather than a non-functional control.
                    <span
                      className="role-toggle role-toggle--active"
                      role="img"
                      aria-label="always active"
                      title="always active — addressed directly by the runtime"
                    >
                      active
                    </span>
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
