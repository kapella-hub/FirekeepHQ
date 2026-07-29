# Firekeep brand assets

The mark is a **keep with a flame knocked out of it, and an ember left standing
inside the flame**. Fire is what the product does — context that survives when
the session dies — and the keep is what holds it.

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
It is a different drawing of the same mark. At 16px the rounded-square keep eats
the margin the flame needs and the ember cutout closes to sub-pixel and fills
in, which collapses the mark back into a solid blob — and a solid blob with a
round bottom and a pointed top is read as a *water droplet* by everyone, every
time. The small variant drops the container, lets the flame fill the box, and
widens the flame/ember gap. Same mark, drawn for the size.

## Why one path with `fill-rule="evenodd"` instead of a `<mask>`

A mask needs an `id`, and ids collide the moment two copies are inlined on one
page — the second mark silently renders wrong. Each file is a single `<path>`
with two or three subpaths and `fill-rule="evenodd"`, so it can be dropped
anywhere, any number of times, with no id namespace at all.

Crossing count does the work: inside the keep only → 1 → filled; inside the
keep and the flame → 2 → knocked out; inside all three → 3 → the ember stands
again.

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
NexusStack rename that survived into the shipped header, with the dead half
highlighted in the accent colour. If you find it anywhere, it is a bug.

`lockup.svg` uses live text rather than outlines, so it needs DM Sans available.
Where the font cannot be guaranteed, compose the lockup in HTML — `mark.svg`
plus a styled element — rather than shipping the file and hoping. Outlining the
wordmark needs a font toolchain and is deliberately not faked here.
