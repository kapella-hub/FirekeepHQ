# Licensing — current state and the open decision

_Last updated 2026-07-26._

## Where things stand

`LICENSE` at the repository root is **proprietary, all rights reserved**. That is
correct for today — the repo is private and nothing has been distributed — and it
is **not** the licence a customer will ever receive. It grants nothing, which is
the right posture with no customers and the wrong one the instant there is one.

## The open decision: open-core

**Recommended, not yet decided.** Two models are on the table.

| | Fully proprietary | Open-core (recommended) |
|---|---|---|
| Client kit, single-user core | Commercial licence | Permissive (Apache-2.0 / MIT) |
| Team features (relay coordination, team memory attribution, replay/evals dashboard) | Commercial licence | Commercial, licence-key gated |
| Distribution | Direct sales only | Free tier is the funnel |
| Enforcement | Contract | **Server-side.** Client-side gating in self-hosted software is decoration — the customer runs the binary. |

The case for open-core is distribution. A closed self-hosted product sold to
small teams has no discovery mechanism without either an open-source funnel or a
marketing budget. The client kit is the natural free half: it is differentiated
plumbing rather than the moat, so giving it away costs little and reaches people.

**What decides it:** whether the free tier is genuinely good enough to attract
users without cannibalising the paid one. That is a product judgement, not a
legal one, and it is the author's to make.

## What must happen before anything is distributed

These are ordered. Nothing below is done.

1. **Pick the model.** Everything else depends on it.
2. **Write the real licence.** If open-core: a permissive licence file for the
   free components plus a separate commercial agreement for team features. If
   proprietary: an EULA covering grant scope, deployment count, term, support,
   warranty disclaimer, liability cap and third-party flow-downs.
3. **Licence lifecycle policy.** Settle these before the enforcement mechanism is
   designed, because the mechanism encodes them:
   - **Expiry: degrade, do not brick.** Block writes and new-feature routes;
     keep recall, export and read paths working indefinitely. Bricking a
     customer's accumulated memory is incompatible with a self-hosted,
     data-sovereignty pitch. This should be a term in the licence, not an
     implementation detail someone can change later.
   - **Exit:** the customer's memory, skills and traces are theirs, with a
     documented export that works after expiry.
   - **Vendor continuity:** what happens if a single-maintainer vendor stops.
     Source escrow or a perpetuity clause. Buyers of solo-maintained software
     ask this, and it is cheap to answer well.
   - **Trial:** what an evaluation licence permits, and for how long.
4. **Package metadata.** Add `license` and `license-files` to
   `client/pyproject.toml` and `symdex/pyproject.toml`. Both currently declare
   neither. Do this before any wheel is published anywhere.
5. **Third-party attribution.** Produce a `NOTICE` covering bundled dependencies.
   `scripts/check_licenses.py` already gates against GPL/AGPL/SSPL/BSL in CI
   across cortex, client and symdex; attribution is the separate obligation that
   permissive licences still impose.
6. **Datastore licences.** Firekeep ships a compose file that pulls Neo4j, Redis,
   Qdrant and Ollama. Redistribution obligations differ by edition and version,
   and by whether images are bundled or pulled at runtime by the customer.
   Neo4j Community is GPLv3; Redis changed licence at 7.4. Re-check the pinned
   versions against the chosen model before publishing anything.

## Why this file exists

A readiness audit found no `LICENSE` anywhere and a README that said "all rights
reserved" — meaning a purchaser would have had no legal right to run the
software. That is fixed for now by the root `LICENSE`, but the fix is a
placeholder. This file exists so the placeholder is not mistaken for a decision,
and so the sequence above is not rediscovered later under time pressure.
