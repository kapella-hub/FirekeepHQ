# Dashboard redesign + brand identity — design

**Date:** 2026-07-28
**Scope:** `dashboard/` — `index.html`, new `dashboard/brand/`,
`docker/Dockerfile.dashboard`, `docker-compose.yml`

**Status — partially implemented.** §3 (brand assets, mark, head tags) and §4
(ember palette) are shipped and deployed. §5–§10 are not. Sections are marked
`SHIPPED` or `PENDING` at their heading.

Line references below are as of the pre-implementation file and have since
shifted; treat them as landmarks, not coordinates.

---

## 1. Why this is smaller than "redesign the dashboard"

The brief was that the dashboard "looks like a mess" and needs to be sellable.
Before designing anything, the current dashboard was rendered and read. Most of
the assumed problem does not exist:

* The stylesheet is **already** a coherent, token-driven design system — a named
  theme ("Graphite Pro"), a full `:root` token set, and exactly **one**
  hard-coded hex outside `:root` in 588 lines of CSS.
* The 14 tabs are **already** grouped into four labelled nav sections
  (NAVIGATION / INTELLIGENCE / QUALITY / SYSTEM).

An earlier draft of this design proposed "rewrite the CSS as a real design
system" and "group the 14 tabs into 3–4 sections". Both were already done. That
work was cut on evidence.

What is actually wrong is **brand, honesty, and consistency** — not styling.
Approach chosen: **restyle in place, keep vanilla.** No React, no npm, no build
step. `docker/Dockerfile.dashboard` gains one `COPY` for the brand assets and
otherwise stays a plain file-copy image.

The Corona React admin template was evaluated and rejected. It is MIT (no legal
blocker) but has only 2 commits, and adopting it would mean rewriting 3,188
lines of working JS, introducing npm into an image that currently has no build
step, and creating a CI blind spot — the `Dependency CVEs + SBOM` and
`Dependency licences` jobs cover Python only.

## 2. Findings this design responds to

Each was verified by rendering the dashboard and reading the source, not by
inspection of the UI description.

| # | Finding | Evidence |
|---|---|---|
| 1 | **The health grid reports failure as success.** | `index.html:2159` — `if (r.ok \|\| r.status === 405 \|\| r.status === 404)` paints the card green. A render against a server with no API showed all four services green at 31–34ms while every request returned 404. |
| 2 | **The brand is still the previous product.** | `index.html:604` `<div class="logo-mark">N</div>` — the predecessor's initial; `:605` `Firekeep<span>Stack</span>` — a find-and-replace artifact, with the dead half highlighted in the accent colour. `<title>` is correct. |
| 3 | **Icons are half geometry, half emoji.** | `&#9673;` ◉ and `&#8644;` ⇄ are monochrome glyphs; `&#9889;` ⚡ and `&#10067;` ❓ are emoji-presentation codepoints that render full-colour and differently per OS. |
| 4 | **Keyboard hints impersonate unread counts.** | `<span class="kbd-hint">1</span>` … `9`, styled as badges beside nav labels. |
| 5 | **~400px of hero says nothing.** | "OBSERVATORY", *"The stack is listening."*, *"Reading the collective state of your agents…"* — mystical voice in an operator console, and "the stack" is more of the same rename residue. |
| 6 | **Loading, empty and failed are indistinguishable.** | `– –` serves as both loading and empty; failure is a bare "Could not load events" with no reason and no retry. |
| 7 | **Three `<link>`s to `fonts.googleapis.com`.** | Contradicts "nothing leaves your infrastructure" and degrades on an air-gapped install. |

Finding 1 is the most commercially damaging: it is on the first screen a buyer
sees, and it manufactures false confidence in a broken install.

## 3. Brand — SHIPPED

### 3.1 The mark

A **keep with a flame knocked out of it, and an ember left standing inside the
flame**. The name maps unusually well to the product — a keep is what holds,
and the fire is what must not go out, which is precisely context surviving the
death of a session.

Four directions were drawn and rendered before choosing (rounded-keep, hearth,
keystone, and four flame treatments within the chosen container). Two rejections
are worth recording because they are not obvious:

* **A symmetric teardrop reads as water, not fire** — universally. The first
  three marks all failed this way. Fire requires asymmetry: a leaning tip and an
  inner lick.
