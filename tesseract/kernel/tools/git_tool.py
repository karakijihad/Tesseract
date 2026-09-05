"""git — one tool for version control, scoped to a repository on disk.

**One tool, not one per verb.** A second git tool added later is guaranteed
drift, and a narrow `commit`-and-`push` tool is worse than none: the first time
the model needs `checkout` it reaches for `bash` instead, and the gate is back
to reading an opaque command string.

**ASK by default, AUTO in headless, and `push` asks for itself.** The posture
comes from `permissions.yaml` like every other tool, with a headless override
so unattended work is not blocked. `push` additionally requires
`confirm_push=true` in the same call, so the operator sees at the gate that
this one leaves the machine — the reasoning `project_new` already applies to
`create_remote`, for the same reason: a commit is undone by a local command, a
push is not. That flag is checked in the tool rather than by policy, so it
holds in headless too, where nothing would otherwise prompt.

**Who it runs as.** One record in the project registry says who the assistant
commits as and what it pushes with, per project or for the machine. Until the
operator connects one there is none, and git behaves exactly as it always did:
the machine's own name, the machine's own credentials. `_identity_config` is
where that record becomes arguments; there is no branch anywhere below it for
whose account it is.

**Where it may run.** The active project's root by default. A `repo` path may
name somewhere else, but it must be a real git worktree outside the sealed
`app/` and `runtime/` trees, so this cannot be used to edit the application
that is running it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from tesseract.credentials.redaction import redact_url_credentials
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

#: What a remote is called when nothing says otherwise. git's own default,
#: and the fallback `_effective_remote` lands on.
_REMOTE = "origin"

_TIMEOUT_S = 60
# `push` and `pull` reach the network, where a credential prompt or a slow
# remote is the normal failure. Longer than a local call, still bounded: a hang
# here holds the turn open.
_NETWORK_TIMEOUT_S = 180

# Git echoes the remote back on push, remote and clone, so the raw output is a
# credential-bearing string headed for the transcript, the audit log and the
# operator's screen. The rule lives in `credentials/redaction.py`, with the
# four properties it has to hold written out beside it: it is not a git fact,
# and a second copy here is how the prompt ended up with none. Called by its
# own name rather than aliased to `redact`, which in that module means
# something wider.


class GitInput(BaseModel):
    operation: Literal[
        "status", "log", "diff", "show", "branch",
        "init", "add", "commit", "checkout", "remote", "pull", "push",
    ] = Field(description="What to do.")
    repo: str | None = Field(
        default=None,
        description=(
            "Repository directory. Defaults to the active project's root. "
            "Must be outside the sealed app/ and runtime/ trees."
        ),
    )
    message: str | None = Field(default=None, description="Commit message. Required by `commit`.")
    paths: list[str] | None = Field(
        default=None,
        description=(
            "Paths for `add`, relative to the repository. Omit to stage every "
            "change, which is what `add` does by default."
        ),
    )
    branch: str | None = Field(
        default=None, description="Branch name. Required by `checkout`."
    )
    create: bool = Field(
        default=False, description="With `checkout`, create the branch instead of switching to it."
    )
    remote_url: str | None = Field(
        default=None, description="With `remote`, the URL to set as origin."
    )
    ref: str | None = Field(default=None, description="With `show`, the commit or object to display.")
    limit: int = Field(default=20, description="With `log`, how many commits to list.")
    set_upstream: bool = Field(
        default=False, description="With `push`, also set the upstream branch."
    )
    confirm_push: bool = Field(
        default=False,
        description=(
            "Required by `push`. A push publishes to the remote and is not "
            "undone by a local command, so it needs an explicit yes in the "
            "same call. Leave false unless the operator said to push."
        ),
    )


class GitTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "operator_gate"

    group: ClassVar[str] = "projects"
    summary: ClassVar[str] = "Run version control on a repository: inspect it, commit, branch, push."
    use_when: ClassVar[str] = (
        "Use for any git work: reading history or a diff, staging and "
        "committing, switching branches, or publishing to a remote. `push` "
        "needs confirm_push=true in the same call, so ask the operator first."
    )
    not_when: ClassVar[str] = (
        "creating or registering a project, which is `project_new` and "
        "`project_link` — those record what a directory IS, and `project_new` "
        "is what creates a GitHub repository for a new one."
    )
    depends_on: ClassVar[str] = ""

    @property
    def name(self) -> str:
        return "git"

    @property
    def input_schema(self) -> type[BaseModel]:
        return GitInput

    def is_concurrency_safe(self) -> bool:
        # Two git calls in one repository contend on `index.lock`, and the
        # loser fails with a lock error rather than waiting.
        return False

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: GitInput = tool_input  # type: ignore[assignment]

        try:
            repo = self._resolve_repo(inp)
        except ValueError as exc:
            return ToolResult(output=f"git: {exc}", is_error=True)

        try:
            # Resolved once, and the same name is then used to authenticate
            # the command and to write it: anything that resolves the remote
            # twice can resolve it two ways.
            remote = (
                await self._effective_remote(repo, inp.operation)
                if inp.operation in ("push", "pull")
                else _REMOTE
            )
            args = self._argv(
                inp, remote, remote_exists=await self._has_origin(inp, repo)
            )
            config, config_env = await self._identity_config(
                inp.operation, repo, remote
            )
        except ValueError as exc:
            return ToolResult(output=f"git: {exc}", is_error=True)

        timeout = _NETWORK_TIMEOUT_S if inp.operation in ("push", "pull") else _TIMEOUT_S
        code, out, err = await self._run_git(
            args, cwd=repo, timeout=timeout, config=config, config_env=config_env
        )

        body = redact_url_credentials((out + ("\n" + err if err else "")).strip())
        if code != 0:
            return ToolResult(
                output=f"git {inp.operation} failed in {repo}:\n{body or '(no output)'}",
                is_error=True,
            )
        return ToolResult(
            output=f"git {inp.operation} in {repo}\n{body or '(no output)'}",
            metadata={"operation": inp.operation, "repo": str(repo)},
        )

    # --- helpers --------------------------------------------------------

    async def _has_origin(self, inp: GitInput, repo: Path) -> bool:
        """Whether `origin` is already configured, for the `remote` operation.

        Asked only for that operation: every other one pays a config read for
        an answer it does not use. A read that fails answers "no", which sends
        `remote` down the `add` path and lets git report the real problem
        rather than this guessing at one.
        """
        if inp.operation != "remote":
            return False
        code, out, _ = await self._run_git(
            ["config", "--get", "remote.origin.url"], cwd=repo, timeout=_TIMEOUT_S
        )
        return code == 0 and bool(out.strip())

    def _resolve_repo(self, inp: GitInput) -> Path:
        """The directory to run in, refused rather than relocated when sealed.

        `safe_cwd` is deliberately not used here. It silently moves a caller
        into `workshop/`, which is right for a delegate that never chose its
        directory and wrong for this: a git command aimed at one repository
        and quietly run in another reports success for work it did not do.
        """
        from tesseract.orchestrator.projects.store import ProjectStore
        from tesseract.orchestrator.seal_guard import SealViolation, assert_cwd_outside_seal

        if inp.repo:
            root = Path(inp.repo).expanduser()
        else:
            try:
                active = ProjectStore().active()
            except Exception as exc:  # noqa: BLE001 — surfaced to the operator
                raise ValueError(f"project registry unreadable ({exc}); pass repo explicitly") from exc
            if active is None:
                raise ValueError(
                    "no repo given and no active project to infer one from. "
                    "Pass repo, or open a project with project_open."
                )
            root = Path(active.root)

        if not root.is_dir():
            raise ValueError(f"{root} is not a directory")
        try:
            assert_cwd_outside_seal(root)
        except SealViolation as exc:
            raise ValueError(str(exc)) from exc
        if inp.operation != "init" and not (root / ".git").exists():
            raise ValueError(
                f"{root} is not a git repository. Run the `init` operation "
                "there first if it should become one."
            )
        return root

    def _argv(
        self, inp: GitInput, remote: str = _REMOTE, *, remote_exists: bool = False
    ) -> list[str]:
        """The argv for `operation`. Never a shell string, so nothing a caller
        passes can add a second command to it.

        Not a shell is not enough on its own. git parses its own argv, so a
        caller-supplied value that begins with `-` is read as an OPTION however
        the process was spawned, and `show`/`log`/`diff` all accept
        `--output=<file>`, which writes wherever it is pointed and ignores
        `-C`. `show` is the one operation here that takes free text, and it is
        auto-allowed, so a ref is refused rather than escaped. `--` would
        escape it but also break the operation: git then reads the ref as a
        pathspec and shows nothing.
        """
        op = inp.operation
        if (inp.ref or "").startswith("-"):
            raise ValueError(
                "a ref may not start with '-': git would read it as an option, "
                "and some of those write files. Pass the commit or object name."
            )
        if op == "status":
            return ["status", "--short", "--branch"]
        if op == "log":
            return ["log", f"-{max(1, min(inp.limit, 200))}", "--oneline", "--decorate"]
        if op == "diff":
            return ["diff", "--stat", "--patch"]
        if op == "show":
            # `--end-of-options` (git 2.24) is the belt to the refusal's braces:
            # everything after it is a revision whatever it looks like.
            return (
                ["show", "--stat", "--end-of-options", inp.ref]
                if inp.ref else ["show", "--stat"]
            )
        if op == "branch":
            return ["branch", "--all", "--verbose"]
        if op == "init":
            return ["init"]
        if op == "add":
            return ["add", "--", *inp.paths] if inp.paths else ["add", "--all"]
        if op == "commit":
            if not (inp.message or "").strip():
                raise ValueError("commit needs a message")
            return ["commit", "-m", inp.message]
        if op == "checkout":
            if not (inp.branch or "").strip():
                raise ValueError("checkout needs a branch")
            return ["checkout", "-b", inp.branch] if inp.create else ["checkout", inp.branch]
        if op == "remote":
            if not (inp.remote_url or "").strip():
                raise ValueError("remote needs a remote_url")
            # `add` on a name that exists is an error, not an update, so a
            # caller pointing a repository somewhere new had no way through
            # this tool at all and had to reach for the shell. That is how a
            # remote got rewritten in the wrong repository: the shell does not
            # run where this does.
            verb = "set-url" if remote_exists else "add"
            return ["remote", verb, "origin", inp.remote_url]
        if op == "pull":
            return ["pull", "--ff-only"]
        if op == "push":
            if not inp.confirm_push:
                raise ValueError(
                    "push publishes to the remote and is not undone by a local "
                    "command, so it needs confirm_push=true in the same call. "
                    "Ask the operator, then call again."
                )
            # The remote is named by `run`, which is the only place that knows
            # which one this command was authenticated against. `origin` used
            # to be written here, and a branch tracking anything else was then
            # published to a remote nobody had proved an identity to.
            argv = ["push"]
            if inp.set_upstream:
                argv += ["--set-upstream", remote, "HEAD"]
            return argv
        raise ValueError(f"unsupported operation {op!r}")

    async def _identity_config(
        self, operation: str, repo: Path, remote: str | None = None
    ) -> tuple[list[str], dict[str, str]]:
        """What makes this command run as the assistant: argv, and environment.

        Two returns rather than one because they carry different things. The
        name and the address are settings a person could read over your
        shoulder and learn nothing from, so they stay on the command line
        where a failed command is legible. The rule that carries the token
        does not: see `_config_env`.

        Four properties, held at once, because each of them was a live defect
        before this existed and a version that holds three reads as done:

          1. **A commit is authored by whoever the record names**, without
             anyone having to have run `git config` in this repository first.
             That is why it is an argument on every call rather than a setting
             written once: a repository cloned tomorrow is covered by the same
             record with nobody remembering anything.
          2. **`--global` is never touched.** The operator's own name and
             their own `gh` sign-in are exactly as they were.
          3. **The credential is spent, never stored.** It reaches one
             process's environment and no file: no remote is rewritten, no
             helper is installed, and the process that ends takes it with it.
          4. **It fails closed.** A registry that cannot be read, or a
             credential that has gone, refuses the command rather than
             quietly running as the machine's own identity, which is the
             attribution this phase exists to stop.

        Ambient and connected are ONE record and one path here: ambient simply
        has nothing to add for the network operations.
        """
        from tesseract.orchestrator.projects.models import AMBIENT
        from tesseract.orchestrator.projects.store import ProjectStore, ProjectStoreError

        # `None` means resolve it, never "assume origin". `run` passes the one
        # it already resolved, because it needs the same name to write the
        # command; anything else that calls this gets the resolution rather
        # than a default that quietly authenticates the wrong remote, which is
        # the defect this parameter was added to fix.
        if remote is None and operation in ("push", "pull"):
            remote = await self._effective_remote(repo, operation)

        try:
            # Off the loop. The lookup reads a file and resolves a path, and
            # a root on an unmounted share takes as long as the OS takes to
            # give up — with every other panel, heartbeat and inbound turn
            # waiting behind it. The same reason AC-0 moved the panel's own
            # root pass off the loop.
            identity = await asyncio.to_thread(ProjectStore().identity_for_root, repo)
        except ProjectStoreError as exc:
            raise ValueError(
                f"the project registry could not be read ({exc}), so who this "
                f"command would run as is unknown. Nothing was run."
            ) from exc
        if identity is None:
            return [], {}

        config = [
            "-c", f"user.name={identity.name}",
            "-c", f"user.email={identity.email}",
        ]
        if operation not in ("push", "pull") or identity.credential_ref == AMBIENT:
            return config, {}
        extra, env = await self._credential_config(
            identity.credential_ref, repo, operation, remote or _REMOTE
        )
        return config + extra, env

    async def _credential_config(
        self, credential_ref: str, repo: Path, operation: str, remote: str
    ) -> tuple[list[str], dict[str, str]]:
        """One `url.<authenticated>.insteadOf=<origin>` rule per destination.

        Returns the argv fragment and an ENVIRONMENT overlay, and the split is
        the point: the rules carrying the credential go in the environment,
        everything else stays on the command line where being legible costs
        nothing. See `_config_env`.

        Rewriting the URL rather than the remote is what keeps `origin` a
        working remote name: `--set-upstream origin HEAD` still means what it
        says, `pull` is covered by the same line, and nothing on disk ever
        holds the value.

        **Every address the operation will actually use, not the one it is
        filed under.** `push` follows `remote.origin.pushurl` when there is
        one, and git allows SEVERAL of them and publishes to each. A rule
        written for the fetch URL covers none of them, and a rule written for
        the first covers one: the rest go out with whatever git and the OS
        already have for those hosts, which is the machine's account under a
        connected identity's name. So push asks for `--push --all` and every
        distinct host gets its own rule, each one checked against the
        credential's own allowlist on the way.

        **Anything ambiguous is refused rather than guessed at.** A remote
        whose address already carries a sign-in cannot be told apart from one
        carrying a bare username by looking at it, and the second kind
        authenticates through the machine's credential helper with nothing
        said. Both refuse here, and a refusal for any one destination stops
        the whole command: a push that reaches three remotes and can only
        prove who it is to two of them has not been made safe by covering the
        two.
        """
        from urllib.parse import quote, urlsplit

        urls = await self._remote_urls(repo, remote, operation)
        if not urls:
            # No remote to authenticate against. git's own "no configured
            # push destination" is the message worth reading here.
            return [], {}

        netlocs: list[str] = []
        for url in urls:
            parsed = urlsplit(url)
            if parsed.scheme != "https":
                raise ValueError(
                    f"{remote} is {redact_url_credentials(url)}, which is not an https remote, "
                    f"so a stored token cannot authenticate it. Either set "
                    f"it to its https address, or disconnect the account "
                    f"in Settings, Git and let this machine's own keys handle "
                    f"it."
                )
            # `@` in the authority, not `parsed.username`, which is the
            # empty string for `https://@host/` and reads as absent. That
            # shape then fails inside git with a URL error nobody can act on,
            # where this says what to do about it.
            if "@" in parsed.netloc:
                raise ValueError(
                    f"{remote} is {redact_url_credentials(url)}, whose address already carries "
                    f"a sign-in, so it is not clear which account this would "
                    f"use. Nothing was run. Set the remote to the plain "
                    f"address (git remote set-url {remote} "
                    f"https://{parsed.hostname}/OWNER/REPO) and the connected "
                    f"account is used, or disconnect it in Settings, Git to "
                    f"keep using the one in the address."
                )
            if parsed.netloc not in netlocs:
                netlocs.append(parsed.netloc)

        await self._refuse_conflicting_rewrites(repo, urls)
        rules = await asyncio.gather(
            *(
                asyncio.to_thread(self._insteadof, credential_ref, netloc)
                for netloc in netlocs
            )
        )
        # Hooks are the last sink. Config given on the command line or in the
        # environment is handed to every child git starts, so a repository's
        # own `pre-push` script can read the token out of `git config` without
        # touching the store at all, and hook scripts arrive with tooling
        # rather than by anyone deciding to run one. A push carrying a
        # credential runs none of them, which is a real change to what the
        # repository asked for and the smaller of the two surprises.
        return (
            ["-c", "core.hooksPath=" + str(repo / ".git" / "tesseract-no-hooks")],
            _config_env(rules),
        )

    async def _remote_urls(self, repo: Path, remote: str, operation: str) -> list[str]:
        """Every address this operation will really contact, as CONFIGURED.

        Read out of the config rather than through `git remote get-url`, which
        applies the repository's own `insteadOf` rewrites and so reports the
        destination a rule already sends the work to. Authenticating that one
        proves nothing: git applies its best-matching rewrite once, so the
        rule we pass for the rewritten address never fires and the push goes
        out with no sign-in at all. What has to be checked, and refused, is
        the address the repository is configured with.

        `pushurl` replaces `url` for pushing when it is set, and git publishes
        to every entry rather than the first. A fetch uses the first `url` and
        no more.
        """
        async def _values(key: str) -> list[str]:
            code, out, _ = await self._run_git(
                ["config", "--get-all", key], cwd=repo, timeout=_TIMEOUT_S
            )
            if code != 0:
                return []
            return [line.strip() for line in out.splitlines() if line.strip()]

        if operation == "push":
            pushurls = await _values(f"remote.{remote}.pushurl")
            return pushurls or await _values(f"remote.{remote}.url")
        return (await _values(f"remote.{remote}.url"))[:1]

    async def _effective_remote(self, repo: Path, operation: str) -> str:
        """The remote name this operation will actually use.

        git's own order, and the reason it is worth asking rather than
        assuming: `pull` and `push` with no remote named follow the branch's
        configuration, so a branch tracking `upstream` publishes there while
        this was proving an identity to `origin` and letting the machine's own
        credentials answer for the remote it really used.
        """
        code, branch, _ = await self._run_git(
            ["symbolic-ref", "--short", "-q", "HEAD"], cwd=repo, timeout=_TIMEOUT_S
        )
        name = branch.strip() if code == 0 else ""
        keys = ["remote.pushDefault", f"branch.{name}.remote"]
        if operation == "push":
            keys.insert(0, f"branch.{name}.pushRemote")
        for key in keys:
            if name == "" and key.startswith("branch."):
                continue
            got, value, _ = await self._run_git(
                ["config", "--get", key], cwd=repo, timeout=_TIMEOUT_S
            )
            if got == 0 and value.strip():
                return value.strip()
        return _REMOTE

    async def _refuse_conflicting_rewrites(self, repo: Path, urls: list[str]) -> None:
        """Refuse when the repository already rewrites one of these addresses.

        `url.<base>.insteadOf` is matched by longest prefix, so a rule already
        in the repository's config that is more specific than ours replaces
        the destination and drops our credential with it: the push goes to
        whatever that rule names, with no token and no error. It cannot be
        overridden from the command line, so the only honest answer is to stop
        and say what is in the way.
        """
        code, out, _ = await self._run_git(
            ["config", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$"],
            cwd=repo,
            timeout=_TIMEOUT_S,
        )
        if code != 0 or not out.strip():
            return
        for line in out.splitlines():
            key, _, replaced = line.strip().partition(" ")
            target = replaced.strip()
            if not target:
                continue
            for url in urls:
                if url.startswith(target):
                    raise ValueError(
                        f"this repository already rewrites addresses starting "
                        f"{redact_url_credentials(target)} ({key.strip()}), and git prefers the "
                        f"longer rule, so the connected account's sign-in would "
                        f"be dropped and the work sent somewhere else without "
                        f"it. Nothing was run. Remove that rewrite with git "
                        f"config --unset {key.strip()}, or disconnect the "
                        f"account in Settings, Git."
                    )

    def _insteadof(self, credential_ref: str, netloc: str) -> tuple[str, str]:
        """The `(key, value)` that authenticates one host, or a refusal.

        The allowlist check is the store's and happens here, per host, so a
        second push destination the operator never allowed refuses rather than
        quietly falling back to whatever the machine has for it.
        """
        from urllib.parse import quote

        from tesseract.credentials.store import (
            CredentialNotSet,
            CredentialStore,
            CredentialStoreError,
            HostNotAllowed,
            UnknownCredentialError,
        )

        host = netloc.rsplit("@", 1)[-1].rsplit(":", 1)[0].strip("[]")
        try:
            injection, value = CredentialStore().resolve(credential_ref, host)
        except UnknownCredentialError as exc:
            raise ValueError(
                f"this repository publishes as an account whose credential "
                f"({credential_ref}) is no longer in the store, so nothing "
                f"can authenticate. Connect it again in Settings, Git: {exc}"
            ) from exc
        except (CredentialNotSet, HostNotAllowed, CredentialStoreError) as exc:
            raise ValueError(str(exc)) from exc

        if injection.kind != "url":
            raise ValueError(
                f"{credential_ref} is stored as something sent in a "
                f"{injection.kind}, and git authenticates through the remote "
                f"address. Connect the account again in Settings, Git, which "
                f"stores it in the shape git can spend."
            )
        if injection.prefix:
            # A prefix is a header's word, and a URL has nowhere to put one:
            # it would become part of the password and read as a rejected
            # token rather than as the record being wrong.
            raise ValueError(
                f"{credential_ref} has a prefix, and a value spent in a web "
                f"address has nothing to put one in front of, so the sign-in "
                f"would be sent wrong. Clear the prefix on that account in "
                f"Settings, Credentials."
            )

        user = quote(injection.name, safe="")
        secret = quote(value, safe="")
        # A key and a value, not a `-c` string, because the caller has to be
        # able to put this somewhere other than the command line.
        return (
            f"url.https://{user}:{secret}@{netloc}/.insteadOf",
            f"https://{netloc}/",
        )

    async def _run_git(
        self,
        args: list[str],
        *,
        cwd: Path,
        timeout: int,
        config: list[str] | None = None,
        config_env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """Run one git command. Never raises: every failure is a result the
        operator reads, including the timeout.

        `config_env` is configuration that must not appear in the command
        line. It is merged into the child's environment and reaches git as
        exactly the same settings `-c` would have given it.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(cwd), *(config or []), *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # No inherited stdin: a git that decides to prompt for a
                # password would otherwise block until the timeout with the
                # operator seeing nothing to answer.
                stdin=asyncio.subprocess.DEVNULL,
                env={**_no_interactive_env(), **(config_env or {})},
            )
        except (OSError, ValueError) as exc:
            return 1, "", f"could not start git ({exc})"
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 1, "", (
                f"git did not finish within {timeout}s and was stopped. A "
                "network operation may be waiting on credentials that cannot "
                "be entered here."
            )
        return (
            proc.returncode or 0,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"),
        )


