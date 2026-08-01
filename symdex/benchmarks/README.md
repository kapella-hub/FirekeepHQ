# Symdex benchmarks

Benchmarks in this directory make narrow, reproducible claims. They do not
measure whole-session token use unless the protocol explicitly says so.

## Polyglot targeted-symbol benchmark

`polyglot_symbol_benchmark.py` measures a common bounded operation: retrieve a
known symbol through Symdex instead of reading its entire containing file.

The protocol:

- pins one public repository for every built-in language in
  `polyglot_repos.json`;
- indexes Python, JavaScript, TypeScript, Go, Rust, Java, PHP, C, C#, Ruby,
  Kotlin, and Swift;
- excludes test, fixture, example, and benchmark paths;
- selects 30 eligible functions, methods, classes, or types per language by a
  stable, file-balanced SHA-256 ordering;
- compares the complete stable `get_symbol` response with sending the complete
  file containing that symbol;
- counts both contexts with `cl100k_base` and `o200k_base`; and
- verifies the indexed file and returned source byte-for-byte against the
  pinned Git blob.

The August 1, 2026 run produced:

| Language | Reference project | Sampled files | Mean reduction (`cl100k_base`) | Exact Git source |
|---|---|---:|---:|---:|
| Python | Click | 17 | 82.01% | 30/30 |
| JavaScript | Express | 6 | 83.13% | 30/30 |
| TypeScript | p-queue | 5 | 60.39% | 30/30 |
| Go | Cobra | 18 | 80.47% | 30/30 |
| Rust | clap | 30 | 69.64% | 30/30 |
| Java | Gson | 30 | 46.40% | 30/30 |
| PHP | Guzzle | 30 | 21.74% | 30/30 |
| C | jq | 29 | 67.35% | 30/30 |
| C# | Humanizer | 30 | 0.55% | 30/30 |
| Ruby | Rack | 30 | 32.95% | 30/30 |
| Kotlin | Moshi | 30 | 57.12% | 30/30 |
| Swift | Swift Argument Parser | 30 | 56.27% | 30/30 |
| **Balanced result** | **12 projects** | **285** | **54.83%** | **360/360** |

The arithmetic mean is the primary metric because every language contributes
the same 30 samples. The file-cluster bootstrap 95% interval was 44.37% to
60.01%, and the `o200k_base` mean was 54.54%. The median was 78.42%. The pooled
total-token reduction was 84.85%, but it is secondary because large files exert
more influence on it.

Results varied substantially. In 58 of 360 lookups, the serialized Symdex
response used more tokens than the baseline because the entire source file was
already small; the worst individual result was 220% more. Reporting those cases
is part of the protocol rather than filtering them out.

This result supports the scoped statement:

> Across 360 direct known-symbol lookups in 12 pinned open-source repositories,
> the stable Symdex response had a 54.8% lower estimated context-token count on
> average than sending the entire containing file. Returned source matched the
> pinned Git source in 360/360 checks.

It does **not** compare Symdex with ordinary range reads, or measure symbol
discovery, answer quality, a complete coding task, or whole-session token use.
Exact source fidelity proves that the requested code was preserved; it is not a
general accuracy score.

### Reproduce

From `symdex/`:

```powershell
uv venv --python 3.12.8
uv pip sync benchmarks/benchmark-requirements.txt --python .venv/Scripts/python.exe
.venv\Scripts\python.exe benchmarks\polyglot_symbol_benchmark.py --output benchmarks\results\polyglot_symbol_v2.json
```

The canonical result is tracked at `results/polyglot_symbol_v2.json`; timestamped
local runs remain ignored. The artifact records repository pins, index counts,
dependency versions, the benchmark-script, manifest and environment-snapshot
hashes, and a fingerprint of the exact Symdex source paths exercised by the
protocol. Rerun without network or indexing work after the first successful run:

```powershell
.venv\Scripts\python.exe benchmarks\polyglot_symbol_benchmark.py --skip-setup --skip-index
```

Canonical artifact SHA-256:
`E5BF30CCF48A4F250AB7648B9CEF8A7E8BEA0193C161E435D11D8EDED1EC1580`.

## Mixed-task Click benchmark

The older Click harness compares generated answers across 20 comprehension,
navigation, and modification questions. The final archived run used 775,275
Symdex context tokens and 1,010,973 raw-file context tokens: 23.31% fewer total
context tokens. Its judged-accuracy point estimate was 4.42 versus 4.45.

The three answer repetitions reused identical deterministic contexts, so this is
20 unique token comparisons rather than 60 independent token samples. The small
accuracy difference does not establish statistical equivalence or
non-inferiority. Modification questions used 25.30% more total context in that
run. Treat this harness as historical calibration, not the primary public proof.