* **The chosen flame works because of topology, not detail.** A closed blob with
  a round bottom and a pointed top is the water schema. A shape with an enclosed
  void echoing its outer contour is the fire schema. Topology survives scaling;
  detail does not — which is why the mark still reads at 16px.

### 3.2 Assets — `dashboard/brand/`

Single source of truth for **both** the dashboard and the marketing website.

One deliberate exception: the topbar mark **is** inlined as a `<path>` in
`index.html`, because `currentColor` only resolves against a parent element and
an `<img>` has none (see decision 1 below). The inlined copy and `mark.svg`
carry identical geometry and must be kept in sync; both are commented to say so.
Everything else references the files.

| File | Colour | Use |
|---|---|---|
| `mark.svg` | `currentColor` | Inlined into the DOM. |
| `mark-ember.svg` | baked `#FF7A2F` | `<img>`, `background-image`, `og:image`, CMS, decks. |
| `favicon.svg` | baked `#FF7A2F` | `<link rel="icon">`, ≤ 24px. Optically sized. |
| `lockup.svg` | `currentColor` | Mark + wordmark. Needs DM Sans. |
| `README.md` | — | Usage rules, clearspace, colour roles. |

**Shipping them is a separate act from creating them — and there are TWO
delivery paths, which is the trap.**

* **k8s / CI** builds `docker/Dockerfile.dashboard`, which copies
  `dashboard/index.html` — a *file*, not the directory. Needs `COPY
  dashboard/brand/`.
* **compose (the VPS)** never builds that image at all. It runs **stock nginx**
  (`image: nginx:...-alpine@sha256:...`) with single-file bind mounts. Needs
  `- ./dashboard/brand:/usr/share/nginx/html/brand:ro`.

Fixing only the Dockerfile leaves the assets 404ing on every compose
deployment, and vice versa. A local `python -m http.server` in the source tree
serves them perfectly either way, which is exactly why the omission is easy to
miss: they work everywhere except where it counts.

This also explains an apparent contradiction worth recording:
`docker/dashboard-htpasswd.sh` and its `DASHBOARD_HTPASSWD` mechanism are **not
present in the running compose container and never execute there** — they exist
only in an image compose does not build. Compose authenticates from the
bind-mounted `./dashboard/.htpasswd` instead. The two mechanisms look
interchangeable and are not.

Three implementation decisions, all verified by rendering:

1. **`currentColor` does not survive `<img>`.** An SVG loaded via `<img src>`,
   `background-image`, or `<link rel="icon">` renders in an isolated document
   with no parent, so `currentColor` resolves to **black** — an invisible mark
   on a dark tab strip. Hence the baked-colour duplicates. A favicon *must*
   carry a baked colour.
2. **`favicon.svg` is a different drawing, not a scaled copy.** At 16px the
   container eats the flame's margin and the ember cutout closes to sub-pixel
   and fills in, collapsing the mark back into the droplet blob. The small
   variant drops the container and widens the flame/ember gap.
3. **One `<path>` with `fill-rule="evenodd"`, never `<mask>`.** A mask needs an
   `id`, and ids collide the moment two copies are inlined on one page — the
   second mark silently renders wrong. Crossing count does the work instead:
   keep → 1 → filled; keep + flame → 2 → knocked out; all three → 3 → the ember
   stands again.

### 3.3 Head tags

The dashboard currently wires **no favicon at all** — the console shows
`/favicon.ico → 404`. The brand commit adds:

```html
<link rel="icon" type="image/svg+xml" href="brand/favicon.svg">
<link rel="apple-touch-icon" href="brand/mark-ember.svg">
<meta name="theme-color" content="#0A0B0E">
<meta property="og:image" content="brand/mark-ember.svg">
```

`theme-color` is the deep background, not the accent — it tints browser chrome
to match the app, and an ember chrome bar would read as a warning. `og:image`
uses the baked-colour file for the reason in §3.2: link unfurlers render the SVG
standalone, where `currentColor` is black. `og:image` is also what makes the
mark work on the marketing site.

### 3.4 Wordmark

**Firekeep.** One word, capital F, no camel case. `FirekeepStack` is a bug
wherever it appears.

## 4. Palette — SHIPPED

The accent moves from electric blue to ember. A product called Firekeep with a
blue accent was always a mismatch, and if the brand is being built the accent
is the brand.

These are the **existing** token names in `:root`. An earlier draft of this
section invented `--accent-hi` / `--accent-dim` / `--ok` / `--warn` / `--danger`,
which exist nowhere in the codebase — following it would have minted parallel
tokens and left 588 lines of CSS still pointing at the originals. Use these:

