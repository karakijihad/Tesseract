---
title: Updates and your settings
description: What an update replaces, what it keeps, and where your previous settings go.
---

What an update replaces, what it keeps, and where your previous settings go.

TESSERACT updates itself by replacing its own program files wholesale. Its
configuration now follows the same rule: when a release ships a new version of
a config file, that file replaces yours, and the copy you had is kept.

This is deliberate, and the alternative is worse. Merging a release's new
settings into your file can only ever *add* — so when the shape of a file
changes, both the old and the new spelling end up in it, and the app has no way
to tell which one you meant. That failure is silent until it isn't.

## What actually happens

Only files a release genuinely changed are touched. If an update ships the same
settings you already have, nothing is replaced, nothing is backed up, and you
are not told anything — which is most updates.

When a file *is* replaced:

1. Your copy is saved to a `config-backup` folder inside your TESSERACT home
   directory, beside `config`.
2. The new version becomes your file.
3. The app tells you once, the next time you open it.

The backup folder holds **only the most recent previous copy**. The next update
that changes those files overwrites what is in it. If there is something in
there you want to keep permanently, move it somewhere else.

## What is kept

Four things are not settings, and they survive:

| Kept | What it is |
| --- | --- |
| The assistant's name | what you called it |
| Your name | what it calls you |
| Its gender | how it refers to itself |
| Its birth date | what the age it reports counts from |

An update has no opinion about any of these — the shipped file carries a
placeholder for each — so replacing them would rename your assistant and reset
its age to day one. They are read before the file is written and put back after.

## What you may need to set again

Everything else in those files is replaced, including choices you made in
Settings. After an update that says it replaced your configuration, check:

- **Models** — which model serves each role, and the order of your fallback
  chains.
- **Voice** — which voice speaks, and whether speech runs locally or in the
  cloud.
- **Permissions** — anything you moved from ASK to AUTO. These reset toward
  asking more often rather than less, which is the safe direction to be wrong in.
- **Schedule** — how often background jobs run, if you changed it.

All of it is a few clicks in Settings, and the backup folder has your previous
values if you would rather copy them across by hand. The files are plain YAML.

One thing repairs itself: the speech model chosen for your machine is worked
out again on the next launch, so a laptop that was given a smaller model keeps
it without you doing anything.

## Where to look

Your TESSERACT home directory holds both:

```
home/
├── config/          your settings, live
└── config-backup/   what you had before the last update that changed them
```

The backup folder contains a `README.md` explaining which files were replaced
and when — written at the moment it happened, so it describes your install
rather than the general case.

## If you would rather not be surprised

Nothing stops you keeping your own copy. The `config` folder is plain files:
copy it somewhere before you update, and you have a permanent record rather
than a rolling one. If you version your home directory in git — which is a
reasonable thing to do with it — you already have this.
