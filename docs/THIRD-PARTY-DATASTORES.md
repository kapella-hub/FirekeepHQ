# Third-Party Datastores — Licence Analysis

_Last updated 2026-07-26. Written by an engineer, not a lawyer — same caveat as
`docs/LICENSING.md`: have a lawyer confirm this before it is relied on for a
sale. Versions and licence claims below were checked against the pinned
tags in this repository and public sources on the date above; re-verify
before publishing anything derived from this file if it is more than a few
months old, since Redis in particular has changed licence terms twice in
recent years._

## Why this file exists

Firekeep's compose file (`docker-compose.yml`) pulls four third-party
datastores: Neo4j, Redis, Qdrant, Ollama. `scripts/check_licenses.py` gates
Python dependencies in CI; it has no visibility into container base images,
so nothing currently checks these automatically. Neo4j's removal was
surveyed and reversed (107h estimate — see `docs/HISTORY-NOTES.md`), which
means the product now permanently ships a compose file that references a
GPLv3-licensed component. That is a fine temporary state; it is not a fine
shipped position without the analysis below.

## Summary table

| Datastore | Pinned tag (docker-compose.yml) | Resolved version (`docker pull` + `--version`, verified 2026-07-26) | Licence | Obligation met today? |
|---|---|---|---|---|
| Neo4j | `neo4j:5-community` | 5.26.28 | GPLv3 (confirmed from the image's own bundled `NOTICE.txt`) | Yes, under the "customer pulls" model — see below |
| Redis | `redis:7-alpine` | 7.4.10 | RSALv2 **or** SSPLv1 (dual, source-available, not OSI open source) | Yes, under RSALv2's internal-component reading — see below |
| Qdrant | `qdrant/qdrant:v1.13.2` | v1.13.2 (pinned exactly) | Apache-2.0 | Yes — permissive, no redistribution restriction |
| Ollama (runtime) | `ollama/ollama:latest` | whatever `latest` resolves to at pull time | MIT | Yes — permissive |
| Ollama model weights (customer path) | `qwen3:4b` + `mxbai-embed-large`, pulled by the customer's own `ollama-pull` container | — | Apache-2.0 (both) | Yes — permissive, and not conveyed by Firekeep anyway (customer's own pull) |
| Ollama model weights (office-only baked image) | `granite-embedding:30m` + `llama3.2:3b`, baked into `docker/Dockerfile.ollama` | — | Granite: Apache-2.0. **Llama 3.2: Llama 3.2 Community License (custom, not OSI open source)** | **No** — attribution/notice obligations exist and are not currently met. Scoped to the office-only image, not the sold product — see below |

None of the four datastores block the current sale model. One real gap
exists (Llama 3.2 attribution on the office-only baked image, which today is
internal tooling, not part of what a customer receives) and one latent risk
is worth tracking (the office CI's Neo4j image mirror — see below).

---

## Neo4j — GPLv3, and the conveyance question

**Pinned:** `neo4j:5-community` in `docker-compose.yml`. Verified directly —
`docker pull neo4j:5-community && docker run --rm --entrypoint sh
neo4j:5-community -c 'neo4j --version'` — resolves to **5.26.28** as of
2026-07-26. The image ships its own `/var/lib/neo4j/NOTICE.txt`, read
directly out of the pulled image rather than assumed from Neo4j's public
docs:

> "The Software developed and owned by Neo4j is licensed under the GNU
> GENERAL PUBLIC LICENSE Version 3 ... to all third parties and that
> license, as required by the GPL, is included in the LICENSE.txt file.
> However, if you have executed an End User Software License and Services
> Agreement or ... another commercial license agreement with Neo4j ..., the
> terms of the license in such Commercial Agreement will supersede the
> GPL."

That confirms both halves of the picture directly from the artifact: the
pulled `5-community` image is GPLv3 (Community Edition, no commercial
agreement in play here), and Enterprise Edition exists as a separate
commercially-licensed track this compose file does not use.

### The conveyance distinction

GPLv3 defines "convey" (§0) as any propagation that enables other parties to
make or receive copies. The obligations that matter here — offer
corresponding source, don't add further restrictions, preserve licence
notices — attach to the act of **conveying** a copy, not to running the
software or linking against it over a network.

- **Model A — customer pulls the image.** Firekeep publishes a text file
  (`docker-compose.yml`) that names a public image (`neo4j:5-community`).
  The customer's own `docker compose up` (run via `install.sh`) causes
  *their own* Docker daemon to fetch the image directly from Docker Hub —
  from Neo4j's own official registry, not from Firekeep. Firekeep never
  possesses, hosts, or transmits a copy of the Neo4j binary. This is the
  same category of act as a README saying `apt-get install postgresql` —
  providing configuration/instructions to obtain software from its
  publisher is not conveying that software. Under this model Firekeep has
  no GPLv3 obligations for Neo4j at all.
- **Model B — Firekeep publishes a pre-built image containing it.** If
  Firekeep's own CI builds an image `FROM neo4j:5-community` (even with zero
  modifications) and pushes/promotes that image to a registry Firekeep
  controls, Firekeep has made a copy available to whoever can pull from that
  registry. That is conveyance, full stop — redistributing a work
  unmodified still triggers GPLv3's redistribution terms (offering
  corresponding source, preserving notices). It would not require any of
  *Firekeep's own* code to become GPL — Neo4j is a separate process
  communicating over Bolt, not linked into Firekeep's binaries — but it
  would obligate Firekeep to handle that one image's redistribution
  correctly (trivial in practice, since the "corresponding source" is
  Neo4j's own unmodified public source, but it is a real, distinct
  compliance action, not a no-op).

