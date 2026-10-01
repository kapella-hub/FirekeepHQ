# Firekeep launch video — script, shot list, storyboard

**Audience:** a developer evaluating Firekeep. They found the site, they are
sceptical, and they will decide in under a minute whether this is real.

**Length:** 62 seconds. **Sound:** optional — every claim is legible with audio off.
**Deliverable:** this document is the only path here that ends in an MP4. Everything
in the SOURCE column already exists; the video is an edit of assets, not a shoot.

---

## The one rule

**Nothing on screen was made up.** Every terminal frame comes from
`scripts/installlab/.runs/server/oneshot.cast.json`, a recorded container install.
Every dashboard frame is the real dashboard. Where data is seeded for the shot, the
lower third says so.

The reason to be strict: the audience is developers who have watched a hundred
tools demo a happy path that does not survive contact with their machine. The only
durable advantage here is that ours is a recording, so any frame can be reproduced
on request. Fabricate one shot and that advantage is gone.

---

## Cold open — 0:00–0:07

| | |
|---|---|
| **Source** | `scripts/demo/install.html` (real cast), first frames |
| **Visual** | Black. A terminal, nothing else. The command types itself. |
| **On screen** | `$ curl -fsSL https://firekeep.ai/latest/install.sh \| sh` |
| **VO** | *(silence — let it type)* |

> **Note.** No logo, no title card, no "introducing". A developer who wanted a brand
> film did not click this. The first frame should be the thing they would type.

---

## The two questions — 0:07–0:20

| | |
|---|---|
| **Source** | Real cast — the wizard prompt |
| **Visual** | Output scrolls. Settle on the menu. Hold. |
| **On screen** | `Where is your Firekeep server?`<br>`1  Set one up on this machine`<br>`2  I have a join code`<br>`3  It is already running`<br>`4  Not yet` |
| **Lower third** | **Two questions. That is the whole install.** |
| **VO** | "It asks who you are, and where your server is. That's it." |

> The pause on `Choose [1]:` is the most important beat in the video. It is the
> moment the viewer realises they are not about to be asked for a VPS IP, a
> database password, and an API key that does not exist yet.

---

## It builds itself — 0:20–0:34

| | |
|---|---|
| **Source** | Real cast — installer output through to the health table |
| **Visual** | Fast scroll. Slow to normal speed on each `[OK]`. |
| **On screen** | `[OK] Host address: 172.19.0.3 (detected)`<br>`[OK] Neo4j password generated`<br>`[OK] Vault encryption key generated`<br>then `Cortex API [OK] · Bridge [OK] · Sentinel [OK] · Relay [OK] · Dashboard [OK]` |
| **Lower third** | **Detected, or generated. Never asked.** |
| **VO** | "Everything it needs, it works out or generates. The address, the database password, the vault key." |

> Include the `⏩ 4m of image pulls` marker if the edit compresses that gap. Cutting
> silently to a finished stack implies a speed the product does not have, and the
> marker costs one second and buys the whole video its credibility.

---

## The part that used to be broken — 0:34–0:44

| | |
|---|---|
| **Source** | Real cast — self-enrolment and the handoff line |
| **Visual** | Settle. This is the payoff. |
| **On screen** | `firekeep: this machine is connected.`<br><br>`To add your laptop, run THIS on it:`<br>`curl -fsSL https://firekeep.ai/latest/install.sh \| FIREKEEP_JOIN=fk_join_… sh` |
| **Lower third** | **The box enrols itself. Then hands you the next one.** |
| **VO** | "The machine that just built the server is already talking to it. And it gives you the line for your laptop." |

> Blur or truncate the join code. It is single-use and expired, but a live-looking
> credential on screen teaches the wrong habit to everyone watching.

---

## Proof — 0:44–0:52

| | |
|---|---|
| **Source** | Real cast — `firekeep doctor` |
| **Visual** | The full green column. Let it sit for a beat with no motion. |
| **On screen** | `[OK] cortex · bridge · sentinel · relay · versions · agent-id · config-perms · credential-expiry …` |
| **Lower third** | **`firekeep doctor` — every check, first try.** |
| **VO** | "One command in. Nothing else typed." |

---

## What it is — 0:52–1:02

| | |
|---|---|
| **Source** | Dashboard stills (seeded demo data — label it) + the explainer page's architecture panel |
| **Visual** | Cut from terminal to the dashboard: memories, then the graph view. Slow push in. |
| **On screen** | `Persistent memory for your coding agents.`<br>`Self-hosted. Your data never leaves your box.` |
| **Lower third** | **Demo data shown** |
| **VO** | "Firekeep gives your agents memory that survives the session — running on hardware you own." |
| **End card** | `firekeep.ai` on the ember mark. No music sting. |

---

## Shot inventory

| # | Shot | Source asset | Status |
|---|---|---|---|
| 1 | Command typing | `scripts/demo/install.html` | ✅ real recording |
| 2 | The menu | same cast | ✅ real recording |
| 3 | `[OK]` derivations | same cast | ✅ real recording |
| 4 | Health table | same cast | ✅ real recording |
| 5 | Self-enrol + handoff | same cast | ✅ real recording |
| 6 | `firekeep doctor` | same cast | ✅ real recording |
| 7 | Dashboard memories | Playwright stills | seeded demo data |
| 8 | Graph view | Playwright stills | seeded demo data |
| 9 | End card | `brand/mark-ember.svg` | ✅ exists |

Shots 1–6 are one continuous recording. An editor can produce the whole first
52 seconds by screen-capturing `scripts/demo/install.html` playing, with no
compositing at all.

---

## Things to say, and things not to

**Say:** self-hosted; your data stays on your hardware; one command; works on
Ubuntu, Debian, Alpine, Fedora, Rocky, Arch and openSUSE (all in CI); the client
runs on Windows too.

**Do not say:** "in seconds" (the install is minutes, and the video shows the
marker admitting it); "zero configuration" (there are two questions, and the video
shows both); "works everywhere" (macOS runs the client but is not in the CI matrix,
and the site says so — the video must not contradict the docs); any figure for
users, teams or memory volume that nobody has measured.

**Uncomfortable but worth keeping:** the compressed-time marker. Every instinct in
video editing says remove it. It is the single most persuasive frame in the piece,
because it proves the rest was not trimmed the same way.

---

## Alternate cuts

- **0:15 social clip** — shots 1, 2, 6. Command, menu, green doctor. No VO. The
  menu is the hook; it is the frame that makes a developer stop scrolling because
  they recognise the install they expected not to get.
- **0:30 docs embed** — shots 1–6 with no lower thirds. Already effectively built:
  it is `scripts/demo/install.html` on autoplay.