| Token | Was | Now | Role |
|---|---|---|---|
| `--accent` | `#4C9AFF` | `#FF7A2F` | Brand + interaction only. |
| `--accent-2` | `#2F7FE6` | `#E2620F` | Gradient partner / pressed. |
| `--accent-bright` | `#82B8FF` | `#FFA05C` | Hover / raised. |
| `--accent-muted` | `rgba(76,154,255,.12)` | `rgba(255,122,47,.12)` | Tinted fills. |
| `--accent-glow` | `rgba(76,154,255,.06)` | `rgba(255,122,47,.06)` | Ambient wash. |
| `--accent-shadow` | `rgba(76,154,255,.22)` | `rgba(255,122,47,.22)` | Glow / drop-shadow. |
| `--pulse` | `#58B0FF` | `#FF9147` | Sparkline stroke. |
| `--pulse-glow` | `rgba(88,176,255,.26)` | `rgba(255,145,71,.26)` | Sparkline bloom. |
| `--green` | `#46C77F` | unchanged | OK state. |
| `--amber` | `#E0A93B` | `#E8C547` | amber → **yellow** (warn). |
| `--red` | `#E5635F` | `#F2555A` | → **truer red** (danger). |

`--green-muted` / `--amber-muted` / `--red-muted` follow their parents.

**`--pulse` also exists as a hard-coded JS fallback** (`'#58B0FF'`, in the
sparkline renderer). A token sweep that only reads CSS misses it, and the
sparkline would render blue on any browser where the computed var came back
empty.

Two rules follow, and they are load-bearing:

* **The accent never encodes state.** Ember sits ~27° of hue from the danger
  red. Letting one colour family mean both "brand" and "on fire" in an operator
  console is a hazard.
* **State is never colour-alone.** Every status carries a text label beside its
  dot (`up 34ms`, `degraded`, `down`). Required regardless of the palette
  change: red/orange/green is exactly the axis deuteranopia collapses, so the
  current dashboard is already unreadable to a colour-blind operator.

Warning moves to yellow and danger to a truer red so the brand orange sits alone
in its hue band.

## 5. Health honesty — PENDING *(separate commit)*

`renderHealthGrid` (`index.html:2147`) is rewritten to require **`2xx` and a
parseable JSON body**. The `404`/`405` tolerance is deleted.

**Observed before specifying** — all four endpoints were probed through the
tunnel rather than assumed:

| Service | `/health` | Body |
|---|---|---|
| cortex | `200 application/json` | `{"status":"ok","services":{"redis":{...},"graph":{...}}}` |
| bridge | `200 application/json` | `{"status":"ok","service":"bridge"}` |
| sentinel | `200 application/json` | `{"status":"ok","service":"sentinel","redis":"connected","collectors":{...}}` |
| relay | `200 application/json` | `{"status":"ok","service":"relay"}` |

This matters: had any returned `200` with an empty or non-JSON body, the new
rule would turn a *healthy* service red — replacing a false green with a false
red, the same defect pointing the other way. All four are JSON with a
`status` field, so the rule is safe as written.

It also makes `degraded` real rather than aspirational: cortex and sentinel
report **per-dependency** status inside a `200`, so a service can be reachable
and answering while its Redis or graph backend is down. That is precisely the
case the current binary up/down cannot express.

Four real states replace the current two:

| State | Condition | Presentation |
|---|---|---|
| `loading` | in flight | skeleton, amber dot |
| `up` | `2xx`, JSON parses, `status == "ok"`, no failing dependency | green dot, `up · NNms` |
| `degraded` | `2xx` and parses, but a nested dependency is not healthy | yellow dot, `degraded`, failing dependency named on hover |
| `down` | non-`2xx`, unparseable, or network failure | red dot, `down · <reason>` |

**Delete the `/` fallback** (`index.html:2175`). Probing showed `/` returns
**`401`** on an auth-enabled deployment, not `404` — so once `2xx` is required
that branch can only ever produce `down`, which the primary call already
produced. Keeping it would leave a branch that cannot change any outcome.
Note this corrects the original rationale for deleting the `404` tolerance:
the tolerance was not redundant *because* of the fallback, it was the only
reason a fallback response could ever be painted green.