def _config_env(rules: "Iterable[tuple[str, str]]") -> dict[str, str]:
    """Git configuration, spelled as environment variables rather than argv.

    **Why not `-c`.** A command line is the most readable thing a process has.
    Any program running as the same user can read it, and on Windows the
    ordinary tools do: Task Manager shows it in a column. An environment block
    needs a handle with `PROCESS_VM_READ` to get at, which is a real step up
    from a column in a list. The token sat in that column for the whole life
    of a network call, which can be a minute on a slow remote.

    **Why this exact form.** `GIT_CONFIG_COUNT` with `GIT_CONFIG_KEY_<n>` and
    `GIT_CONFIG_VALUE_<n>` is git's own way of receiving configuration, added
    in 2.31, and it is the SAME configuration `-c` sets: same parser, same
    precedence, same behaviour. So this is a move rather than a redesign, and
    nothing about how the credential is spent changes.

    A credential helper is the textbook answer and costs more than it buys
    here: `credential.helper=!f() { ... }` is a shell snippet git runs through
    `sh`, which is a dependency this tool does not otherwise have on the
    platform the app ships to, and a helper that cannot run fails as
    "authentication required" with nothing saying why.

    **What it does not claim.** The environment is inherited by children, as
    `-c` was through `GIT_CONFIG_PARAMETERS`, so a hook could still read it.
    That is why the hook path is neutered on any command carrying one, and
    that stays. This narrows where the value is readable; it does not make a
    local credential safe from code running as the operator, which
    `SECURITY.md` says plainly.
    """
    env: dict[str, str] = {}
    count = 0
    for key, value in rules:
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    if not count:
        return {}
    env["GIT_CONFIG_COUNT"] = str(count)
    return env


def _no_interactive_env() -> dict[str, str]:
    """The environment with every interactive credential path disabled.

    Without this a push to a remote the machine has no credentials for opens
    a GUI prompt on the operator's desktop, or blocks on a terminal prompt
    nothing is attached to. Failing with "authentication required" is the
    outcome that can actually be reported.
    """
    import os

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env["GCM_INTERACTIVE"] = "never"
    return env