### Which model Firekeep uses today

**The sold product uses Model A.** `install.sh` and the root
`docker-compose.yml` are what a paying customer runs; Neo4j is pulled by
their Docker daemon from Docker Hub, never touched by Firekeep. This is
sound as shipped.

**A second, internal path exists and is closer to Model B.**
`docker/Dockerfile.neo4j` in this repo is a one-line mirror
(`FROM neo4j:5-community`, no modification). Per this repo's own `CLAUDE.md`
("Deploy to Kubernetes (office — two-repo pattern)"), the office GitLab CI
pipeline builds `docker/Dockerfile.*` — described there as "infra mirrors" —
as part of its tagged-release image builds and promotes them to Firekeep's
own registry, from which the office Kubernetes deployment (the config
repo's Helm chart) pulls. That pipeline configuration
(`.gitlab-ci.yml`) is **not present in this checked-out repository** — it
lives on the office GitLab remote per the two-repo pattern the docs
describe — so I could not directly inspect it; the description above is
read from `CLAUDE.md`, not independently verified against the pipeline
itself. If that description is accurate, Firekeep's own CI is building and
publishing a Neo4j image copy today, which is Model B, not Model A.

`git log --oneline -- docker/Dockerfile.neo4j` shows exactly one commit
touching the file — the initial bulk seed commit — and nothing since. That
is consistent with either an actively-used mirror definition that simply
hasn't needed a change, or a vestigial file carried over from the seed and
never wired to anything live in this repo's own history; the commit history
alone can't distinguish the two, which is exactly why this is flagged as
"reportedly," not confirmed.

This does **not** affect the product being sold — the office deployment
described there is Firekeep's/Omnicron's own internal tooling
("office deployment," dogfooding), not something a customer receives. Internal
mirroring to a registry that never reaches parties outside the company is
widely treated as not "conveying to other parties" in the GPLv3 sense
(the same way companies routinely mirror Ubuntu/Postgres images to an
internal Artifactory without triggering redistribution obligations), but
this is a practitioner norm, not settled by GPLv3's text, and I have not had
it reviewed. Flag for the lawyer review this file already calls for, and
resolve the pipeline visibility gap (get the actual `.gitlab-ci.yml`
reviewed) before treating it as settled.

**What would change the analysis for the sold product:** shipping any of
the following would flip the customer path from Model A to Model B, and
would need the redistribution obligations above actually satisfied first:
- a pre-built VM image, AMI, OVA, or single-file "appliance" that bundles
  Neo4j;
- a Firekeep-hosted/managed offering where Firekeep itself operates Neo4j on
  customers' behalf (also raises AGPL-style "offered as a service"
  questions for other components — Neo4j Community is GPLv3, not AGPL, so
  this specific concern is narrower for Neo4j than it would be for an AGPL
  component, but worth re-checking if a hosted tier is ever built);
- a Kubernetes/Helm distribution path sold to customers that pulls images
  from Firekeep's own registry instead of the customer's own `docker compose
  up` against Docker Hub — which is exactly what the office two-repo pattern
  already does internally, so reusing that pattern for a customer-facing
  offering (plausible, given the tooling already exists) is the concrete
  scenario to watch.