**This ships as its own commit**, ahead of the restyle. It will make things go
red that currently look green — possibly on the author's own VPS. That is a
discovery about the infrastructure, and it must not be entangled with a
cosmetic change.

Regression test: a stubbed `404` response must produce `down`. The current
implementation must fail that test — if it passes against the old code, the
test is not testing anything.

## 6. Icons — PENDING

One inline SVG sprite, monochrome, `currentColor`, ~14 glyphs — one per nav
item. Replaces every HTML-entity glyph so nothing renders as an OS emoji.
Sprite lives in `index.html` (inline, so `currentColor` works) rather than in
`brand/`, because these are UI chrome, not brand assets.

## 7. Navigation — PENDING

The four existing groups are kept unchanged — they are already correct.

The `1`–`9` `kbd-hint` badges are demoted to muted monospace characters,
right-aligned, so they stop reading as unread counts. They remain visible
(they are genuinely useful) but stop lying.

## 8. Hero → status ribbon — PENDING

Delete `OBSERVATORY`, *"The stack is listening."* and *"Reading the collective
state of your agents…"*. Replace with a compact status ribbon: four service
chips with live state plus the at-a-glance counts, on one row.

**Measured, not estimated.** An earlier draft of this section claimed "reclaims
roughly 300px". The actual figure, from `getBoundingClientRect()` before and
after at a 1011px viewport:

| | before | after | delta |
|---|---|---|---|
| `.hero` height | 328px | 219px | −109px |
| Vital Signs top | y=472 | y=354 | −118px |

118px, not 300. The estimate was invented and roughly 2.5× the truth — worth
recording, because a spec that quotes unmeasured numbers trains its readers to
trust the next one.

The `FIREKEEP PULSE` sparkline is retained (150px → 104px) — it is the one part
of the hero that showed something. The `hero-summary` line was already
assembling real counts and is promoted from caption to primary readout, so the
band now carries data at the size the slogan used to occupy.

## 9. States — PENDING

Four distinct treatments, applied consistently across all 14 tabs:

* **loading** — skeleton rows, never `– –`
* **empty** — says what the surface holds and what to do to fill it
* **error** — the reason and a retry control, never a bare sentence
* **degraded** — its own treatment, not collapsed into error

## 10. Fonts — PENDING

Self-host DM Sans and JetBrains Mono as subset `woff2` under
`dashboard/brand/fonts/`, drop all three Google `<link>`s. Air-gapped installs
render correctly and "nothing leaves your infrastructure" becomes literally
true rather than nearly true.

**Ship `OFL.txt` per family.** Both fonts are SIL Open Font License 1.1.
Self-hosting is permitted, but the OFL requires the licence text to travel with
the font binaries — a redistribution obligation, and these binaries are
redistributed inside the customer's image. CI will not catch this: the
`Dependency licences` job covers Python only. Same category as the
GPL `html2text` → `markdownify` swap that was a commercial-readiness blocker.

## 11. Explicitly out of scope

**The `events` and `patterns` tabs stay for now.** The commercialization design
(§3.2) cuts pattern-engine statistics and Sentinel-derived surfaces from the
*supported* product, so the dashboard currently advertises subsystems that are
not intended to be sold. Removing them is a product decision, not a design one,
and is flagged here rather than actioned.

Also out of scope: any change to the 3,188 lines of JS beyond
`renderHealthGrid`, the state-rendering helpers, the hero/ribbon markup, and
colour literals embedded in JS (the `--pulse` fallback was one).

## 12. Risks

| Risk | Mitigation |
|---|---|
| The health fix turns the live VPS red. | Ships as its own commit, ahead of the restyle, so the finding is legible on its own. |
| Ember accent collides with danger red. | Warning → yellow, danger → truer red, accent barred from encoding state, and every state carries a text label. |
| `lockup.svg` renders wrong without DM Sans. | Documented; compose the lockup in HTML where the font cannot be guaranteed. Outlining the wordmark needs a font toolchain and is deliberately not faked. |
| A single-file bind mount serves stale content after a deploy. | Docker resolves a single-file mount to an INODE at container start. Writing in place (`>`) is seen; REPLACING the file (`cp`+`mv`, scp, editors that write-then-rename) is not — the host shows new content while the container serves the old file, silently. Write in place, or recreate the container. Cost 20min on the .htpasswd rotation. |
| Brand assets drift between dashboard and website. | `dashboard/brand/` is the single source; `mark.svg` and `mark-ember.svg` must be kept in sync if the path changes. |
