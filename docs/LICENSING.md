# Licensing — source-available release policy

_Last updated 2026-07-31._

**This file and the `LICENSE` it describes were written by an engineer, not a
lawyer. Have a lawyer review the licence text before the first paid sale** —
nothing here should be treated as vetted legal advice, particularly the
liability cap, the expiry/degrade term, and the trademark notice.

## Where things stand

`LICENSE` at the repository root is the real licence, not a placeholder.
**Decided model: source-available Firekeep under BUSL-1.1.** The Additional Use
Grant permits production use by one natural person in one workspace and one
deployment, with unlimited personal devices, agent identities, terminals, and
background workers. Production use for more than one member requires a
commercial license. This is not an OSI Open Source licence; each version changes
to Apache-2.0 four years after its first public distribution.

| | Solo plan (free) | Team plan (paid) |
|---|---|---|
| Client kit and server | Source-available; production use for one member | Source-available; commercial license for additional members |
| Agent identities, devices, terminals, background workers | Unlimited when controlled by the one member | Unlimited |
| Group use | Not granted | Commercial license |
| Source | Available under BUSL-1.1; Apache-2.0 after the Change Date | Same |
| Enforcement | Built-in Solo fallback; no licence document needed | Signed offline entitlement; seat checks only when member invites are issued or accepted |

Entitlements do not gate memory, sessions, coordination, code intelligence,
presence, night-shift, device enrollment, or agent concurrency. An absent,
malformed, unsigned, or expired document degrades to Solo; existing data and
members remain usable. The server performs no entitlement phone-home.

## Production signing key and Team issuance

This is a one-time operator setup, performed before the first Team sale. Run the
tool on a trusted machine and write the private key **outside this repository**:

```bash
python /path/to/Firekeep/deploy/licence_tool.py keygen \
  --private-key /offline/path/firekeep-licence-signing.key
```

The command prints `FIREKEEP_LICENCE_PUBLIC_KEY=...`. The public value is not a
secret: put it in `.env.example` before building the first public server bundle,
so every Solo installation is already capable of validating a later Team
upgrade. Never copy the private key into the repository, GitHub Actions, a
customer server, the Firekeep vault, or a support bundle. Keep at least one
encrypted offline backup; losing it prevents renewal of existing customers.

To issue a Team entitlement after the customer supplies the `workspace_id`
shown on their Licence dashboard:

```bash
python /path/to/Firekeep/deploy/licence_tool.py mint \
  --private-key /offline/path/firekeep-licence-signing.key \
  --workspace-id workspace-... \
  --customer "Customer name" \
  --plan team --max-members 5 --days 365 \
  --output customer.firekeep-licence
```

The customer applies that file in Dashboard → Licence or with
`deploy/firekeep-admin licence apply customer.firekeep-licence`. The signing
key stays offline throughout issuance and application.

Pricing, seat counts, and trial duration are deliberately **not** decided and
are not in the licence — they are referred to "the applicable commercial
agreement" wherever they would otherwise appear.

### Every place a licence gets stated

A wheel carries **two** licence statements and they are set in different files.
`pyproject.toml`'s `license` field becomes `License-Expression` in the built
METADATA; the file named by `readme` becomes the long description **in that same
METADATA**. Correcting one and not the other ships a self-contradicting artifact.

That is not hypothetical: `symdex/pyproject.toml` was corrected to
`LicenseRef-Firekeep-BUSL-1.1` while `symdex/README.md` still said `MIT`, so
every `firekeep-symdex` wheel — installed unconditionally by the bootstrap on
every developer machine — asserted both. Fixed 2026-07-26 and now guarded by
`tests/test_package_licence_consistency.py`, which fails if a packaged README's
License section opens with an OSI identifier.

| Surface | Where | Guarded by |
|---|---|---|
| Wheel `License-Expression` | `client/pyproject.toml`, `symdex/pyproject.toml` → `license` | `test_pyproject_declares_the_source_available_licence` |
| Wheel long description | the file each `readme` field names | `test_readme_licence_section_does_not_contradict_metadata` |
| Bundled licence text | `license-files = ["LICENSE", "NOTICE"]` | `test_root_licence_file_is_busl` |
| Third-party notices | `NOTICE`, `client/NOTICE`, `symdex/NOTICE` (identical) | `scripts/generate_notice.py` + the CI licences job |
| Datastore obligations | `docs/THIRD-PARTY-DATASTORES.md` | prose; reviewed by hand. `tests/test_image_pins.py` guards the *pins* underneath it — that every image reference is digest-pinned, and that the versions the summary table states a licence for are the versions actually pinned. It cannot check a licence; it can stop the versions moving out from under one |

Known gap, not a contradiction: `client/pyproject.toml` declares no `readme` at
all, so the `firekeep-client` wheel ships with no long description and therefore
no human-readable licence statement in its metadata. Worth filling, but it states
nothing false today.

