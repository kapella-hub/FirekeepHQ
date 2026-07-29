# Dashboard redesign + brand identity — design

**Date:** 2026-07-28
**Status:** design approved, not yet implemented
**Scope:** `dashboard/` — `index.html`, new `dashboard/brand/`, `docker/Dockerfile.dashboard`

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
step. `docker/Dockerfile.dashboard` remains a single `COPY`.

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
| 2 | **The brand is still the previous product.** | `index.html:604` `<div class="logo-mark">N</div>` (N for NexusStack); `:605` `Firekeep<span>Stack</span>` — a find-and-replace artifact, with the dead half highlighted in the accent colour. `<title>` is correct. |
| 3 | **Icons are half geometry, half emoji.** | `&#9673;` ◉ and `&#8644;` ⇄ are monochrome glyphs; `&#9889;` ⚡ and `&#10067;` ❓ are emoji-presentation codepoints that render full-colour and differently per OS. |
| 4 | **Keyboard hints impersonate unread counts.** | `<span class="kbd-hint">1</span>` … `9`, styled as badges beside nav labels. |
| 5 | **~400px of hero says nothing.** | "OBSERVATORY", *"The stack is listening."*, *"Reading the collective state of your agents…"* — mystical voice in an operator console, and "stack" is more NexusStack residue. |
| 6 | **Loading, empty and failed are indistinguishable.** | `– –` serves as both loading and empty; failure is a bare "Could not load events" with no reason and no retry. |
| 7 | **Three `<link>`s to `fonts.googleapis.com`.** | Contradicts "nothing leaves your infrastructure" and degrades on an air-gapped install. |

Finding 1 is the most commercially damaging: it is on the first screen a buyer
sees, and it manufactures false confidence in a broken install.

## 3. Brand

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
The mark is not redrawn inline in `index.html`.

| File | Colour | Use |
|---|---|---|
| `mark.svg` | `currentColor` | Inlined into the DOM. |
| `mark-ember.svg` | baked `#FF7A2F` | `<img>`, `background-image`, `og:image`, CMS, decks. |
| `favicon.svg` | baked `#FF7A2F` | `<link rel="icon">`, ≤ 24px. Optically sized. |
| `lockup.svg` | `currentColor` | Mark + wordmark. Needs DM Sans. |
| `README.md` | — | Usage rules, clearspace, colour roles. |

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

### 3.3 Wordmark

**Firekeep.** One word, capital F, no camel case. `FirekeepStack` is a bug
wherever it appears.

## 4. Palette

The accent moves from electric blue to ember. A product called Firekeep with a
blue accent was always a mismatch, and if the brand is being built the accent
is the brand.

| Token | Value | Role |
|---|---|---|
| `--accent` | `#FF7A2F` | Brand + interaction only. |
| `--accent-hi` | `#FFA05C` | Hover / raised. |
| `--accent-dim` | `#C2560D` | Pressed; `#E2620F` on light. |
| `--ok` | `#46C77F` | unchanged |
| `--warn` | `#E8C547` | amber → **yellow** |
| `--danger` | `#F2555A` | → **truer red** |

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

## 5. Health honesty *(separate commit)*

`renderHealthGrid` (`index.html:2147`) is rewritten to require **`2xx` and a
parseable body**. The `404`/`405` tolerance is deleted: all four services
register `/health` and `/version`, and the function already falls back to `/`
on failure, so the tolerance protects against nothing while destroying the
signal.

Four real states replace the current two:

| State | Condition | Presentation |
|---|---|---|
| `loading` | in flight | skeleton, amber dot |
| `up` | `2xx` + parseable body | green dot, `up · NNms` |
| `degraded` | `2xx` but body reports a failing dependency | yellow dot, `degraded`, reason on hover |
| `down` | non-`2xx`, unparseable, or network failure | red dot, `down · <reason>` |

**This ships as its own commit**, ahead of the restyle. It will make things go
red that currently look green — possibly on the author's own VPS. That is a
discovery about the infrastructure, and it must not be entangled with a
cosmetic change.

Regression test: a stubbed `404` response must produce `down`. The current
implementation must fail that test — if it passes against the old code, the
test is not testing anything.

## 6. Icons

One inline SVG sprite, monochrome, `currentColor`, ~14 glyphs — one per nav
item. Replaces every HTML-entity glyph so nothing renders as an OS emoji.
Sprite lives in `index.html` (inline, so `currentColor` works) rather than in
`brand/`, because these are UI chrome, not brand assets.

## 7. Navigation

The four existing groups are kept unchanged — they are already correct.

The `1`–`9` `kbd-hint` badges are demoted to muted monospace characters,
right-aligned, so they stop reading as unread counts. They remain visible
(they are genuinely useful) but stop lying.

## 8. Hero → status ribbon

Delete `OBSERVATORY`, *"The stack is listening."* and *"Reading the collective
state of your agents…"*. Replace with a compact status ribbon: four service
chips with live state plus the at-a-glance counts, on one row.

Reclaims roughly 300px so real data sits above the fold. The `FIREKEEP PULSE`
sparkline is retained — it is the one piece of the hero that shows something.

## 9. States

Four distinct treatments, applied consistently across all 14 tabs:

* **loading** — skeleton rows, never `– –`
* **empty** — says what the surface holds and what to do to fill it
* **error** — the reason and a retry control, never a bare sentence
* **degraded** — its own treatment, not collapsed into error

## 10. Fonts

Self-host DM Sans and JetBrains Mono as subset `woff2` under
`dashboard/brand/fonts/`, drop all three Google `<link>`s. Air-gapped installs
render correctly and "nothing leaves your infrastructure" becomes literally
true rather than nearly true.

## 11. Explicitly out of scope

**The `events` and `patterns` tabs stay for now.** The commercialization design
(§3.2) cuts pattern-engine statistics and Sentinel-derived surfaces from the
*supported* product, so the dashboard currently advertises subsystems that are
not intended to be sold. Removing them is a product decision, not a design one,
and is flagged here rather than actioned.

Also out of scope: any change to the 3,188 lines of JS beyond
`renderHealthGrid` and the state-rendering helpers.

## 12. Risks

| Risk | Mitigation |
|---|---|
| The health fix turns the live VPS red. | Ships as its own commit, ahead of the restyle, so the finding is legible on its own. |
| Ember accent collides with danger red. | Warning → yellow, danger → truer red, accent barred from encoding state, and every state carries a text label. |
| `lockup.svg` renders wrong without DM Sans. | Documented; compose the lockup in HTML where the font cannot be guaranteed. Outlining the wordmark needs a font toolchain and is deliberately not faked. |
| Brand assets drift between dashboard and website. | `dashboard/brand/` is the single source; `mark.svg` and `mark-ember.svg` must be kept in sync if the path changes. |
