---
title: Memory and the vault
description: Two stores. They do not merge, and that is deliberate.
---

Two stores. They do not merge, and that is deliberate.

## Memory — what it knows about you

Small, personal, and actively curated. It **decays**: things not reinforced
fade. It **consolidates**: related fragments are merged into something sharper
than either. It is consulted on every turn, automatically, without you asking.

The design goal is a store that improves as it gets smaller. See
[Memory](../anatomy/memory.md) for how retrieval actually runs.

## The vault — what it has read

Large, append-only, and never forgotten on purpose. Documents you give it:
markdown, text, PDFs, spreadsheets, structured data, Word files. Binaries are
stored but not indexed, because there is nothing useful to index.

The vault is queried **on request**, not on every turn. It has its own wiki and
its own search.

## Why they are separate

If they merged, every question would drag the whole library through it, and
personal memory would drown in reference material. Keeping them apart means
"what do you know about me" and "what have you read" stay different questions
with different costs.

## Both are files

Canonical state is files on your disk, in Markdown. The search indexes are
derived and can be rebuilt from those files at any time. If the indexes were
lost tomorrow, nothing you told it would be.

That also means the store is portable and readable by anything else that reads
Markdown — copy the directory and the assistant's knowledge comes with it.
