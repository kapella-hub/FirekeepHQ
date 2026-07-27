# Third-Party Datastores — Licence Analysis

_Last updated 2026-07-26. Written by an engineer, not a lawyer — same caveat as
`docs/LICENSING.md`: have a lawyer confirm this before it is relied on for a
sale. Versions and licence claims below were checked against the pinned
references in this repository and public sources on the date above; re-verify
before publishing anything derived from this file if it is more than a few
months old, since Redis in particular has changed licence terms twice in
recent years._

_Every image reference is now pinned by **tag and digest**, so the versions
named below cannot move without a commit. Before that they could, and one of
them had already moved across a licence boundary — see "Why the pins are what
make this file true" after the summary table._

## Why this file exists

Firekeep's compose file (`docker-compose.yml`) pulls four third-party
datastores: Neo4j, Redis, Qdrant, Ollama. `scripts/check_licenses.py` gates
Python dependencies in CI; it has no visibility into container base images, so
no automated check reads a licence out of an image. `tests/test_image_pins.py`
now guards the *pins* — that every reference carries a digest and that
references which must agree do — which is a different and narrower thing: it
keeps the versions named here from moving, but it cannot tell you what any of
them is licensed under. That analysis is still this file, by hand. Neo4j's removal was
surveyed and reversed (107h estimate — see `docs/HISTORY-NOTES.md`), which
means the product now permanently ships a compose file that references a
GPLv3-licensed component. That is a fine temporary state; it is not a fine
shipped position without the analysis below.

## Summary table

Every row states the licence **at the exact version now pinned**, not at "the
5.x line" or "the 7.x line". That distinction is the whole point of the Redis
row: the two are different licences.

| Datastore | Version pinned in `docker-compose.yml` (digest prefix) | Licence **at that exact version** | Where that licence claim comes from | Obligation met today? |
|---|---|---|---|---|
| Neo4j | **5.26.28** — `neo4j:5.26.28-community` (`sha256:36254241…`) | GPLv3 | **The artifact.** The image bundles `/var/lib/neo4j/NOTICE.txt`, read directly out of the pulled image | Yes, under the "customer pulls" model — see below |
| Redis | **7.4.10** — `redis:7.4.10-alpine` (`sha256:e7723ff7…`) | **RSALv2 or SSPLv1** (dual, source-available, **not** OSI open source) | **Not the artifact — Redis's own published licensing terms.** The image ships no licence text of any kind (verified: `find / -iname '*LICENSE*' -o -iname '*COPYING*' -o -iname '*NOTICE*'` inside it returns nothing, and it carries no licence labels). Only the version string `v=7.4.10` comes from the artifact, via `redis-server --version` | Yes **for self-hosted**. Not for a hosted Firekeep offering — see below |
| Qdrant | **v1.13.2** — `qdrant/qdrant:v1.13.2` (`sha256:81bdf0a9…`) | Apache-2.0 | **Not the artifact — Qdrant's own published licence.** Same check inside this image also returns no bundled licence file | Yes — permissive, no redistribution restriction |
| Ollama (runtime) | **0.32.4** — `ollama/ollama:0.32.4` (`sha256:10c13eb5…`) | MIT | **Not the artifact — Ollama's own published licence.** Same check inside this image also returns no bundled licence file. The version `0.32.4` is from the artifact (`ollama --version`) | Yes — permissive |
| Ollama (runtime, office-only images) | **0.32.0** — `ollama/ollama:0.32.0` (`sha256:57f573b4…`) in `docker/Dockerfile.ollama` and `docker/Dockerfile.embed` | MIT | as above | Yes — permissive |
| Ollama model weights (customer path) | `qwen3:4b` + `mxbai-embed-large`, pulled by the customer's own `ollama-pull` container. **Not digest-pinnable** — Ollama model tags are not content-addressed the way image references are | Apache-2.0 (both) | Their published model cards (Hugging Face / ModelScope) | Yes — permissive, and not conveyed by Firekeep anyway (customer's own pull) |
| Ollama model weights (office-only baked image) | `granite-embedding:30m` + `llama3.2:3b`, baked into `docker/Dockerfile.ollama`. Same non-pinnability caveat | Granite: Apache-2.0. **Llama 3.2: Llama 3.2 Community License (custom, not OSI open source)** | Meta's published licence text (llama.com/llama3_2/license) | **No** — attribution/notice obligations exist and are not currently met. Scoped to the office-only image, not the sold product — see below |

