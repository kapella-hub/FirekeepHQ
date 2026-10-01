#!/usr/bin/env python3
"""Turn a recorded install into a self-contained terminal demo for the website.

Input is an asciinema-shaped cast written by scripts/installlab/lab.py — real
lines with real elapsed timings from a real container install. Output is one
HTML file with no external anything: no CDN, no font, no JS library, no network.

THE HONESTY RULES, because a marketing asset is exactly where they slip:

  1. Every line of output shown was produced by the recorded run. Nothing is
     written for effect. The ONLY synthesised text is the command the user
     types at the start, which is the scenario's own command line, typed.
  2. Lines are DROPPED, never edited. A 30,000-line transcript is mostly docker
     layer-pull chatter and uv progress bars redrawing themselves; cutting that
     is editing for length, and rewording what survives would not be.
  3. Idle time is compressed, VISIBLY, and the clock keeps real time. When the
     player skips four minutes of image pulls it says so and the elapsed
     counter jumps. A demo that silently plays a 9-minute install in 40 seconds
     is claiming a speed the product does not have.
  4. Lab scaffolding is dropped. The recording runs inside a container that has
     to `apt-get install docker.io` first; a user on their own box never sees
     that, so showing it would misrepresent the install in the other direction.

Usage:
    python scripts/demo/render.py \
        --cast scripts/installlab/.runs/server/oneshot.cast.json \
        --out  ../firekeep-site/demo/install.html
"""
from __future__ import annotations

import argparse
import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")
#: The WHOLE Braille Patterns block, not a handful of spinner glyphs. Listing
#: "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" by hand caught uv's spinner and missed uv's "⠿ Preparing
#: packages..." and compose's "⠿ qdrant [⠿⠿⠿⠿] Pulling", which are drawn from
#: the same block with different glyphs — 5,334 lines survived on that mistake.
#: Braille never appears in this product's real output, so the block is a safe
#: signature for "this is a progress widget".
PROGRESS_GLYPH = re.compile(r"[⠀-⣿]")

#: A run of dashes, equals or block elements is a bar being filled, whatever
#: drew it. uv's per-wheel download rows are the ones this catches.
PROGRESS_BAR = re.compile(r"-{6,}|={6,}|[█▉▊▋▌▍▎▏░▒▓]{2,}")

#: A join code is single-use and expires, but a live-looking credential in a
#: marketing asset teaches the wrong habit to everyone who watches it. Truncated
#: rather than removed, so the SHAPE of the handoff line is still visible.
JOIN_CODE = re.compile(r"(fk_join_[A-Za-z0-9_-]{8})[A-Za-z0-9_.-]+")

#: Noise that redraws itself or repeats per-layer. Dropped wholesale.
#
#: The compose entries are the big one and the least obvious. `docker compose`
#: REPAINTS its whole progress block on every tick, so a nine-minute pull emits
#: the same twelve "Pulled" lines roughly 1,900 times each — 35,895 surviving
#: lines and a 3.4MB page on the first attempt. Dropping them is the same act as
#: dropping a spinner frame: it is one status widget redrawing, not output. The
#: pull itself is still accounted for, out loud, by the compressed-gap marker.
NOISE = (
    "(download)", "Downloading", "Extracting", "Verifying Checksum",
    "Download complete", "Pull complete", "Already exists", "Waiting",
    "Pulling fs layer", "digest:", "status: Downloaded", "status: Image is up",
    "[+] Pulling", "[+] Running", "[+] Building", "[+] Creating",
    " Pulled ", " Skipped - ", " Created", " Started", " Healthy", " Recreated",
    "] Pulling", "Preparing packages", "Resolving dependencies",
)

#: The recording harness's own setup, which a real user never runs. Dropped so
#: the demo shows the product rather than the test rig.
SCAFFOLD = (
    "apt-get", "debconf:", "+ set +x", "+ apt-get", "docker info",
    "dpkg-preconfigure", "update-alternatives", "Setting up ", "Selecting previously",
    "Preparing to unpack", "Unpacking ", "Processing triggers",
)

#: Marks the boundaries of the part worth showing.
START_AFTER = "firekeep: fetching uv"
STOP_AFTER = "=== doctor exit="


