"""Assert no container is running close to its memory cap.

Reads `docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}'` on stdin.

Exists because the smoke test measured cortex-beat at 116.9MiB against a 128MiB
limit (91%) - and nothing reported it. That failure mode is silent: the beat
scheduler gets OOM-killed and periodic tasks simply stop firing.
"""
from __future__ import annotations

import re
import sys

CEILING_PCT = 85.0

_UNITS = {"b": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12,
          "kib": 2**10, "mib": 2**20, "gib": 2**30, "tib": 2**40}
_VAL = re.compile(r"^\s*([\d.]+)\s*([a-zA-Z]+)\s*$")


def to_bytes(text: str) -> float | None:
    m = _VAL.match(text)
    if not m:
        return None
    mult = _UNITS.get(m.group(2).lower())
    return float(m.group(1)) * mult if mult else None


def main() -> int:
    rows, worst, failures = [], 0.0, []
    for line in sys.stdin:
        if "/" not in line or not line.strip():
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 2:
            parts = re.split(r"\s{2,}", line.strip(), maxsplit=1)
        if len(parts) < 2:
            continue
        name, usage = parts[0].strip(), parts[1]
        used_s, _, lim_s = usage.partition("/")
        used, limit = to_bytes(used_s), to_bytes(lim_s)
        if used is None or limit is None or limit <= 0:
            continue
        pct = used / limit * 100
        rows.append((pct, name, used_s.strip(), lim_s.strip()))
        worst = max(worst, pct)
        if pct > CEILING_PCT:
            failures.append((name, used_s.strip(), lim_s.strip(), pct))

    if not rows:
        print("::error::no container memory rows parsed - has the stats format changed?")
        return 1

    print(f"{'CONTAINER':<34}{'USED':>12}{'LIMIT':>12}{'PCT':>8}")
    for pct, name, used_s, lim_s in sorted(rows, reverse=True):
        flag = "  <-- OVER" if pct > CEILING_PCT else ""
        print(f"{name:<34}{used_s:>12}{lim_s:>12}{pct:>7.1f}%{flag}")
    print(f"\nparsed {len(rows)} containers; worst {worst:.1f}% of cap "
          f"(ceiling {CEILING_PCT:.0f}%)")

    for name, used_s, lim_s, pct in failures:
        print(f"::error::{name} at {pct:.1f}% of its memory limit "
              f"({used_s} / {lim_s}). Raise the limit in docker-compose.yml - an "
              f"OOM kill here is silent.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