**Only Neo4j's licence is sourced from the artifact itself.** The other three
images ship no licence text at all, so their rows rest on the vendors' published
terms. A buyer's counsel treats "the thing we ship says so" and "the vendor's
website said so" as different quality of answer, and the table says which is
which rather than blurring them.

Digest prefixes above are for cross-checking only. The full digests live in
`docker-compose.yml` and the `docker/Dockerfile.*` mirrors, which are
authoritative; this file does not reproduce them because nothing would keep a
second copy in sync. What *is* mechanically enforced is that the version tags
named here still exist in the tree —
`tests/test_image_pins.py::test_documented_datastore_versions_are_the_versions_pinned`
fails if a pin is bumped without this table being revisited, which is the only
thing standing between a licence analysis and a stale licence analysis.

None of the four datastores block the current sale model. One real gap
exists (Llama 3.2 attribution on the office-only baked image, which today is
internal tooling, not part of what a customer receives) and one latent risk
is worth tracking (the office CI's Neo4j image mirror — see below).

---

## Why the pins are what make this file true

Until 2026-07-26 three of the four datastore references floated —
`redis:7-alpine`, `neo4j:5-community`, `ollama/ollama:latest` (only
`qdrant/qdrant:v1.13.2` already named an exact version) — as did every
supporting base image: `nginx:alpine`, `caddy:2-alpine`, `python:3.11-slim`,
`ubuntu:24.04`. A floating tag means **the licence
of the software being shipped can change with no commit to this repository, and
no line in any diff for a reviewer to notice.** This file would go on asserting
the old licence, correctly as of the day it was written and wrongly thereafter.

That is not a theoretical risk here — it is a thing that already happened, to
this repository, unnoticed:

> Redis relicensed at **7.4**, from 3-Clause BSD (permissive, OSI-approved) to
> dual RSALv2/SSPLv1 (source-available, **not** OSI open source). The tag
> `redis:7-alpine` carried Firekeep across that boundary on its own schedule.
> Nobody chose it, nobody reviewed it, and nothing in the repository recorded
> it. It was found by resolving the tag and asking the running image what
> version it actually was.

Two other floating tags carried different flavours of the same problem:
`neo4j:5-community` moves across 5.x minors and **Neo4j store-format upgrades
are one-way** — a customer running `docker compose pull` could upgrade their
database into a state they cannot roll back — and `ollama/ollama:latest` was
entirely unpinned, so two installs a month apart were two different products.

Pinning by **tag and digest** (`redis:7.4.10-alpine@sha256:e7723ff7…`) is what
converts every licence statement in this file from "true when written" to "true
until somebody changes it on purpose, in a commit, in a diff":

- the **digest** is content-addressed, so the bytes cannot change under a
  reference that has not changed;
- the **tag** is what makes the line reviewable — `redis@sha256:e7723ff7…` is
  equally immutable but tells nobody it is 7.4.10 and therefore RSALv2/SSPLv1
  rather than a 7.2.x on the old BSD terms. This table's whole structure
  depends on the tag being readable, which is why `tests/test_image_pins.py`
  asserts a pin carries both halves and not just the digest;
- the digests are **multi-arch manifest lists** (OCI image index / Docker
  manifest list), not per-platform manifests — pinning a platform manifest
  would break every architecture except the one it was resolved on.

The practical consequence for this document: **a licence claim here can now only
go stale through a deliberate act.** Bumping a pin is a commit; that commit is
the moment to re-check the row. `docs/LICENSING.md` item 6 and this file are
only as current as the last pin bump, and now there is a diff marking each one.

See `CLAUDE.md` → "Image pinning" for how to bump a pin correctly.

---

## Neo4j — GPLv3, and the conveyance question

**Pinned:** `neo4j:5.26.28-community@sha256:36254241…` in `docker-compose.yml`.
Verified directly — `docker pull neo4j:5-community && docker run --rm
--entrypoint sh neo4j:5-community -c 'neo4j --version'` — the then-floating
`5-community` tag resolved to **5.26.28** as of 2026-07-26, and that is the
version now pinned by exact tag and digest. (The floating tag was the version
of this that mattered most operationally, not legally: Neo4j store-format
upgrades are irreversible, so a customer's `docker compose pull` could have
carried their database across a 5.x minor they could not roll back.) The image
ships its own `/var/lib/neo4j/NOTICE.txt`, read directly out of the pulled image
rather than assumed from Neo4j's public docs:

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
  (`docker-compose.yml`) that names a public image
  (`neo4j:5.26.28-community@sha256:36254241…`).
  The customer's own `docker compose up` (run via `install.sh`) causes
  *their own* Docker daemon to fetch the image directly from Docker Hub —
  from Neo4j's own official registry, not from Firekeep. Firekeep never
  possesses, hosts, or transmits a copy of the Neo4j binary. This is the
  same category of act as a README saying `apt-get install postgresql` —
  providing configuration/instructions to obtain software from its
  publisher is not conveying that software. Under this model Firekeep has
  no GPLv3 obligations for Neo4j at all.
- **Model B — Firekeep publishes a pre-built image containing it.** If
  Firekeep's own CI builds an image `FROM neo4j:5.26.28-community` (even with
  zero modifications) and pushes/promotes that image to a registry Firekeep
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
(`FROM neo4j:5.26.28-community@sha256:36254241…`, no modification — and pinned
to the identical digest as the compose reference, which
`tests/test_image_pins.py` asserts, since a mirror on a different version of
the same tag would be a distinct artifact with distinct obligations). Per this repo's own `CLAUDE.md`
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
   `neo4j:5.26.28-community@sha256:…` reference directly rather than a
   Firekeep-mirrored one).
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