### Remediation options, priced roughly, if Model B for the sold product
ever becomes real

Not needed today, but worth having priced since the office pattern already
exists and could get reused for a customer offering without anyone
re-running this analysis:

1. **Buy Neo4j Enterprise Edition and redistribute that instead.** Neo4j
   Enterprise is under Neo4j's own commercial terms, not GPLv3 — this
   removes the GPLv3 question entirely but replaces it with a Neo4j
   commercial licence fee and Neo4j's own redistribution terms, which would
   need their own review. Cost: a commercial negotiation with Neo4j, not
   estimated here — pricing tiers are deliberately undecided per this
   project's standing decisions, and a Neo4j Enterprise cost has not been
   quoted.
2. **Comply with GPLv3 for the Community image specifically.** Since
   Firekeep would be redistributing Neo4j *unmodified*, "corresponding
   source" is just a pointer to Neo4j's own public GitHub repo — cheap.
   The real cost is process discipline: preserve GPLv3 notices on that one
   image, don't bundle it into the same image as Firekeep's proprietary
   code (keep it a separate container, which is already how it's deployed),
   and don't represent the combined *product* as proprietary in a way that
   implies Neo4j's licence terms don't apply to that component (the root
   `LICENSE`'s §4 THIRD-PARTY COMPONENTS clause already covers this
   generally). Rough cost: low — mostly a documentation and packaging
   discipline exercise, not a code change.
3. **Restructure so the customer always pulls it themselves** (i.e., make
   Model A the *only* path, including for any future Kubernetes/hosted
   offering — e.g., a Helm chart that references the public
   `neo4j:5-community` tag directly rather than a Firekeep-mirrored one).
   Rough cost: low for a from-scratch design; higher if it means
   re-plumbing the office two-repo pattern's existing registry-promote
   flow, which was built around Firekeep controlling every image it
   deploys (consistent image provenance, `verify_pull` gating, etc.) —
   carving out one exception for Neo4j specifically is a real but bounded
   change.

None of these require removing Neo4j, consistent with the standing decision
not to re-litigate that call.

---

## Redis — the 7.4 relicense, checked against the actual pin

**Pinned:** `redis:7-alpine` in `docker-compose.yml` and
`docker/Dockerfile.redis`. This is a **floating** tag — it moves forward
within the 7.x line. Verified directly — `docker pull redis:7-alpine &&
docker run --rm redis:7-alpine redis-server --version` — prints
`Redis server v=7.4.10`, confirmed 2026-07-26.

Redis Ltd relicensed starting at 7.4 (announced March 2024): versions
≤7.2.4 are 3-Clause BSD (fully permissive, OSI-approved); **7.4.x through
7.8.x are dual-licensed under RSALv2 or SSPLv1**, licensee's choice of
which term to comply with; Redis 8.0+ added AGPLv3 as a third option. (The
licence-terms-per-version mapping itself is Redis's published release
policy, not something extractable from the pulled image — the alpine image
does not bundle a licence text file — so that part is sourced from Redis's
own release documentation, cross-checked against two independent pages;
only the version number 7.4.10 is directly verified against the artifact.)

**The pinned tag (7.4.10) falls on the post-relicense, source-available
side of that line — this is not the BSD-licensed Redis a reader might
assume from the CLAUDE.md docs' phrasing "Redis changed licence at 7.4"
without checking the actual resolved version.** That check is the point of
this file, and it changes the analysis from a hypothetical to a real one.

### Is the obligation met?

Neither RSALv2 nor SSPLv1 is OSI-approved open source — both are
source-available licences with a "don't compete with us" style restriction.
Electing to comply with **RSALv2** (the licensee's choice, since Redis 7.4
offers RSALv2 *or* SSPLv1 and only one needs to be satisfied) is the
relevant one here: its Limitations clause restricts making "the
functionality of the Software... available to third parties as a service,"
elaborated as offering a product whose value "entirely or primarily
derives from the value of the Software" — i.e., it targets a Redis-as-a-
service / Redis-cloud competitor. It explicitly permits using Redis
internally, as a cache, as a message broker, and distributing an
application with Redis embedded as a component.

Firekeep's usage — each customer runs their own `redis:7-alpine` container,
used internally by Firekeep's own services as a cache/broker/queue backing
store, never exposed as a standalone product or resold — is squarely the
permitted "embedded component" pattern, not the restricted "Redis as your
product" pattern. **Obligation met today**, on the strength of that reading.
This has not been reviewed by a lawyer; RSALv2's "primarily derives from"
language is judgment-dependent and worth confirming given the stakes of
getting it wrong for a commercial product.

**Caveat for the future:** if Firekeep ever ships a Firekeep-hosted/managed
tier (Firekeep itself running the stack and selling access, rather than the
current self-hosted-only model), the "available to third parties... through
a computer network" language starts to bite closer to the line, even though
Redis still would not be the *primary* value of that offering. Re-run this
analysis before building a hosted tier.

### Remediation options, if a cleaner position is wanted regardless

1. **Pin to the last BSD-licensed tag, `redis:7.2-alpine` (or an explicit
   `redis:7.2.4-alpine` version pin instead of the floating `7-alpine`).**
   Removes the RSALv2/SSPLv1 question entirely by staying on the fully
   permissive licence. Cost: low — a one-line compose change, plus losing
   Redis 7.4+ feature/security fixes going forward (7.2 is a maintenance
   branch, not the current line). Also fixes the more mundane
   reproducibility problem that a floating `7-alpine` tag silently moves
   the shipped Redis version out from under the pin over time — worth
   fixing on its own merits, independent of licensing.
2. **Switch to Valkey** (`valkey/valkey`), the Linux Foundation's BSD-3-
   Clause fork of Redis 7.2.4, maintained by AWS/Google/Oracle/Ericsson and
   others, wire-protocol and API compatible ("drop-in" per its own
   documentation — worth a compatibility smoke test against Firekeep's
   actual Redis usage before trusting that claim wholesale, since "drop-in"
   claims from any vendor are exactly the kind of thing this project's
   licensing work has learned not to take at face value). Removes the
   question entirely, on a maintained current line rather than a frozen
   7.2 branch. Cost: low-to-moderate — an image swap plus a compatibility
   pass across every `redis://` usage in this codebase (Cortex, Bridge,
   Sentinel, Relay, replay, auth, vault all read Redis directly).
3. **Do nothing, keep the RSALv2 reading above, and pin the exact version**
   (`redis:7.4.10-alpine3.21` rather than the floating `7-alpine`) so the
   licence actually shipped doesn't silently drift on a future rebuild.
   Cost: near-zero. This is the pragmatic recommendation if the RSALv2
   reading survives lawyer review — it fixes the reproducibility gap
   without a component swap.

Recommendation, not yet a decision: option 3 now (exact-pin, keep the
RSALv2 reading, get it lawyer-reviewed), with option 1 or 2 as the fallback
if that review disagrees.

---

## Qdrant — Apache-2.0, no issue

**Pinned:** `qdrant/qdrant:v1.13.2`, an exact (non-floating) version pin.
Qdrant is Apache-2.0 licensed — fully permissive, no redistribution
restriction, no "as a service" carve-out to worry about. Nothing to
remediate. (Attribution for Apache-2.0 components conventionally lives in a
NOTICE file when the licensee is redistributing the software itself; since
Qdrant here is pulled by the customer the same way Neo4j is under Model A
above, there's no redistribution act by Firekeep to attribute — noted here
for completeness, not because it's required.)

---

## Ollama — runtime is MIT; model weights are the real question

**Runtime image, pinned:** `ollama/ollama:latest` in `docker-compose.yml`
(a floating tag — same reproducibility caveat as Redis's `7-alpine`, though
with no licence question attached to it). The Ollama runtime itself is
MIT-licensed. No obligation beyond standard MIT attribution, which — same
as Qdrant above — is moot here since the customer pulls this image
themselves; it is not redistributed by Firekeep.

Model **weights** are a separate legal question from the runtime that
serves them — they carry their own licence terms, and Ollama's MIT licence
says nothing about what you may do with a model pulled through it.

### Customer path (the sold product): Apache-2.0, not conveyed by Firekeep either way

`docker-compose.yml`'s `ollama-pull` service runs `ollama pull
${LLM_MODEL:-qwen3:4b}` and `ollama pull
${EMBEDDING_MODEL:-mxbai-embed-large}` — both defaults confirmed against
`.env.example`. Both model families are Apache-2.0 licensed (Qwen3 per
its Hugging Face/ModelScope release; `mxbai-embed-large-v1` per its
Hugging Face model card). Even if they carried a stricter licence, this is
the same Model A pattern as Neo4j: the *customer's own* `ollama-pull`
container fetches the weights directly from Ollama's public model registry
at install/update time — Firekeep never possesses or transmits a copy.
**Obligation met, and moot regardless of the licence, for the same
conveyance reason as Neo4j above.**

### Office-only baked image: Llama 3.2's licence obligations are real and not yet met

`docker/Dockerfile.ollama` is a different situation: it **bakes model
weights directly into a Firekeep-built image** at build time (`ollama pull
granite-embedding:30m && ollama pull llama3.2:3b`, then the image is
chunked and published — see the file's own header comment and
`CLAUDE.md`'s office-deployment notes). This is unambiguous conveyance by
Firekeep of whatever weights get baked in, the same distinction drawn for
Neo4j's Model B above, except here it's not hypothetical — it is what this
Dockerfile already does.

- `granite-embedding:30m` — IBM Granite, Apache-2.0. No issue.
- `llama3.2:3b` — **Meta's Llama 3.2 Community License**, a custom licence,
  not OSI-approved open source. Confirmed obligations that attach to
  distributing it (per the licence text at llama.com/llama3_2/license):
  - provide a copy of the Llama 3.2 Community License Agreement alongside
    any distributed copy;
  - **prominently display "Built with Llama"** on any related website, UI,
    product documentation, or about page;
  - include a `Notice` text file bearing the attribution: *"Llama 3.2 is
    licensed under the Llama 3.2 Community License, Copyright © Meta
    Platforms, Inc. All Rights Reserved."*
  - (a >700M-monthly-active-user threshold requires a separate licence from
    Meta directly — not a concern at Firekeep's current or foreseeable
    scale, noted for completeness only.)

**None of the three obligations above are currently met** — there is no
Llama licence text, no "Built with Llama" notice, and no attribution file
shipped alongside `docker/Dockerfile.ollama` or its output image. This is
scoped narrowly: `docker/Dockerfile.ollama` builds the office-only baked
image (per `CLAUDE.md`, deployed via the config repo's Helm chart for
Firekeep's/Omnicron's own internal use), **not** something a paying
customer receives today. It is a real gap, just not one that touches the
sold product yet — same caveat as the Neo4j office-mirror finding above:
if this baked-image pattern is ever reused for a customer-facing offering,
these three obligations become customer-facing obligations too, and must be
satisfied before that ships.

### Remediation

1. **Fix the gap where it already exists (office-only image), cheaply:**
   add the Llama 3.2 licence text and the required `Notice` file to
   `docker/Dockerfile.ollama`'s build context, and add a "Built with Llama"
   line to whatever internal-facing documentation describes that
   deployment (`docs/DEPLOYMENT-OFFICE.md` or equivalent). Low cost, and
   the honest thing to do regardless of whether this image ever reaches a
   customer.
2. **Or swap the office LLM to an Apache/MIT-licensed model** (e.g. reuse
   `qwen3:4b`, already the customer-path default, instead of `llama3.2:3b`)
   to remove the obligation rather than satisfy it. `CLAUDE.md` records
   that this exact model choice has already moved twice (a chunked-ollama
   saga, an abandoned qwen2.5:1.5b piggyback, then the current
   granite+llama3.2 baked pair) for infra/replication reasons unrelated to
   licensing — worth folding a licence-simplicity argument into whatever
   process owns that decision next time it's revisited, rather than
   treating this remediation as urgent on its own.

Recommendation: option 1 now (cheap, fixes the current gap without
disturbing a model choice that was already picked for unrelated
infra reasons), with option 2 worth raising the next time that model choice
is revisited anyway.

---

## What this file does not cover

- Non-Python, non-container dependencies (none identified — the dashboard
  is a static SPA with no bundled third-party JS framework found during
  this pass; re-check if that changes).
- A machine-enforced gate for container base image licences. Today only
  Python packages are gated in CI (`scripts/check_licenses.py`); the four
  datastores above were checked by hand for this file and will drift out of
  date silently on the next version bump (see the Redis floating-tag point
  above) unless someone re-runs this analysis or builds a scanner for it.
  Worth a follow-up if datastore version pins change often — not attempted
  here since it's a meaningfully different scanning problem (image
  manifests and their component licences aren't exposed the way Python
  package metadata is).
