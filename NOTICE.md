# Notices and attribution

TESSERACT is free software under the GNU Affero General Public License, version
3 or later. The full terms are in [LICENSE](LICENSE).

Copyright (C) 2026 TESSERACT. Developed and owned by [Jihad Karaki](https://github.com/karakijihad).

This file lists what TESSERACT is built from and what it downloads to your
machine, and under what terms. It is generated from the project's lockfiles
rather than written by hand, so it describes the versions that actually ship.
The commands that produced it are at the bottom.

The full text of every licence named below that asks to travel with the
software is in [LICENSES/](LICENSES/), including libgit2's own file, which
carries the linking exception that lets it be compiled into this app at all.

Last generated: 2026-08-28.

## One open question you should know about

**The wake word model has an unresolved licence.** TESSERACT downloads
`sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2` from the
sherpa-onnx project to recognise the word that wakes it up.

The sherpa-onnx source code is Apache-2.0, and the archive that carries the
model declares Apache-2.0 in its own README. What that declaration does not
settle is where the model came from. It was trained on the GigaSpeech XL speech
corpus, whose own conditions of access say the data is for non-commercial
research and education, while the same corpus is also published carrying an
Apache-2.0 badge. The people who publish GigaSpeech have an open discussion
about exactly this contradiction and have not settled it, and no statement from
the sherpa-onnx maintainers was found that resolves it for this model.

So the position is this. The publisher says Apache-2.0, and that is recorded
here as their statement rather than repeated as a finding. The question
underneath it is open upstream and is not TESSERACT's to answer. If you intend
to use TESSERACT commercially, that question is worth your own look.

Everything else in the system, including all speech recognition and all speech
synthesis, is unaffected and clearly licensed.

## Models and voices

None of these are included in the download. TESSERACT fetches them from their
publishers when you set it up, and each is verified against a checksum before
it is used.

| What it does | Artifact | From | Licence |
| --- | --- | --- | --- |
| Speaks | Kokoro TTS, ONNX export and voice bundle | `thewh1teagle/kokoro-onnx` | MIT for the export, Apache-2.0 for the underlying `hexgrad/Kokoro-82M` weights |
| Listens | faster-whisper, base and small and large-v3-turbo | `Systran`, `deepdml` on Hugging Face | MIT, and MIT for OpenAI Whisper underneath |
| Ranks search results | ms-marco-MiniLM-L-6-v2, int8 ONNX | `Xenova` on Hugging Face | The ONNX build states no licence of its own. Its stated base model, `cross-encoder/ms-marco-MiniLM-L-6-v2`, is Apache-2.0 |
| Hears its name | sherpa-onnx keyword spotter, zipformer | `k2-fsa/sherpa-onnx` | Unsettled. See the section above |
| Runs local models | Ollama | `ollama/ollama` | MIT |
| Turns text into vectors | nomic-embed-text | Ollama library, from `nomic-ai` | Apache-2.0 |

Two of these are worth stating plainly rather than leaving in a table. The
reranker's ONNX build carries no licence tag at all, so the Apache-2.0 above is
inherited from the base model it names, not asserted by the file itself.
Ollama's download page states terms of use rather than a software licence, so
the MIT above comes from the source repository the installer is built from.

## Python

`tesseract/uv.lock` pins **122 entries**, one of which is TESSERACT itself, so
**121 third-party packages**. That is the universal set: it covers Windows,
macOS and Linux, and every optional extra.

The licence counts below are narrower, and the difference is worth stating
rather than glossing. They were read from the installed metadata of that same
locked set resolved for **one platform, Windows, with the development extra**,
which installs **109 third-party packages**. The twelve not counted are the
ones another platform or another extra would pull instead. Nothing was read
from a fresh resolve.

Of those 109: 25 MIT, 21 MIT License, 17 BSD-3-Clause, 10 Apache Software
License, 9 Apache-2.0, 5 BSD License, 2 BSD-2-Clause, 2 Python Software
Foundation License, and one each of a dozen further permissive forms. The
naming is inconsistent because each package declares its own; `MIT` and
`MIT License` are the same licence spelled two ways by their publishers.

Three need naming rather than counting:

- **`phonemizer` 3.4.0** is GPL-3.0-or-later, and **`espeakng-loader` 0.2.4**
  loads the `espeak-ng` library, which is GPL-3.0-only. Both arrive through
  `kokoro-onnx`, which is how TESSERACT speaks. Combining GPL-3.0 code into an
  AGPL-3.0-or-later work is the case AGPL version 3 was written to allow, so
  this is not a conflict. It is listed because those two components carry their
  own obligations, and a notice that mentioned only the outer licence would be
  incomplete.
- **`certifi`** is MPL-2.0 and **`tqdm`** is MPL-2.0 with MIT. The Mozilla
  licence covers the files themselves, neither is modified here, and neither
  affects the rest of the work.

## JavaScript, the app

`tesseract/mirror`, pinned in `pnpm-lock.yaml`.

178 packages ship: 150 MIT, 13 BSD-3-Clause, 4 Apache-2.0, 4 ISC, 3 OFL-1.1
for the fonts, and 4 others. **No copyleft licence appears in anything that
ships.**

193 further packages are used only to build and test. Two of them,
`lightningcss` at versions 1.32.0 and 1.33.0, are MPL-2.0. They are build
tools and do not reach the application.

## JavaScript, the documentation site

`Guide`, pinned in `package-lock.json`. 371 packages: 333 MIT, 11 ISC, 9
BSD-2-Clause, 5 Apache-2.0, 4 BSD-3-Clause, and a handful of others.

One package, `@img/sharp-win32-x64`, is Apache-2.0 with LGPL-3.0-or-later. It
is a prebuilt image-processing binary used while building the site. The LGPL
obligations attach to changes made to that library, and none are made here.

## Rust

`tesseract/mirror/src-tauri`, pinned in `Cargo.lock`. 477 crates: 449 MIT, 19
Unicode-3.0, 18 ISC, 7 Apache-2.0, 5 MPL-2.0, 4 BSD-3-Clause, 2
CDLA-Permissive-2.0, 1 Zlib. Most crates carry more than one, so the counts do
not add to 477.

The five MPL-2.0 crates are `cssparser`, `cssparser-macros`, `selectors`,
`dtoa-short` and `option-ext`. None is modified.

**libgit2 needs its own paragraph, because automated scanning misses it.**
TESSERACT uses `git2` to update itself. That crate and `libgit2-sys` are both
MIT or Apache-2.0, which is what a licence scanner reports. What the scanner
cannot see is that `libgit2-sys` compiles the actual libgit2 C library,
version 1.9.6, straight into the binary, and libgit2 itself is GPL-2.0-only.

It is included legitimately because libgit2's own terms grant an explicit
exception: the authors give unlimited permission to link the compiled library
into combinations with other programs and to distribute those combinations
without restriction. In SPDX terms that is
`GPL-2.0-only WITH GPL-2.0-linking-exception`. This is a specific grant written
into libgit2's licence, not a general argument about GPL compatibility.

libgit2 in turn bundles zlib (zlib licence), PCRE2 (BSD style), the Clar test
framework (ISC), and Windows networking definitions (LGPL-2.1). These are
recorded for completeness.

## How this file was produced

- **Python.** `uv 0.11.32` resolved and locked the graph, the locked set was
  installed into a throwaway environment outside the repository, and
  `pip-licenses 5.5.5` read the installed metadata.
- **JavaScript.** `license-checker-rseidelsohn 5.0.1` against the existing
  resolved trees in `tesseract/mirror` and `Guide`, production and development
  separately.
- **Rust.** `cargo-about 0.9.2` on `cargo 1.94.1`, with its configuration kept
  outside the repository.
- **Models.** Read one at a time from each publisher's own model card metadata
  and from each project's own `LICENSE` or `COPYING` file. Not from an
  aggregator, and not from any claim made inside this repository.
- **libgit2.** Read by hand from the library's own `COPYING`, because no
  scanner reaches vendored C source.

`uv lock --check` runs in continuous integration and fails when
`tesseract/pyproject.toml` and `tesseract/uv.lock` disagree, so a dependency
added without relocking cannot quietly make this file incomplete.
