# Licence texts

TESSERACT itself is under the GNU Affero General Public License, version 3 or
later. That text is in [../LICENSE](../LICENSE), not here.

This folder holds the full text of the licences that **other people's code**
inside TESSERACT is under. Naming a component and its licence is not enough
when that licence is one of the GNU or Mozilla family: those ask that the text
travel with the software, so it is here rather than a link someone has to
follow.

[../NOTICE.md](../NOTICE.md) is the inventory. It says what every component is
and which licence it is under. This folder is only the texts those entries
point at.

## Which text covers what

| File | Covers |
| --- | --- |
| `libgit2-COPYING.txt` | **libgit2 1.9.6**, compiled into the desktop app by `libgit2-sys` and used to update the app. This is libgit2's own file, not a generic copy, because its terms are GPL-2.0 plus a linking exception written specifically for it. It also carries the terms of what libgit2 itself bundles: zlib, PCRE2, the Clar test framework and Windows networking definitions. |
| `GPL-2.0.txt` | The base licence the file above modifies. Kept separately so the exception can be read against the thing it excepts. |
| `GPL-3.0.txt` | **phonemizer**, which is GPL-3.0 or later, and **espeak-ng**, which is GPL-3.0 only. Both arrive with the text to speech stack and are downloaded when you set TESSERACT up. |
| `LGPL-3.0.txt` | **@img/sharp-win32-x64**, Apache-2.0 with LGPL-3.0 or later. It processes images while the documentation site is built and is not part of the app. |
| `MPL-2.0.txt` | **certifi** and **tqdm** in Python, **lightningcss** in the app's build tools, and five Rust crates: `cssparser`, `cssparser-macros`, `selectors`, `dtoa-short` and `option-ext`. |
| `Apache-2.0.txt` | The Apache-2.0 components, which are the largest group. Named individually in `NOTICE.md`. Several of the downloaded models are under it too. |

The MIT, BSD and ISC components are not here. Those licences ask that each
package's own copyright notice travel with that package, which is how the
packages themselves are published, and there are several hundred of them.
`NOTICE.md` counts them and the lockfiles name every one.

## Where these came from

Downloaded on 2026-08-28 from the body that maintains each one, not from an
aggregator or from another project's copy.

- `GPL-2.0.txt`, `GPL-3.0.txt`, `LGPL-3.0.txt` and `MPL-2.0.txt` from the SPDX
  licence list, which is the reference text the identifiers in `NOTICE.md`
  refer to.
- `Apache-2.0.txt` from the Apache Software Foundation.
- `libgit2-COPYING.txt` from the libgit2 repository.

None of them is edited. If one needs replacing, replace it from the same
source rather than patching it.