**Pinned:** `redis:7.4.10-alpine@sha256:e7723ff7…` in `docker-compose.yml`,
`docker-compose.test.yml` and `docker/Dockerfile.redis`.

It was `redis:7-alpine` — a **floating** tag that moved forward within the 7.x
line on Redis's schedule, not ours. Verified directly — `docker run --rm
redis:7-alpine redis-server --version` — it printed `Redis server v=7.4.10`,
confirmed 2026-07-26, and **7.4.10 is on the far side of Redis's relicensing.**
The floating tag had silently carried this product across a licence boundary.
That is the single clearest argument for the pinning described above, and it is
why the exact version is now written into the compose file where a reviewer can
see it.

**Two claims, two different sources — they are not equally strong:**

- **The version, 7.4.10: from the artifact.** `redis-server --version` inside
  the pinned image.
- **The licence at that version: NOT from the artifact.** The image ships **no
  licence text at all** — verified by searching the running container for any
  `LICENSE`/`COPYING`/`NOTICE` file (zero matches) and by inspecting its config
  labels (no licence labels). So unlike the Neo4j row above, nothing in what we
  ship states Redis's terms. The mapping below is sourced from Redis Ltd's own
  published licensing documentation and the `LICENSE.txt` in the `redis/redis`
  repository, cross-checked across independent pages.

Redis Ltd relicensed starting at 7.4 (announced March 2024): versions
≤7.2.4 are 3-Clause BSD (fully permissive, OSI-approved); **7.4.x through
7.8.x are dual-licensed under RSALv2 or SSPLv1**, licensee's choice of
which term to comply with; Redis 8.0+ added AGPLv3 as a third option.

**So the pinned version, 7.4.10, is source-available and not OSI open
source** — not the BSD-licensed Redis a reader might assume from the phrasing
"Redis changed licence at 7.4" without checking which side of it we are on.

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

Firekeep's usage — each customer runs their own `redis:7.4.10-alpine`
container, used internally by Firekeep's own services as a cache/broker/queue
backing store, never exposed as a standalone product or resold — is squarely
the permitted "embedded component" pattern, not the restricted "Redis as your
product" pattern. **Obligation met today**, on the strength of that reading.
This has not been reviewed by a lawyer; RSALv2's "primarily derives from"
language is judgment-dependent and worth confirming given the stakes of
getting it wrong for a commercial product.

Stated plainly, because this is the sentence a buyer's counsel will look for:

> **The Redis version Firekeep ships (7.4.10) is source-available, not open
> source. This is fine for the delivery model Firekeep sells — the customer
> runs the whole stack on their own infrastructure. It would NOT be fine for a
> hosted Firekeep offering, where Firekeep itself operates Redis on customers'
> behalf.**

The restriction that makes 7.4 different from 7.2 is precisely the one Firekeep
does not touch, and **a hosted offering is a non-goal** — self-hosted licensed
delivery is a settled decision, not a stage on the way somewhere else. So this
closes the question rather than deferring it. **If the delivery model ever
changes, this pin is where the licence question resurfaces** — re-run this
analysis before building a hosted tier, and note that RSALv2's "available to
third parties... through a computer network" language would then bite even
though Redis would still not be the *primary* value of that offering.

### Decided: stay on 7.4.10, exact-pinned

An earlier revision of this file listed three options and recommended "exact-pin
now, keep the RSALv2 reading, get it lawyer-reviewed." **That option is now
executed**, not proposed: the reference is `redis:7.4.10-alpine@sha256:e7723ff7…`
in all three places Redis appears. The remaining half of that recommendation —
lawyer review of the RSALv2 reading — is still open and still gates first sale.

**Downgrading to the last BSD line (7.2.4) was considered and rejected**, for
four reasons, each sufficient on its own:

1. **The restriction does not bind this product.** RSALv2 forbids offering the
   software to third parties as a managed service. Firekeep is self-hosted
   licensed — the customer runs the whole stack on their own infrastructure,
   their own daemon pulls Redis from Redis's own channel, and Firekeep never
   conveys it. The one term that makes 7.4 different from 7.2 is a term this
   product does not touch.
2. **7.2.4 is off the current line and no longer maintained alongside it.**
   Downgrading would trade a live security posture for a licence clause that
   does not apply — a bad trade in the wrong direction, and one nothing in CI
   would catch: `pip-audit --strict` gates Python packages only, and no gate in
   this repository watches a datastore image for CVEs. (Redis's support status
   per its published release policy; not verified from the artifact, and worth
   re-checking against Redis's current lifecycle page if this decision is ever
   reopened.)
3. **The downgrade may not even be safe.** 7.4 → 7.2 with existing RDB/AOF data
   is not a supported direction. Anyone who ran the stack before this pin has
   data written by 7.4.
4. **Staying is the status quo.** Downgrading is a behaviour change nobody asked
   for, taken on a licence theory that does not apply to how the product is
   sold.

The alternative worth keeping on file, if lawyer review disagrees with the
RSALv2 reading: **switch to Valkey** (`valkey/valkey`), the Linux Foundation's
BSD-3-Clause fork of Redis 7.2.4, maintained by AWS/Google/Oracle/Ericsson and
others, wire-protocol and API compatible ("drop-in" per its own documentation —
worth a compatibility smoke test against Firekeep's actual Redis usage before
trusting that claim wholesale, since "drop-in" claims from any vendor are
exactly the kind of thing this project's licensing work has learned not to take
at face value). It removes the question entirely on a maintained current line
rather than a frozen 7.2 branch, and unlike the 7.2.4 downgrade it does not
regress the security posture. Cost: low-to-moderate — an image swap plus a
compatibility pass across every `redis://` usage in this codebase (Cortex,
Bridge, Sentinel, Relay, replay, auth, vault all read Redis directly).

