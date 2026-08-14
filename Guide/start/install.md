---
title: Install
description: The installer sets up a shell and a package manager, then clones the app itself.
---

The installer sets up a shell and a package manager, then clones the app itself
from the public repository. Updates come the same way, so the app you install
today is the one that keeps itself current.

Full setup instructions, including prerequisites and the first-run
walkthrough, live in `SETUP.md` alongside the code.

## What first run does

First run writes the configuration you own from then on: which models to use,
which keys are present, what the hardware can do. Nothing in this guide's
[reference](../reference/config.md) section is written to your machine — those
pages describe the **shipped defaults**, which first run copies and then hands
to you.

## If something goes wrong

The runtime prefers to fail loudly. A missing config key raises at boot naming
the key; a missing model says which role wanted it and which file names it.
Silence is the one failure mode it is designed not to have.
