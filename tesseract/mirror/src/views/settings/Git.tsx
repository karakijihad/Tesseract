import { useState } from "react";

import { Block } from "../../components/common/Block";
import { Button } from "../../components/common/Button";
import { DataTable } from "../../components/common/DataTable";
import { Input } from "../../components/common/Input";
import { Note } from "../../components/common/Note";
import { Select } from "../../components/common/Select";
import { Tabs } from "../../components/common/Tabs";
import {
  connectGitIdentity,
  disconnectGitIdentity,
  fetchGitState,
  forgetGitProject,
  openGitProject,
  registerGitProject,
  type GitIdentity,
  type GitProjectRow,
  type GitState,
} from "../../lib/api";
import { useCachedFetch } from "../../lib/useCachedFetch";

type GitTab = "state" | "identity" | "projects";

const TABS = [
  { key: "state" as const, label: "State" },
  { key: "identity" as const, label: "Identity" },
  { key: "projects" as const, label: "Projects" },
];

// The scope a form is aimed at. "" is this machine, so the value can go
// straight into a Select without a second field saying which kind it is.
const MACHINE = "";

interface IdentityDraft {
  scope: string;
  name: string;
  email: string;
  ownAccount: boolean;
  account: string;
  host: string;
  token: string;
}

const BLANK_IDENTITY: IdentityDraft = {
  scope: MACHINE,
  name: "",
  email: "",
  ownAccount: false,
  account: "",
  host: "github.com",
  token: "",
};

// What the form should hold for a scope: whatever is already recorded for it,
// so a reconnect changes what the operator meant to change. Retyping a name to
// get at the token field is how the name silently becomes something else, and
// the row above the form showing the old one makes the retyping look like
// confirmation. The token is never carried, because nothing can read it back.
function draftFor(scope: string, state: GitState): IdentityDraft {
  const entry =
    scope === MACHINE
      ? state.identity
      : (state.projects.find((p) => p.id === scope)?.identity ?? null);
  if (!entry) return { ...BLANK_IDENTITY, scope };
  return {
    ...BLANK_IDENTITY,
    scope,
    name: entry.name,
    email: entry.email,
    ownAccount: entry.credential_ref !== null,
  };
}

// What the tab and the panel head say about the whole page.
//
// Both used to report the MACHINE record alone, so a panel with a project
// connected and no machine default announced "not connected" directly above
// the row saying otherwise. The heading has to describe everything the table
// below it holds, or it is contradicting its own contents.
function identitySummary(state: GitState): string {
  const overrides = state.projects.filter((p) => p.identity).length;
  const named = overrides === 1 ? "1 project" : `${overrides} projects`;
  if (state.identity) {
    return overrides > 0
      ? `commits as ${state.identity.name}, ${named} set separately`
      : `commits as ${state.identity.name}`;
  }
  // Says which folders are covered and which are not, because "not set" on
  // its own left a reader working out what it was not set FOR.
  return overrides > 0
    ? `${named} covered, everywhere else uses your own account`
    : "not connected";
}

// One sentence per identity, and it never claims more than it knows. A record
// naming an account whose token has gone reads as broken here rather than as
// connected, because the push is what would otherwise discover it.
function pushesWith(identity: GitIdentity): string {
  if (identity.credential_ref === null) return "this machine's own sign-in";
  if (identity.credential_ready === false) return "its own account, no token stored";
  if (identity.credential_ready === null) return "its own account, token cannot be checked";
  return "its own account";
}

