---
title: Permissions
description: How the gate decides, who has the last word, and what no setting can relax.
---

The thing the whole system is arranged around.

This page explains the mechanism. It deliberately states no figures and lists
no tools — **[what it asks before doing](../reference/permissions.md)** is
generated from the configuration itself and is the place to look for those.
The split is not tidiness: the hand-written version of those facts was wrong
four times running, always claiming the gate covered more than it did.

## Three postures

| Posture | Meaning |
| --- | --- |
| `AUTO` | runs without asking |
| `ASK` | stops and asks you, every time |
| `DENY` | refused, and no prompt can change that |

## Who decides

`config/permissions.yaml` is the **authority**. A tool declares a posture in
its own source, but that is a starting point rather than a limit: the config
can raise or lower it, and mode- and path-specific rules layer on top. If the
file and the code disagree, the file wins.

Path rules are the ones worth understanding, because they beat the tool. A
tool allowed to write files is still refused when the path is the runtime's own
source, the permissions file itself, or `.env` — the assistant cannot widen its
own reach, and that is enforced by path, not by good behaviour.

## What no configuration can relax

Before policy is consulted at all, command inspection runs a fixed set of
checks. Most are absolute refusals covering attempts to tamper with the audit
trail or attack the runtime itself; those cannot be re-enabled by any setting,
hook, plugin or agent. A smaller set forces a prompt whatever the policy says:
shell evaluation, piping a download straight into a shell, inline interpreter
invocations that reach the OS, scheduling, and recursive destructive commands.

Writes aimed at the sealed application tree are refused ahead of the
verb-based prompts, so a sealed-tree refusal outranks an "are you sure" on the
same command.

## When nobody is there

Anything that would have prompted is refused rather than assumed — the system
does not guess on your behalf. Tools already set to `AUTO` still run, which is
how [autonomy](../anatomy/autonomy.md) gets anything done at all, and a narrow
carve-out lets a few writes land in a quarantined area for you to review.

So the honest summary is not "nothing happens unattended". It is that the set
of things that can happen unattended is exactly the set marked `AUTO`, and you
can read that set rather than trust this sentence.

## What gets written down

Every tool call that passes the gate is recorded to
`runtime/logs/approvals.jsonl` — the time, the tool, a short summary of what it
was given, which rule decided, and the outcome. It survives restarts.

That includes the calls nobody was asked about. A tool resolving to `AUTO`
writes a row marked `auto`, kept distinct from the `allow_once` a tool gets
when you were asked and said yes — so "did I approve this?" and "did this
happen?" stay two different questions. Most of the file is `auto` rows, since
reading tools run several times a turn; filter them out to see only what you
were actually asked.

Two limits. The ledger records decisions, not outcomes — a row says a tool was
allowed to run, not what it did. And nothing prunes the file, so it grows.
(`runtime/logs/audit/` is a different log; it belongs to the MCP client.)