@dataclass
class Frame:
    """One line of the demo, with the real clock reading when it appeared."""

    t: float
    text: str
    #: Seconds of real time skipped immediately BEFORE this line.
    skipped: float = 0.0


@dataclass
class Cast:
    frames: list[Frame] = field(default_factory=list)
    real_duration: float = 0.0
    command: str = ""
    #: Non-empty when the dist host was rewritten; rendered as a footnote on the
    #: page so the substitution is disclosed rather than assumed harmless.
    rewrote: str = ""


def load(path: Path) -> tuple[dict, list[tuple[float, str]]]:
    header: dict = {}
    events: list[tuple[float, str]] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        parsed = json.loads(line)
        if index == 0 and isinstance(parsed, dict):
            header = parsed
            continue
        if isinstance(parsed, list) and len(parsed) >= 3:
            events.append((float(parsed[0]), str(parsed[2])))
    return header, events


def clean(text: str) -> str:
    text = ANSI.sub("", text)
    text = JOIN_CODE.sub(r"\1…", text)
    return text.replace("\r", "").rstrip("\n")


def keep(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if PROGRESS_GLYPH.search(line) or PROGRESS_BAR.search(line):
        return False
    # Belt and braces for casts recorded before the encoding fix in lab.py: a
    # mis-decoded glyph becomes U+FFFD. One is enough to drop the line — this
    # product never emits a replacement character, so its presence means the
    # bytes were mangled, and a mangled line cannot be displayed legibly
    # whatever it once said. (A threshold of 3 was too lax: compose's mojibake
    # rows carried only two.)
    if "�" in line:
        return False
    if any(marker in line for marker in NOISE):
        return False
    if any(marker in line for marker in SCAFFOLD):
        return False
    # Anything shaped like a compose progress row that the NOISE list missed.
    if re.match(r"^\s*[✔✘⠿×]\s", line):
        return False
    # The lab's own scenario echo and section markers.
    if stripped.startswith("==="):
        return False
    if stripped.startswith("lab:"):
        return False
    return True


def _shape(line: str) -> str:
    """A line with every number flattened, for spotting repainted rows."""
    return re.sub(r"[0-9]+", "N", line.strip())


def distil(events: list[tuple[float, str]], *, gap: float) -> Cast:
    """Drop noise, then collapse any idle gap longer than `gap` seconds."""
    kept: list[tuple[float, str]] = []
    started = False
    for stamp, raw in events:
        for piece in clean(raw).split("\n"):
            if not started:
                if START_AFTER in piece:
                    started = True
                else:
                    continue
            if not keep(piece):
                continue
            # Collapse consecutive lines that differ only in a counter or a
            # duration -- "redis Pulled 41.5s" then "redis Pulled 42.1s" is one
            # row being repainted, and exact-match dedup never catches it.
            if kept and _shape(kept[-1][1]) == _shape(piece):
                continue
            kept.append((stamp, piece))
            if STOP_AFTER in piece:
                break

    frames: list[Frame] = []
    previous = kept[0][0] if kept else 0.0
    for stamp, text in kept:
        delta = stamp - previous
        skipped = delta - gap if delta > gap else 0.0
        frames.append(Frame(t=stamp, text=text, skipped=round(skipped, 1)))
        previous = stamp
    return Cast(frames=frames, real_duration=kept[-1][0] if kept else 0.0)


# --------------------------------------------------------------------------- html

PAGE = r"""<title>Firekeep — one command</title>
<style>
  /* Light is the base palette; dark is redefined for BOTH the explicit
     [data-theme] choice and the prefers-color-scheme default, so the page is
     never left borrowing the host's colours. */
  :root {
    --page:#f6f5f3; --ink:#1b1a18; --muted:#6b6560;
    --term-bg:#14110f; --term-ink:#e8e2d9; --term-dim:#8a8178;
    --ember:#e0642a; --flame:#f2a03d; --ok:#5bb98b; --rule:#dedad4;
  }
  :root:not([data-theme="light"]) { }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --page:#121110; --ink:#eceae7; --muted:#9a938c; --rule:#2b2825;
    }
  }
  :root[data-theme="dark"] {
    --page:#121110; --ink:#eceae7; --muted:#9a938c; --rule:#2b2825;
  }

  * { box-sizing:border-box; }
  body {
    margin:0; padding:clamp(16px,4vw,48px); background:var(--page); color:var(--ink);
    font:15px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  }
  .wrap { max-width:920px; margin:0 auto; }
  h1 { font-size:clamp(22px,3.4vw,30px); margin:0 0 6px; letter-spacing:-0.02em; }
  .sub { color:var(--muted); margin:0 0 22px; max-width:60ch; }

  .term {
    background:var(--term-bg); border-radius:10px; overflow:hidden;
    box-shadow:0 18px 50px rgba(0,0,0,.28); border:1px solid rgba(255,255,255,.06);
  }
  .bar {
    display:flex; align-items:center; gap:8px; padding:10px 14px;
    background:rgba(255,255,255,.045); border-bottom:1px solid rgba(255,255,255,.06);
  }
  .dot { width:11px; height:11px; border-radius:50%; }
  .bar .title {
    margin-left:8px; color:var(--term-dim); font-size:12px;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  .clock {
    margin-left:auto; color:var(--term-dim); font-size:12px;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    font-variant-numeric:tabular-nums;
  }
  /* The scroller is the only thing that scrolls; the page body never does
     horizontally, whatever a long line does. */
  .screen {
    height:clamp(300px,52vh,460px); overflow-y:auto; overflow-x:auto;
    padding:14px 16px 20px;
    font:13px/1.55 ui-mono, ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    color:var(--term-ink); white-space:pre; scrollbar-width:thin;
  }
  .screen::-webkit-scrollbar { width:8px; height:8px; }
  .screen::-webkit-scrollbar-thumb { background:rgba(255,255,255,.14); border-radius:4px; }

  .l-cmd    { color:#fff; }
  .l-cmd .p { color:var(--ember); }
  .l-fk     { color:var(--flame); }
  .l-ok     { color:var(--ok); }
  .l-prompt { color:#fff; }
  .l-head   { color:var(--flame); font-weight:600; }
  .skip {
    color:var(--term-dim); font-style:italic; opacity:.85;
    border-left:2px solid rgba(255,255,255,.14); padding-left:8px; margin:3px 0;
  }
  .cursor {
    display:inline-block; width:8px; height:15px; background:var(--flame);
    vertical-align:-2px; animation:blink 1.05s steps(1) infinite;
  }
  @keyframes blink { 50% { opacity:0; } }

  .controls { display:flex; gap:10px; align-items:center; margin-top:14px; flex-wrap:wrap; }
  button {
    font:inherit; font-size:13px; padding:7px 14px; border-radius:7px; cursor:pointer;
    border:1px solid var(--rule); background:transparent; color:var(--ink);
  }
  button:hover { border-color:var(--ember); color:var(--ember); }
  .note { color:var(--muted); font-size:12.5px; margin:0; }

  @media (prefers-reduced-motion: reduce) {
    .cursor { animation:none; }
  }
</style>

<div class="wrap">
  <h1>One command. Two questions.</h1>
  <p class="sub">
    A real recording of <code>curl -fsSL https://firekeep.ai/latest/install.sh | sh</code>
    on a clean Ubuntu box with Docker — client kit, server, and the machine enrolled
    against it. Nothing here is a mockup.
  </p>

  <div class="term">
    <div class="bar">
      <span class="dot" style="background:#ff5f57"></span>
      <span class="dot" style="background:#febc2e"></span>
      <span class="dot" style="background:#28c840"></span>
      <span class="title">root@vps — bash</span>
      <span class="clock" id="clock">0:00</span>
    </div>
    <div class="screen" id="screen" role="img" aria-label="__ARIA__"></div>
  </div>

  <div class="controls">
    <button id="replay">Replay</button>
    <button id="skip">Skip to end</button>
    <p class="note" id="note"></p>
  </div>

  <p class="note" style="margin-top:14px; max-width:70ch">__DISCLOSURE__</p>
</div>

<script>
const CAST = __CAST__;
const REAL = __REAL__;
const COMMAND = __COMMAND__;

const screenEl = document.getElementById("screen");
const clockEl  = document.getElementById("clock");
const noteEl   = document.getElementById("note");
const reduced  = matchMedia("(prefers-reduced-motion: reduce)").matches;

function mmss(s) {
  s = Math.max(0, Math.round(s));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

// Classification is presentation only — it colours a line, never rewrites it.
function classOf(text) {
  if (/^\\[OK\\]/.test(text) || /^\s+(Cortex|Bridge|Sentinel|Relay|Dashboard)\s+\\[OK\\]/.test(text)) return "l-ok";
  if (/^firekeep:/.test(text)) return "l-fk";
  if (/^(Where is your Firekeep server\\?|Choose|Agent identity)/.test(text)) return "l-prompt";
  if (/^(=+|Firekeep is running!|\s*Firekeep Installer)/.test(text)) return "l-head";
  return "";
}

let timer = null;

function reset() {
  clearTimeout(timer);
  screenEl.innerHTML = "";
  clockEl.textContent = "0:00";
  noteEl.textContent = "";
}

function line(text, cls) {
  const div = document.createElement("div");
  if (cls) div.className = cls;
  div.textContent = text;
  screenEl.appendChild(div);
  screenEl.scrollTop = screenEl.scrollHeight;
  return div;
}

function renderAll() {
  reset();
  const cmd = line("", "l-cmd");
  cmd.innerHTML = '<span class="p">$</span> ' + COMMAND.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
  for (const f of CAST) {
    if (f.skipped > 0) line("⏩ " + mmss(f.skipped) + " of image pulls and model download", "skip");
    line(f.text, classOf(f.text));
  }
  clockEl.textContent = mmss(REAL);
  noteEl.textContent = "Real elapsed: " + mmss(REAL) + ".";
}

function typeCommand(done) {
  const el = line("", "l-cmd");
  const cursor = document.createElement("span");
  cursor.className = "cursor";
  let i = 0;
  (function step() {
    el.innerHTML = '<span class="p">$</span> ' +
      COMMAND.slice(0, i).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
    el.appendChild(cursor);
    if (i++ <= COMMAND.length) timer = setTimeout(step, 26);
    else { cursor.remove(); timer = setTimeout(done, 420); }
  })();
}

function play() {
  if (reduced) { renderAll(); return; }
  reset();
  typeCommand(() => {
    let i = 0;
    (function step() {
      if (i >= CAST.length) {
        clockEl.textContent = mmss(REAL);
        noteEl.textContent = "Real elapsed: " + mmss(REAL) + ". Playback compresses the waiting.";
        return;
      }
      const f = CAST[i++];
      if (f.skipped > 0) line("⏩ " + mmss(f.skipped) + " of image pulls and model download", "skip");
      line(f.text, classOf(f.text));
      clockEl.textContent = mmss(f.t);
      // Pace by the REAL inter-line gap, clamped: fast enough to watch, slow
      // enough that a burst of output still reads as a burst.
      const gap = i < CAST.length ? Math.min(340, Math.max(18, (CAST[i].t - f.t) * 90)) : 0;
      timer = setTimeout(step, gap);
    })();
  });
}

document.getElementById("replay").addEventListener("click", play);
document.getElementById("skip").addEventListener("click", () => { clearTimeout(timer); renderAll(); });

// Start when it is actually on screen, so a hero demo is not already finished
// by the time the visitor scrolls to it.
if (reduced) {
  renderAll();
} else {
  const io = new IntersectionObserver((entries) => {
    if (entries.some(e => e.isIntersecting)) { io.disconnect(); play(); }
  }, { threshold: 0.35 });
  io.observe(screenEl);
}
</script>
"""


def _disclosure(cast: Cast) -> str:
    """The footnote that makes every edit to the recording visible to a reader.

    A marketing page claiming "this is a real recording" has to be able to say
    exactly what was done to it, or the claim is worth nothing.
    """
    skipped = sum(f.skipped for f in cast.frames)
    parts = [
        f"Real run: {cast.real_duration:.0f}s on a clean Ubuntu container with Docker."
    ]
    if skipped > 0:
        parts.append(
            f"{skipped:.0f}s of image pulls and model download are compressed in "
            "playback and marked ⏩ where they occur."
        )
    parts.append(
        "Progress bars and repeated status rows are dropped; no line is reworded."
    )
    if cast.rewrote:
        parts.append(
            f"One substitution: the distribution host ({cast.rewrote}), because the "
            "recording installs a build that is not published yet."
        )
    parts.append("Join code truncated.")
    return " ".join(parts)


def render(cast: Cast, *, out: Path) -> Path:
    payload = [{"t": round(f.t, 2), "text": f.text, "skipped": f.skipped} for f in cast.frames]
    aria = (
        "Terminal recording: one command installs the Firekeep client, provisions "
        "the server, enrols the machine, and firekeep doctor reports every check OK."
    )
    page = (
        PAGE.replace("__CAST__", json.dumps(payload, ensure_ascii=False))
        .replace("__REAL__", json.dumps(round(cast.real_duration, 1)))
        .replace("__COMMAND__", json.dumps(cast.command))
        .replace("__ARIA__", html.escape(aria, quote=True))
        .replace("__DISCLOSURE__", html.escape(_disclosure(cast)))
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8", newline="\n")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cast", type=Path,
        default=REPO / "scripts" / "installlab" / ".runs" / "server" / "oneshot.cast.json",
    )
    parser.add_argument("--out", type=Path, default=REPO / "scripts" / "demo" / "install.html")
    parser.add_argument(
        "--command", default="curl -fsSL https://firekeep.ai/latest/install.sh | sh",
        help="the command shown being typed (the scenario's own, not decoration)",
    )
    parser.add_argument(
        "--gap", type=float, default=4.0,
        help="idle seconds beyond which a wait is compressed and labelled",
    )
    parser.add_argument(
        "--rewrite-base", metavar="OLD=NEW", default="http://dist:8000=https://firekeep.ai",
        help=(
            "rewrite the distribution host in captured output. The lab records "
            "against a LOCAL dist server, because the whole point is to install "
            "a build that is not published yet — so the recording says "
            "http://dist:8000 where a user's would say firekeep.ai. This is the "
            "one substitution made to captured text; it is narrow (host only), "
            "mechanical, and disclosed in a footnote on the rendered page. Pass "
            "an empty string to disable it and show the lab host verbatim."
        ),
    )
    args = parser.parse_args(argv)

    if not args.cast.is_file():
        raise SystemExit(
            f"demo: no cast at {args.cast}\n"
            "      record one:  python scripts/installlab/lab.py server --scenario oneshot"
        )
    header, events = load(args.cast)

    # Refuse a double-encoded cast rather than rendering it. UTF-8 bytes decoded
    # as cp1252 produce "âœ”" and "â£¿" instead of "✔" and "⣿" -- valid UTF-8, no
    # U+FFFD, nothing that looks broken to a filter. The progress-widget rules
    # key off the real glyphs, so mojibake sails straight through them and 3,291
    # spinner frames end up in a marketing asset. Silent corruption in the one
    # artifact nobody re-reads before publishing is worth a hard stop.
    sample = "".join(text for _, text in events[:4000])
    if "âœ" in sample or "â£" in sample or "â " in sample:
        raise SystemExit(
            "demo: this cast is double-encoded (UTF-8 read as cp1252) — you will\n"
            "      see 'âœ”' where '✔' belongs. It was recorded before lab.py\n"
            "      pinned encoding='utf-8'. Re-record it:\n"
            "        python scripts/installlab/lab.py server --scenario oneshot"
        )

    cast = distil(events, gap=args.gap)
    cast.command = args.command

    if args.rewrite_base and "=" in args.rewrite_base:
        old, new = args.rewrite_base.split("=", 1)
        touched = 0
        for frame in cast.frames:
            if old in frame.text:
                frame.text = frame.text.replace(old, new)
                touched += 1
        if touched:
            cast.rewrote = f"{old} → {new}"
            print(f"demo: rewrote the dist host on {touched} line(s): {cast.rewrote}")
    if header.get("duration"):
        cast.real_duration = float(header["duration"])

    if not cast.frames:
        raise SystemExit("demo: the cast distilled to nothing — check START_AFTER/STOP_AFTER")

    out = render(cast, out=args.out)
    skipped = sum(f.skipped for f in cast.frames)
    print(f"demo: {len(events)} recorded events -> {len(cast.frames)} shown lines")
    print(f"demo: real run {cast.real_duration:.0f}s, {skipped:.0f}s of it compressed and labelled")
    print(f"demo: wrote {out} ({out.stat().st_size / 1024:.0f} KB, self-contained)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
