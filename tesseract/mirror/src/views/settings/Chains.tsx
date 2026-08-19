import { useMemo, useState } from "react";

import { Button } from "../../components/common/Button";
import { Note } from "../../components/common/Note";
import { Select } from "../../components/common/Select";
import { Hint } from "../../components/ui/Hint";
import { fetchCatalog, fetchChains, postChain } from "../../lib/api";
import type {
  CatalogEntry,
  CatalogResponse,
  Chain,
  ChainEntry,
  ChainsResponse,
} from "../../lib/types";
import { useCachedFetch } from "../../lib/useCachedFetch";

function followers(chain: Chain): string {
  if (chain.used_by.length === 0) return "followed by nothing";
  if (chain.used_by.length === 1) return `followed by ${chain.used_by[0]}`;
  return `followed by ${chain.used_by.length} roles — ${chain.used_by.join(", ")}`;
}

function entryLabel(entry: CatalogEntry): string {
  const ctx = entry.context_window
    ? `  ${Math.round(entry.context_window / 1000)}k`
    : "";
  const tags = entry.good_for?.length ? `  ·  ${entry.good_for.join(" ")}` : "";
  return `${entry.model}  ·  ${entry.tier}.${entry.provider}${ctx}${tags}`;
}

/** What the entry's status column says, or null when there is nothing to say.
 *
 * Two failures live on this row and they are not the same repair: an
 * unresolvable ref is already reported beside the model ("not in the
 * catalog"), so only a RESOLVED entry reaches here. A backend older than the
 * `available` field sends nothing, and the column stays blank rather than
 * inventing a state for it.
 *
 * Glyph-only inside `t-meta`, matching how Capabilities reports a provider —
 * a status colour of its own would be a second palette for one column.
 */
function statusOf(entry: ChainEntry): { ok: boolean; text: string } | null {
  if (!entry.resolved || entry.available === undefined) return null;
  if (entry.available) return { ok: true, text: "ready" };
  // The reason comes from the runtime and names the flag that is false. The
  // fallback string is for a backend that sent `available: false` with no
  // sentence — still truer than reporting ready.
  return { ok: false, text: entry.reason ?? "unavailable" };
}