// Every row says what it is and what it means for the operator, because the
// raw answers do not. `gh auth status` reports "keyring" and "Token scopes"
// to someone who never asked about either, and a scope list is only useful
// next to a sentence saying what it lets the assistant reach.
function stateRows(state: GitState) {
  const rows: { key: string; what: string; value: string; means: string }[] = [];

  rows.push({
    key: "git",
    what: "git",
    value: state.git_installed ? (state.git_version ?? "installed") : "not installed",
    means: state.git_installed
      ? "The assistant can track changes, commit, and switch branches."
      : "Nothing can be committed or published until git is installed.",
  });

  rows.push({
    key: "signin",
    what: "GitHub sign-in",
    value: !state.gh_installed
      ? "the GitHub tool is not installed"
      : state.gh_authenticated
        ? "signed in"
        : "not signed in",
    means: !state.gh_installed
      ? "You can still use git with a remote you set up yourself. The GitHub tool is only needed to create repositories from here."
      : state.gh_authenticated
        ? "Pushing to GitHub will work without asking you for a password."
        : "A push to GitHub will fail on credentials. Run gh auth login in a terminal to fix it.",
  });

  if (state.gh_account) {
    rows.push({
      key: "account",
      what: "account",
      value: state.gh_account,
      means: "Anything published from here appears as this account's work.",
    });
  }

  if (state.gh_protocol) {
    rows.push({
      key: "protocol",
      what: "connection",
      value: state.gh_protocol,
      means: "How this machine talks to GitHub when it sends work there.",
    });
  }

  if (state.gh_scopes.length > 0) {
    rows.push({
      key: "scopes",
      what: "permissions",
      value: state.gh_scopes.join(", "),
      means: "What the saved sign-in is allowed to do on GitHub. Anything not listed here is refused by GitHub itself.",
    });
  }

  return rows;
}

function repoLabel(project: GitProjectRow): string {
  // Missing folder outranks everything else the row could say. A registration
  // whose directory is gone reported "tracked by git" from what was recorded
  // when it was registered, which reads as a healthy project.
  if (!project.root_exists) return "folder not found";
  if (!project.is_repo) return "not tracked by git";
  if (!project.active) return "tracked by git";
  if (project.clean === null) return "tracked, state could not be read";
  return project.clean ? "no uncommitted changes" : "has uncommitted changes";
}

