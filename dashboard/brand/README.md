# Firekeep brand assets

The mark is **the Beacon** (adopted 2026-08-14): a signal fire in its bowl —
the fire you keep lit for others, the one agents navigate by. Fire is what the
product does — context that survives when the session dies — and the beacon is
why you keep it: so the next session, the next agent, the next teammate can
find it. The flame contour is unchanged through every mark before it (the
keep, retired 2026-08-13; the Vault, retired 2026-08-14): the vessels change,
the fire does not. The flame/bowl gap is deliberate — kept, not held down.

These files are the single source of truth for both the dashboard and the
website. Do not redraw the mark inline in `index.html`.

## Files

| File | Colour | Use it for |
|---|---|---|
| `mark.svg` | `currentColor` | The mark, **inlined into the DOM**. Takes the surrounding colour, so one file serves ember, mono and grey. |
| `mark-ember.svg` | baked `#FF7A2F` | Every context that cannot supply a colour: `<img src>`, CSS `background-image`, `og:image`, CMS uploads, decks, README badges. |
| `favicon.svg` | baked `#FF7A2F` | `<link rel="icon">`, tab strips, anything ≤ 24px. **Optically sized** — see below. |
| `lockup.svg` | `currentColor` | Mark + wordmark, horizontal. Needs DM Sans present. |

## The two rules that are easy to get wrong

**1. `currentColor` does not survive `<img>`.**
An SVG loaded through `<img src="mark.svg">`, `background-image`, or
`<link rel="icon">` is rendered in an isolated document with no parent element,
so it inherits no `color` and `currentColor` resolves to **black**. On the dark
dashboard that is an invisible mark. This was verified by rendering, not
assumed. Inline the SVG (or `<use>` it from an inline sprite) when you want
`currentColor`; otherwise reach for `mark-ember.svg`.

**2. `favicon.svg` is not `mark.svg` scaled down.**
It is a different drawing of the same mark. At 16px the foot is sub-pixel
(dropped), the bowl is drawn shallower so the flame gets the box, the flame is
solid — the inner ember cut would close up and read as noise at tab size —
and it is flat `#FF7A2F`, because a gradient bands at 16px. Same mark, drawn
for the size.

## Ids: who may carry one

Ids collide the moment two copies of a file are inlined on one page — the
second copy silently renders wrong. So the split follows how each file is
loaded:

- `mark.svg` and `lockup.svg` are made for INLINING and carry **no ids of any
  kind** — no mask, no gradient. The flame keeps its inner ember cut as an
  evenodd subpath (that detail is what a one-colour mark has instead of the
  gradient), and the bowl and foot are plain paths.
- `mark-ember.svg` is always loaded standalone (`<img>`, `og:image`) — an
  isolated document with a private id space — so it may carry the flame's
  ember-ramp `<linearGradient>` id. If you ever inline it, namespace the id
  first. `favicon.svg` is flat and id-free by design.

## Colour

| Token | Value | Role |
|---|---|---|
| `--accent` | `#FF7A2F` | Brand and **interaction only** — active nav, links, focus rings. |
| `--accent-hi` | `#FFA05C` | Hover / raised. |
| `--accent-dim` | `#C2560D` | Pressed / on light backgrounds where `#FF7A2F` is too hot (`#E2620F` also reads well). |

**The accent never encodes state.** Health, severity and status use
`--ok` / `--warn` / `--danger` exclusively. Brand orange sits close enough to
danger red that letting it mean "fine" and "on fire" in the same console is a
hazard — and state is never colour-alone anyway, it always carries a text label.

## Clearspace

Half the mark's height on every side. In the lockup that is 16 units in a
32-unit viewBox. Don't crowd it and don't put it on a busy photograph.

## Wordmark

**Firekeep.** One word, capital F, no camel case.

There is no "FirekeepStack" — that string was a find-and-replace artifact of the
rename that survived into the shipped header, with the dead half highlighted in
the accent colour. If you find it anywhere, it is a bug.

`lockup.svg` uses live text rather than outlines, so it needs DM Sans available.
Where the font cannot be guaranteed, compose the lockup in HTML — `mark.svg`
plus a styled element — rather than shipping the file and hoping. Outlining the
wordmark needs a font toolchain and is deliberately not faked here.