---

## Qdrant — Apache-2.0, no issue

**Pinned:** `qdrant/qdrant:v1.13.2@sha256:81bdf0a9…`. This tag was already an
exact version before the pinning pass — the digest adds immutability against a
re-push under the same tag, which is a smaller risk than a floating tag but not
zero. Qdrant **v1.13.2** is Apache-2.0 licensed — fully permissive, no
redistribution restriction, no "as a service" carve-out to worry about. Nothing
to remediate. Sourced from Qdrant's own published licence, **not** from the
artifact: the image states Qdrant's own terms nowhere. Precisely, because
re-running the Redis check here does *not* come back empty and the difference
matters: `find / -xdev -iname '*LICENSE*' -o -iname '*COPYING*' -o -iname
'*NOTICE*'` returns exactly one hit, `/usr/share/common-licenses/` — the Debian
base image's stock directory of *generic* licence texts (Apache-2.0, the GPLs,
MPL…), present on every Debian-derived image and saying nothing about what the
software on top of it is licensed under. The application directory `/qdrant`
contains the binary, its config and static assets, and no licence file at all.
So the substance matches Redis — nothing we ship states Qdrant's terms — but the
evidence is "a generic base-image directory, not an application licence",
not "no files found". (Attribution for Apache-2.0 components conventionally lives in a
NOTICE file when the licensee is redistributing the software itself; since
Qdrant here is pulled by the customer the same way Neo4j is under Model A
above, there's no redistribution act by Firekeep to attribute — noted here
for completeness, not because it's required.)

---

## Ollama — runtime is MIT; model weights are the real question

**Runtime image, pinned:** `ollama/ollama:0.32.4@sha256:10c13eb5…` in
`docker-compose.yml` (both the `ollama` service and the `ollama-pull` sidecar —
`tests/test_image_pins.py` asserts the two carry the *same* digest, since a
client and the daemon it populates should not be on different versions). The
office-only images `docker/Dockerfile.ollama` and `docker/Dockerfile.embed` pin
a deliberately different version, `ollama/ollama:0.32.0@sha256:57f573b4…`.

This was `ollama/ollama:latest` — **completely unpinned**, the worst of the
three floating references. Two installs a month apart were literally different
products, with no licence question attached to make anyone look. Resolving it
2026-07-26 (`ollama --version` inside the pulled image) gave **0.32.4**, which
is the version now pinned.

The Ollama runtime itself is MIT-licensed — per Ollama's own published licence,
**not** the artifact; this image ships no bundled licence file either (verified
the same way as Redis and Qdrant above). No obligation beyond standard MIT
attribution, which — same as Qdrant above — is moot here since the customer
pulls this image themselves; it is not redistributed by Firekeep.

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
- **A machine-enforced gate for container image *licences*.** Today only Python
  packages are licence-gated in CI (`scripts/check_licenses.py`). The four
  datastores above were checked by hand. Nothing reads a licence out of an image
  — and for three of the four there is nothing inside the image to read, which
  is why that scanner would be a meaningfully harder problem than the Python one
  (image manifests and their component licences aren't exposed the way package
  metadata is).

  What *is* enforced, and what it is not: `tests/test_image_pins.py` (CI job
  `repo-scripts`) asserts that every `image:` and every `FROM` carries a
  well-formed digest, that a pin keeps a readable tag alongside it, that
  references which must agree do, and that the version tags this file names are
  still the versions actually pinned. That last one is what keeps this analysis
  from going quietly stale: a pin bump now fails CI until this file is
  revisited. **None of it verifies a licence.** If Redis relicenses again at
  7.6, the guard will be perfectly green and this file will be wrong until a
  human re-reads it — the difference being that reaching 7.6 now requires a
  deliberate commit rather than a `docker compose pull`.