## What must happen before anything is distributed

1. ~~**Pick the model.**~~ **Done.** BUSL-1.1 with a Solo-only Additional Use
   Grant, then Apache-2.0 after four years (above).
2. ~~**Write the real licence.**~~ **Done.** `LICENSE` contains the unmodified
   BUSL-1.1 terms and its project-specific parameters. **Needs a lawyer read
   before first sale — see the note at the top of this file.**
3. **Commercial lifecycle policy.** Signed entitlements may degrade paid Group
   functionality, but this is product behavior rather than a term added to the
   standard BUSL text. The commercial agreement must state renewal, support,
   data export, and any trial terms before paid sale.
4. ~~**Package metadata.**~~ **Done.** `client/pyproject.toml` and
   `symdex/pyproject.toml` now declare `license = "LicenseRef-Firekeep-BUSL-1.1"`
   and `license-files = ["LICENSE", "NOTICE"]` (each package carries its own
   copy of both root files — PEP 639 `license-files` globs cannot point
   outside the project directory, so `client/LICENSE`/`client/NOTICE` and
   `symdex/LICENSE`/`symdex/NOTICE` are copies of the root files, not the
   source of truth). `LICENSE`'s three copies are still kept in sync by
   hand; `NOTICE`'s three copies are not — `scripts/generate_notice.py`
   writes all three from the same render (see item 5), so they cannot drift
   apart the way `LICENSE`'s copies could. `client`'s build-system now
   requires `setuptools>=77` and `symdex`'s requires `hatchling>=1.27` —
   both are the minimum versions that understand the PEP 639
   `license`/`license-files` fields; verified by building both wheels and
   confirming `License-Expression`/`License-File` (now listing both
   `LICENSE` and `NOTICE`) land correctly in `METADATA`.