export function GitSection() {
  const {
    data: state,
    error,
    setError,
    set,
  } = useCachedFetch<GitState>("settings.git", () => fetchGitState());
  const [tab, setTab] = useState<GitTab>("state");
  const [refreshing, setRefreshing] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [newRoot, setNewRoot] = useState("");
  const [identity, setIdentity] = useState<IdentityDraft>(BLANK_IDENTITY);

  const refresh = async () => {
    setRefreshing(true);
    setError(null);
    try {
      // The one call that asks this machine again rather than reading what it
      // last said. A button offering to check and serving a cache would be
      // the lie the cache exists to avoid.
      set(await fetchGitState(true));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRefreshing(false);
    }
  };

  // Every mutation re-reads the whole state rather than patching the row it
  // changed: opening a project moves the "(open)" mark off another row, and
  // forgetting the open one clears the selection entirely, so the row the
  // click was on is never the only row that changed.
  const mutate = async (id: string, act: () => Promise<unknown>) => {
    setBusyId(id);
    setError(null);
    try {
      await act();
      set(await fetchGitState());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  };

  const connect = async () => {
    const scope = identity.scope === MACHINE ? null : identity.scope;
    await mutate("__identity__", async () => {
      await connectGitIdentity({
        scope,
        name: identity.name.trim(),
        email: identity.email.trim(),
        credential: identity.ownAccount
          ? {
              account: identity.account.trim(),
              token: identity.token.trim(),
              host: identity.host.trim(),
            }
          : null,
      });
      // The token is dropped from the form the moment the store has it, so a
      // panel left open is not a copy of it.
      setIdentity({ ...identity, token: "" });
    });
  };

  const disconnect = async (scope: string) =>
    mutate(`disconnect:${scope}`, () =>
      disconnectGitIdentity(scope === MACHINE ? null : scope),
    );

  const addProject = async () => {
    const root = newRoot.trim();
    if (!root) return;
    await mutate("__add__", async () => {
      await registerGitProject(root);
      setNewRoot("");
    });
  };

  if (!state) {
    return (
      <section className="settings-section">
        <div className="t-meta">{error ?? "(loading…)"}</div>
      </section>
    );
  }

  const projects = state.projects;
  const activeName = projects.find((p) => p.active)?.name ?? null;

  return (
    <section className="settings-section">
      {error && <Note tone="bad">{error}</Note>}

      <Tabs
        items={[
          { ...TABS[0], badge: state.gh_authenticated ? "signed in" : null },
          {
            ...TABS[1],
            badge: state.identity
              ? state.identity.name
              : projects.some((p) => p.identity)
                ? "some projects"
                : "not set",
          },
          { ...TABS[2], badge: projects.length || null },
        ]}
        active={tab}
        onSelect={(next) => {
          setTab(next);
          // Opening the tab shows what is recorded, but only into a form
          // nobody has started filling in: tabbing away and back must not
          // throw away a token that has been pasted and not yet saved.
          const untouched =
            !identity.name.trim() && !identity.email.trim() && !identity.token.trim();
          if (next === "identity" && untouched) {
            setIdentity(draftFor(identity.scope, state));
          }
        }}
        label="Git settings"
      />

      {tab === "state" ? (
        <Block
          title={null}
          meta={activeName ? `working on ${activeName}` : "no project open"}
        >
          <Note>
            What the assistant can do with version control depends on this
            machine being set up for it. Nothing here changes a setting.
          </Note>
          <DataTable
            label="Version control state"
            columns={[
              { label: "what", width: "120px" },
              { label: "now", width: "180px" },
              { label: "what it means", width: "minmax(0, 1fr)" },
            ]}
            rows={stateRows(state).map((r) => ({
              key: r.key,
              cells: [r.what, r.value, <span className="t-meta">{r.means}</span>],
            }))}
          />
        </Block>
      ) : tab === "identity" ? (
        <Block
          title={null}
          meta={identitySummary(state)}
        >
          <Note>
            Who the assistant is when it commits, and what it pushes with. It
            can use your own account, or one of its own with a token you paste
            here. Either way your own name and your own GitHub sign-in are left
            alone, and nothing is written outside this folder.
          </Note>
          <DataTable
            label="Git identities"
            columns={[
              { label: "applies to", width: "150px" },
              { label: "commits as", width: "minmax(0, 1fr)" },
              { label: "pushes with", width: "200px" },
              { label: "", width: "120px" },
            ]}
            empty={
              <span className="t-meta">
                Nothing is connected, so git behaves as it does everywhere else
                on this machine: your name on the commit, your sign-in on the
                push.
              </span>
            }
            rows={[
              ...(state.identity
                ? [{ scope: MACHINE, label: "this machine", entry: state.identity }]
                : []),
              ...projects
                .filter((p) => p.identity)
                .map((p) => ({
                  scope: p.id,
                  label: p.name,
                  entry: p.identity as GitIdentity,
                })),
            ].map((row) => ({
              key: row.scope || "__machine__",
              cells: [
                row.label,
                <span>
                  {row.entry.name}
                  <span className="t-meta"> {row.entry.email}</span>
                </span>,
                <span className="t-meta">{pushesWith(row.entry)}</span>,
                <Button
                  onClick={() => void disconnect(row.scope)}
                  disabled={busyId !== null}
                  tone="danger"
                  ariaLabel={`Disconnect ${row.label}`}
                >
                  Disconnect
                </Button>,
              ],
            }))}
          />

          <div className="git-form">
            <label className="git-form__field">
              <span className="t-meta">Applies to</span>
              <Select
                value={identity.scope}
                options={[
                  { value: MACHINE, label: "this machine" },
                  ...projects.map((p) => ({ value: p.id, label: p.name })),
                ]}
                onChange={(next) => setIdentity(draftFor(next, state))}
                ariaLabel="Applies to"
                disabled={busyId !== null}
              />
            </label>
            <label className="git-form__field">
              <span className="t-meta">Pushes with</span>
              <Select
                value={identity.ownAccount ? "own" : "machine"}
                options={[
                  {
                    value: "machine",
                    label: state.gh_account
                      ? `your account, ${state.gh_account}`
                      : "this machine's own sign-in",
                  },
                  { value: "own", label: "an account of its own" },
                ]}
                onChange={(next) =>
                  setIdentity({ ...identity, ownAccount: next === "own" })
                }
                ariaLabel="Pushes with"
                disabled={busyId !== null}
              />
            </label>
            <label className="git-form__field">
              <span className="t-meta">Name on the commit</span>
              <Input
                value={identity.name}
                onChange={(next) => setIdentity({ ...identity, name: next })}
                placeholder="the name commits are authored by"
                ariaLabel="Name on the commit"
                disabled={busyId !== null}
              />
            </label>
            <label className="git-form__field">
              <span className="t-meta">Email on the commit</span>
              <Input
                value={identity.email}
                onChange={(next) => setIdentity({ ...identity, email: next })}
                placeholder="git refuses to commit without one"
                ariaLabel="Email on the commit"
                disabled={busyId !== null}
              />
            </label>
            {identity.ownAccount && (
              <>
                <label className="git-form__field">
                  <span className="t-meta">Its username</span>
                  <Input
                    value={identity.account}
                    onChange={(next) => setIdentity({ ...identity, account: next })}
                    placeholder="the login the token belongs to"
                    ariaLabel="Its username"
                    disabled={busyId !== null}
                  />
                </label>
                <label className="git-form__field">
                  <span className="t-meta">Host it may be sent to</span>
                  <Input
                    value={identity.host}
                    onChange={(next) => setIdentity({ ...identity, host: next })}
                    placeholder="github.com"
                    ariaLabel="Host it may be sent to"
                    disabled={busyId !== null}
                  />
                </label>
                <label className="git-form__field git-form__field--wide">
                  <span className="t-meta">Token</span>
                  <Input
                    value={identity.token}
                    onChange={(next) => setIdentity({ ...identity, token: next })}
                    type="password"
                    autoComplete="off"
                    placeholder="paste the token the account signs in with"
                    ariaLabel="Token"
                    disabled={busyId !== null}
                  />
                </label>
              </>
            )}
            <div className="git-form__actions">
              <Button
                onClick={() => void connect()}
                disabled={
                  busyId !== null ||
                  !identity.name.trim() ||
                  !identity.email.trim() ||
                  (identity.ownAccount &&
                    (!identity.account.trim() ||
                      !identity.host.trim() ||
                      !identity.token.trim()))
                }
              >
                Connect
              </Button>
              <span className="t-meta">
                {identity.ownAccount
                  ? "The token is stored encrypted for your Windows sign-in. The assistant never sees it: the runtime puts it into the push at the moment it happens."
                  : "Commits get this name and email. Pushing keeps using whatever this machine is already signed in with."}
              </span>
            </div>
          </div>
        </Block>
      ) : (
        <Block title={null} meta={`${projects.length} registered`}>
          <Note>
            Folders the assistant has been told about. Only the one it is
            working on is checked for unsaved changes, so the others report
            what was recorded when they were registered. Forgetting a project
            removes it from this list and leaves the folder alone.
          </Note>
          <DataTable
            label="Registered projects"
            // The fixed tracks are kept tight on purpose: the settings pane is
            // narrow, and at 160/200/120 the remote column had no room left
            // and clipped its own heading.
            columns={[
              { label: "project", width: "110px" },
              { label: "state", width: "140px" },
              { label: "branch", width: "80px" },
              { label: "publishes to", width: "minmax(0, 1fr)" },
              { label: "", width: "150px" },
            ]}
            empty={
              <span className="t-meta">
                No project is registered yet. Ask the assistant to open one and
                it will appear here.
              </span>
            }
            rows={projects.map((p) => ({
              key: p.id,
              cells: [
                <span>
                  {p.name}
                  {p.active && <span className="t-meta"> (open)</span>}
                </span>,
                repoLabel(p),
                p.branch ?? <span className="t-meta">unknown</span>,
                p.remote ?? (
                  <span className="t-meta">
                    nowhere. Work stays on this machine.
                  </span>
                ),
                <span className="git-row-actions">
                  {/* A missing folder cannot be opened, and offering it would
                      hand the assistant a working directory that is not
                      there. Forgetting stays available, because that is the
                      whole remedy for a row in this state. */}
                  {!p.active && p.root_exists && (
                    <Button
                      onClick={() => void mutate(p.id, () => openGitProject(p.id))}
                      disabled={busyId !== null}
                      ariaLabel={`Open ${p.name}`}
                    >
                      Open
                    </Button>
                  )}
                  <Button
                    onClick={() => void mutate(p.id, () => forgetGitProject(p.id))}
                    disabled={busyId !== null}
                    tone="danger"
                    ariaLabel={`Forget ${p.name}`}
                  >
                    Forget
                  </Button>
                </span>,
              ],
            }))}
          />
          <div className="git-add">
            <Input
              value={newRoot}
              onChange={setNewRoot}
              placeholder="Full path to a folder"
              ariaLabel="Folder to register"
              className="git-add__path"
              disabled={busyId !== null}
            />
            <Button
              onClick={() => void addProject()}
              disabled={busyId !== null || !newRoot.trim()}
            >
              Register
            </Button>
          </div>
        </Block>
      )}

      <Button onClick={refresh} disabled={refreshing}>
        {refreshing ? "Checking…" : "Check again"}
      </Button>
    </section>
  );
}
