# Dashboard stills

Captured from a real Firekeep dashboard at 1440×900 (device scale) against a local
stack, with `scripts/demo/seed.py` providing the content.

| File | Shows | Caption it needs |
|---|---|---|
| `firekeep-01-overview.png` | Overview — four services green, 8 memories, the memory graph | **Demo data** |
| `firekeep-02-memory.png` | Recall returning the deploy memory at 100% relevance | **Demo data** |

## Both of these are seeded

The memories are invented (`northwind-api`, a fictional service). Any use of these
images — site, deck, video, README — carries a visible "demo data" caption. The
dashboard is real; the content in it is not, and the difference is not something a
viewer can infer.

## Devices was captured and deleted, deliberately

`Devices` renders the operator's own enrolled machines — real hostnames and real
credential identifiers. It photographs well and it is not publishable. If a devices
still is wanted, capture it on a throwaway stack whose devices were all enrolled for
the shot, never on a machine anyone actually uses.

## Known blemish

Attribution reads `unknown` rather than the `created_by` the seed sets. Cosmetic for
a still; worth chasing before anyone crops in on that column.

## Re-capture

    python scripts/demo/seed.py          # 8 memories, tagged demo-data
    # dashboard at http://127.0.0.1:8040 (basic auth: dashboard/.htpasswd.cred)
    python scripts/demo/seed.py --purge  # take them back out