5. ~~**Third-party attribution.**~~ **Done, including licence-text
   reproduction, not just naming.** `NOTICE` covers every third-party
   Python distribution installed by the actual shipped dependency sets —
   `cortex/requirements.txt` (covers bridge, relay, sentinel), the
   `firekeep-client` wheel, and the `firekeep-symdex` wheel — each
   collected in its own isolated venv (181 distributions total). Generated
   by `scripts/generate_notice.py`, which drives
   `scripts/check_licenses.py --attributions` (which licence a package
   declares) and `--license-texts` (that licence's actual bundled text,
   where locatable) in each venv rather than re-implementing licence
   classification or licence-file discovery, so the CI gate and the NOTICE
   generator can never silently disagree. Regenerate NOTICE
   (`python scripts/generate_notice.py`) after any dependency change; do
   not hand-edit the package list or the appendix — the generator writes
   the identical result to all three of `NOTICE`, `client/NOTICE`, and
   `symdex/NOTICE`, and `COPY NOTICE .` in the root `Dockerfile` puts the
   root copy into the shipped server image (verified: built the image and
   confirmed `/app/NOTICE` is present, 181-distribution content intact).
   One package (`caio`, a transitive dependency in the cortex set) declares
   no scannable licence *name* metadata at all — its licence was confirmed
   by hand (Apache-2.0, read directly from the installed package's bundled
   `COPYING` file) and recorded as a documented override in
   `generate_notice.py` so a future regeneration doesn't silently lose that
   finding.

   **Licence-text appendix.** Naming a licence does not by itself satisfy
   what MIT/BSD conventionally require of a redistribution (the copyright
   notice and permission text travelling with it) or what Apache-2.0 §4(d)
   requires (carrying forward upstream NOTICE content). `NOTICE`'s
   "Appendix: Bundled Third-Party Licence Texts" now reproduces each
   dependency's actual bundled licence file(s), read directly from the
   installed package (not retyped) — this is also what resolves the
   `cryptography` "Apache Software License ; BSD License" disjunction
   noted in an earlier review: the appendix carries the real dual-licence
   text (`LICENSE`, `LICENSE.APACHE`, `LICENSE.BSD`), not just an ambiguous
   classifier string. 180 of 181 distributions have a locatable bundled
   licence file; the one exception (`fastmcp-slim`, cortex set) is recorded
   explicitly in the appendix as a known gap, not silently dropped — it
   declares a permissive classifier (`License :: OSI Approved :: Apache
   Software License`) but its installed wheel contains no licence file at
   all to reproduce.

   **Discovered while building the appendix, not previously known:**
   `pywin32` (a transitive Windows-only dependency of `mcp` and
   `portalocker`, gated by a `sys_platform == "win32"` marker) declares a
   clean, permissive top-level classifier (Python Software Foundation
   License), but one of its bundled sub-components — `adodbapi`, vendored
   inside the same distribution — is licensed under LGPL-2.1, invisible to
   any classifier/metadata-level scan because it lives only in a nested
   `License-File` entry (`adodbapi/license.txt`), never in `pywin32`'s own
   declared licence metadata. Confirmed absent from the actual shipped
   server artifact: built the root `Dockerfile` image and queried its
   installed distributions directly — `pywin32` is not among them (the
   `win32` marker excludes it on the image's Linux base). It is a real
   dependency, however, of a Windows install of the `firekeep-client`/
   `firekeep-symdex` wheels (confirmed present in both wheels' actual pip
   installs during this same pass). `check_licenses.py`'s gate does not
   fail on this today (LGPL is a deliberate "report, don't deny" case — see
   `tests/test_check_licenses.py::test_lgpl_is_not_matched_by_the_gpl_rule`),
   and the new appendix now reproduces that LGPL text in full rather than
   hiding it, which is arguably the correct outcome rather than a new
   problem — but it had not been surfaced by any prior review of this repo
   and is flagged here for a human decision, not resolved by this change.
   Note also that the identification section above and this appendix now
   disagree on what licence `pywin32` "is" (permissive vs. permissive-plus-
   an-LGPL-sub-component) — that's a pre-existing gap in
   `check_licenses.py`'s whole-distribution classification model, not
   something this change resolves.
6. ~~**Datastore licences.**~~ **Analysis done; two action items open.** See
   `docs/THIRD-PARTY-DATASTORES.md` for the full analysis of Neo4j, Redis,
   Qdrant, and Ollama against their exact pinned versions (not assumed
   ones — the Redis pin in particular is **7.4.10**, past the 7.4 licence
   change, not the BSD-licensed line a casual read of "Redis changed licence
   at 7.4" might suggest: what Firekeep ships is source-available under
   RSALv2/SSPLv1, fine self-hosted, not fine for a hosted offering).

   **Those version claims are now load-bearing and enforced.** Every image
   reference in the compose files and Dockerfiles is pinned by **tag and
   digest** (`redis:7.4.10-alpine@sha256:e7723ff7…`), so a version — and
   therefore a licence — cannot change without a commit. Before that, all four
   datastores floated: `redis:7-alpine` had already carried this product across
   the BSD → RSALv2/SSPLv1 boundary with no diff anywhere, which is the concrete
   reason this is listed as a licensing item and not merely a reproducibility
   one. `tests/test_image_pins.py` fails CI if a reference loses its digest, if
   a pin drops its human-readable tag, or if a datastore is bumped without the
   licence analysis being revisited. Summary: the sold product
   (`docker-compose.yml` + `install.sh`) is clean today — Neo4j and Ollama
   model weights are pulled by the customer's own Docker/Ollama daemon, not
   conveyed by Firekeep, and Redis's dual RSALv2/SSPLv1 terms permit the
   internal-component usage pattern here under the RSALv2 reading. Two
   items are open, both scoped to internal/office tooling rather than the
   sold product: (a) the office GitLab CI pipeline reportedly mirrors and
   republishes a Neo4j image to Firekeep's own registry for the internal
   Kubernetes deployment — real conveyance if the description in `CLAUDE.md`
   is accurate, not independently verified since that pipeline config isn't
   in this repository; (b) `docker/Dockerfile.ollama` (the office-only baked
   image) bakes in `llama3.2:3b`, whose Llama 3.2 Community License requires
   a licence copy, a "Built with Llama" notice, and an attribution file that
   are not currently shipped with that image. Neither blocks the current
   sale model; both should be resolved before any customer-facing offering
   reuses that image-mirroring pattern.

## Why this file exists

A readiness audit found no `LICENSE` anywhere and a README that said "all
rights reserved" — meaning a purchaser would have had no legal right to run
the software. The root `LICENSE` now grants source-available rights under the
decided BUSL model. This file exists so the remaining commercial terms,
third-party attribution, and datastore licences are not lost, and so the
licence text is not treated as final without the lawyer review noted at the top.

---

## Name status: pre-filtered, NOT cleared

"Firekeep" has passed a registry/domain/live-product pre-filter. **USPTO classes
9 and 42 and EUIPO searches remain outstanding, and they gate the rename.** A
pre-filter is not clearance and must never be recorded as one — the repository
now carries the name in ~350 files, so a refusal is expensive and the reserve
candidate (`Remanence`) exists precisely so a refusal does not restart the
search from zero.

Mark owner: **Omnicron, LLC**.

## Pending: service-name collapse (Stage 2)

The seed renamed components to `FirekeepCortex` / `FirekeepBridge` /
`FirekeepRelay`. The decided end state is a collapse to **`firekeep-memory`**,
**`firekeep-sessions`** and **`firekeep-coord`** — "Cortex" has its own
collision with cortex.io.

This was deliberately not done in the seed: renaming the `cortex/` directory
moves every import path in the tree, and that cost belongs in the Stage 2
estimate alongside the Neo4j decoupling rather than being smuggled into a
mechanical find-and-replace.
