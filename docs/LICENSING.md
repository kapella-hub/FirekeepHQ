# Licensing — current state and remaining work

_Last updated 2026-07-26._

**This file and the `LICENSE` it describes were written by an engineer, not a
lawyer. Have a lawyer review the licence text before the first paid sale** —
nothing here should be treated as vetted legal advice, particularly the
liability cap, the expiry/degrade term, and the trademark notice.

## Where things stand

`LICENSE` at the repository root is now the real licence, not a placeholder.
**Decided model: free core, not open source.** A single-user tier is gratis
and closed source (no redistribution, no derivative works, no source
disclosure); team features are unlocked by a licence key under a separate
commercial agreement. This is **not** open-core — there is no permissively
licensed component, no source split, and nothing about the free tier implies
a right to the source.

| | Free tier | Team tier |
|---|---|---|
| Client kit, single-user core | Gratis, closed source | — |
| Team features (relay coordination, team memory attribution, replay/evals dashboard) | Not included | Commercial, licence-key gated |
| Distribution | Direct install, no fee | Sold under a separate commercial agreement |
| Source | Never disclosed, either tier | Never disclosed, either tier |
| Enforcement | Contract (licence terms) + licence-key gating server-side for Team Features | Same |

Pricing, seat counts, and trial duration are deliberately **not** decided and
are not in the licence — they are referred to "the applicable commercial
agreement" wherever they would otherwise appear.

## What must happen before anything is distributed

1. ~~**Pick the model.**~~ **Done.** Free core, not open source (above).
2. ~~**Write the real licence.**~~ **Done.** `LICENSE` at the repo root covers:
   free-tier grant (single user, one deployment, non-transferable, gratis,
   closed source, no redistribution/derivative works), the paid team-tier
   grant (Team Features licence-key gated, terms deferred to the commercial
   agreement),
   warranty disclaimer, a liability cap, third-party components governed by
   their own terms, and the licence-lifecycle term below. **Needs a lawyer
   read before first sale — see the note at the top of this file.**
3. **Licence lifecycle policy.**
   - ~~**Expiry: degrade, do not brick.**~~ **Done, as a licence term** (`LICENSE`
     §3): on expiry, writes and Team Feature / new-feature routes may be
     blocked; recall, export, and other read paths keep working indefinitely.
   - ~~**Exit / data export guarantee.**~~ **Done, as a licence term**
     (`LICENSE` §3): export works at all times, including after expiry or
     termination, and survives termination of the licence.
   - **Vendor continuity** (source escrow / perpetuity clause if the
     single-maintainer vendor stops) — **not yet decided**, not in `LICENSE`.
     Still open.
   - **Trial** (what an evaluation licence permits, and for how long) —
     **deliberately undecided**, not in `LICENSE`; left to the commercial
     agreement so no number gets invented here.
4. ~~**Package metadata.**~~ **Done.** `client/pyproject.toml` and
   `symdex/pyproject.toml` now declare `license = "LicenseRef-Firekeep-Proprietary"`
   and `license-files = ["LICENSE"]` (each package carries its own copy of the
   root `LICENSE` — PEP 639 `license-files` globs cannot point outside the
   project directory, so `client/LICENSE` and `symdex/LICENSE` are copies of
   the root file, not the source of truth; keep them in sync if `LICENSE`
   changes). `client`'s build-system now requires `setuptools>=77` and
   `symdex`'s requires `hatchling>=1.27` — both are the minimum versions that
   understand the PEP 639 `license`/`license-files` fields; verified by
   building both wheels and confirming `License-Expression`/`License-File`
   land correctly in `METADATA`.
5. **Third-party attribution.** Produce a `NOTICE` covering bundled
   dependencies. `scripts/check_licenses.py` already gates against
   GPL/AGPL/SSPL/BSL in CI across cortex, client and symdex; attribution is
   the separate obligation that permissive licences still impose. Not done.
6. **Datastore licences.** Firekeep ships a compose file that pulls Neo4j,
   Redis, Qdrant and Ollama. Redistribution obligations differ by edition and
   version, and by whether images are bundled or pulled at runtime by the
   customer. Neo4j Community is GPLv3; Redis changed licence at 7.4. Re-check
   the pinned versions against the free/team-tier model before publishing
   anything. Not done.

## Why this file exists

A readiness audit found no `LICENSE` anywhere and a README that said "all
rights reserved" — meaning a purchaser would have had no legal right to run
the software. The root `LICENSE` now grants real rights under the decided
free-core model. This file exists so the remaining open items (vendor
continuity, trial terms, third-party attribution, datastore licences) are not
lost, and so the licence text is not treated as final without the lawyer
review noted at the top.

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
