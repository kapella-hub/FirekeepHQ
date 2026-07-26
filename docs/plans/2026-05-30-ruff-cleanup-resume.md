# Resume: ruff cleanup + gate (chore/ruff-cleanup-and-gate)

**Date:** 2026-05-30
**Branch:** `chore/ruff-cleanup-and-gate` (off main `fa7c693`). NOT committed/pushed yet.

## Context
Foundation bundle (CI + provenance) already merged to main `fa7c693`, deployed, validated.
CI first run: all 6 test jobs GREEN. Only `lint (ruff)` red — intentionally non-blocking.
User chose: **full cleanup + flip lint to blocking** AND **bump GitHub Actions versions**.

## DONE on this branch (uncommitted, in working tree)
- `ruff check . --fix`: **158 auto-fixed** (mostly F401 unused imports) across ~90 files.
- Verified SAFE: full cortex suite **780 passed, 29 skipped, 0 failed** (after
  `pip install scikit-learn joblib` locally — host lacked them; only reason tests looked red).

## REMAINING: 57 findings, manual
- **F841 unused-variable (30):** ~21 in test files (`result = await ...` no assert → drop assign,
  keep call). ~9 in APP code — inspect each for real bugs before removing:
  bridge/app/session.py:163,439 `prev_active`; cortex/app/db/vector.py:275 `filter_tags`;
  cortex/app/main.py:1194 `ranker` (RecallRanker() — instantiation side effect? check);
  cortex/app/patterns/store.py:331 `feature_map`; cortex/app/skills/synthesizer.py:100 `s`;
  cortex/app/transfer.py:186 `count`; symdex get_evolution_timeline.py:121 `line_end`,
  suggest_symbols.py:92 `keyword_freq`, :113 `sym_sig`.
- **E402 module-import-not-at-top (20):** many legit lazy/conditional imports → prefer
  `[tool.ruff.lint.per-file-ignores]` in a repo-root `ruff.toml`, not code moves.
- **E741 ambiguous-name (5):** rename l/I/O vars.
- **F821 (2): VERIFIED FALSE POSITIVES** (forward-ref string annotations, runtime imports):
  cortex/app/agent_gateway/service.py:209 `-> "ActionAfterResponse"`;
  symdex/.../tools/get_file_outline.py:94 `-> "Symbol"`. FIX: `# noqa: F821` on those lines.

## Next steps
1. Inspect & fix 9 app-code F841 (watch for real bugs / side effects).
2. Drop 21 test F841 assignments (keep the calls).
3. E741 renames.
4. F821 noqas.
5. Repo-root `ruff.toml`: pin config + per-file-ignores for legit E402.
6. Full suites green: cortex (needs scikit-learn+joblib), bridge, sentinel, relay, symdex, shared.
7. ci.yml: remove `continue-on-error: true` from lint; bump actions/checkout@v4→v5, setup-python@v5→v6 (verify latest).
8. Commit, merge --no-ff to main, push, watch `gh run list --branch main`.

## Gotchas (heed — cost time this session)
- Bash cwd does NOT persist — always `cd /opt/Firekeep && ...`.
- Host lacks scikit-learn/joblib → test_ranker fails MISLEADINGLY. Install both first.
- Don't batch dependent git ops with verification reads — one failure cancels the batch.
- Pre-existing stashes stash@{0},{1} are NOT mine — leave them.
- After Edits, grep to confirm they applied before claiming success.