export function ChainsSection() {
  const {
    data: chains,
    error,
    setError,
    set: setChains,
  } = useCachedFetch<ChainsResponse>("settings.chains", fetchChains);
  const { data: catalog } = useCachedFetch<CatalogResponse>(
    "settings.catalog",
    fetchCatalog,
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [adding, setAdding] = useState<Record<string, string>>({});

  const entriesByKind = useMemo(() => {
    const out = new Map<string, CatalogEntry[]>();
    for (const entry of catalog?.entries ?? []) {
      const list = out.get(entry.kind) ?? [];
      list.push(entry);
      out.set(entry.kind, list);
    }
    for (const list of out.values()) {
      list.sort((a, b) => a.model.localeCompare(b.model));
    }
    return out;
  }, [catalog]);

  const write = async (name: string, refs: string[]) => {
    setBusy(name);
    setError(null);
    try {
      await postChain({ name, refs });
      setChains(await fetchChains());
    } catch (err) {
      setError(err instanceof Error ? err.message : "chain update failed");
    } finally {
      setBusy(null);
    }
  };

  const remove = async (chain: Chain) => {
    setBusy(chain.name);
    setError(null);
    try {
      await postChain({ name: chain.name, delete: true });
      setChains(await fetchChains());
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not delete the chain");
    } finally {
      setBusy(null);
    }
  };

  if (!chains) {
    return (
      <section className="settings-section">
        <div className="t-meta">(loading…)</div>
        {error && <Note tone="bad">{error}</Note>}
      </section>
    );
  }

  return (
    <section className="settings-section">
      <Note>
        A chain is a failover order: the first entry serves, and each one below
        it is tried when the one above fails. Roles follow a chain rather than
        naming models of their own — so editing a chain here moves every role
        listed under it. To move one role alone, give it a different chain in
        Model roles. Each entry says whether it could be built right now &mdash;
        one that cannot is skipped at failover rather than raised, so a chain
        can be healthy with an entry off.
      </Note>
      {error && <Note tone="bad">{error}</Note>}

      {chains.chains.map((chain) => {
        const refs = chain.entries.map((e) => e.ref);
        const saving = busy === chain.name;
        const options = chain.kind ? (entriesByKind.get(chain.kind) ?? []) : [];
        const unused = options.filter((o) => !refs.includes(o.ref));
        const pending = adding[chain.name] ?? "";

        const move = (from: number, to: number) => {
          if (to < 0 || to >= refs.length) return;
          const next = [...refs];
          const [moved] = next.splice(from, 1);
          next.splice(to, 0, moved);
          void write(chain.name, next);
        };

        return (
          <div key={chain.name} className="chain-card">
            <div className="chain-card__head">
              <span className="chain-card__name">{chain.name}</span>
              <span className="chain-card__kind t-meta">
                {chain.kind ?? "unresolved"}
              </span>
              <span className="chain-card__users t-meta">{followers(chain)}</span>
              <Hint
                label={
                  chain.used_by.length
                    ? "a chain a role still follows cannot be deleted"
                    : "nothing follows this chain"
                }
              >
                <Button
                  tone="danger"
                  onClick={() => void remove(chain)}
                  disabled={saving || chain.used_by.length > 0}
                >
                  delete
                </Button>
              </Hint>
            </div>

            <ol className="chain-card__list">
              {chain.entries.map((entry, i) => {
                const status = statusOf(entry);
                return (
                <li key={entry.ref} className="chain-entry">
                  <span className="chain-entry__rank t-meta">
                    {i === 0 ? "serves" : `fallback ${i}`}
                  </span>
                  <span className="chain-entry__model">
                    {entry.resolved ? entry.model : entry.ref}
                    {!entry.resolved && (
                      <span className="chain-entry__broken t-meta">
                        {" "}
                        — not in the catalog
                      </span>
                    )}
                  </span>
                  <span className="chain-entry__meta t-meta">
                    {entry.resolved ? `${entry.tier}.${entry.provider}` : ""}
                  </span>
                  {/* Where the model's tags used to sit. A tag informs the
                      choice and is still on every option in the picker below;
                      on a row already chosen, whether it can be BUILT is the
                      fact worth the column. A reason runs long, so the cell
                      truncates and the hover carries all of it. */}
                  <span className="chain-entry__status t-meta">
                    {status === null ? null : status.ok ? (
                      <>● ready</>
                    ) : (
                      <Hint label={status.text}>
                        <span>○ {status.text}</span>
                      </Hint>
                    )}
                  </span>
                  <span className="chain-entry__actions">
                    <Button onClick={() => move(i, i - 1)} disabled={saving || i === 0}>
                      ↑
                    </Button>
                    <Button
                      onClick={() => move(i, i + 1)}
                      disabled={saving || i === chain.entries.length - 1}
                    >
                      ↓
                    </Button>
                    <Hint
                      label={
                        refs.length === 1
                          ? "a chain needs at least one entry"
                          : "remove from this chain"
                      }
                    >
                      <Button
                        onClick={() =>
                          void write(
                            chain.name,
                            refs.filter((r) => r !== entry.ref),
                          )
                        }
                        disabled={saving || refs.length === 1}
                      >
                        remove
                      </Button>
                    </Hint>
                  </span>
                </li>
                );
              })}
            </ol>

            <div className="chain-card__add">
              <Select
                value={pending}
                disabled={saving || unused.length === 0}
                ariaLabel={`add a model to ${chain.name}`}
                onChange={(v) => setAdding((s) => ({ ...s, [chain.name]: v }))}
                options={[
                  { value: "", label: "add a fallback…" },
                  ...unused.map((o) => ({ value: o.ref, label: entryLabel(o) })),
                ]}
              />
              <Button
                onClick={() => {
                  void write(chain.name, [...refs, pending]);
                  setAdding((s) => ({ ...s, [chain.name]: "" }));
                }}
                disabled={saving || !pending}
              >
                add
              </Button>
            </div>
          </div>
        );
      })}
    </section>
  );
}
