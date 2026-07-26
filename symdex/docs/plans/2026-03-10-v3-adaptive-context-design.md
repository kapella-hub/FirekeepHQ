# V3 Adaptive Multi-Strategy Context Assembly

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Date**: 2026-03-10
**Status**: Approved

## Goal

Build a context builder that is strictly better than raw file reading — same completeness, plus structural intelligence from FirekeepSymdex. Auto-selects strategy based on question analysis and file sizes.

## Architecture

Three strategies auto-selected per question:

### Strategy 1: PRECISE (raw < 8K tokens)
- Include full raw files
- Prefix each file with structured outline header
- Append dependency annotations
- Add reference bridges between files

### Strategy 2: ENRICHED RAW (raw 8K-20K tokens)
- Include full raw files
- Add inline annotations: `# ← called by X`, `# ← implements Y`
- Outline header + dependency summary
- Budget-aware: may trim least-relevant files

### Strategy 3: SURGICAL FOCUS (raw > 20K tokens)
- File outlines for all relevant files
- Targeted symbol extraction (search hits + deps)
- Progressive budget filling
- Reference bridges between files

## Question Classification

Analyze question text to detect intent:
- Location words ("where", "find", "located", "defined") → prioritize outlines + exact locations
- Mechanism words ("how does", "works", "process", "handle") → prioritize symbol source + call chains
- Change words ("modify", "add", "change", "would you") → prioritize structure + deps + patterns

## Reference Bridging

For multi-file questions, include explicit cross-file references:
```
## Cross-File References
- types.py:ParamType.convert() ← called by core.py:Parameter.type_cast_value()
- exceptions.py:BadParameter ← raised by types.py:ParamType.fail()
```

## Context Quality Score

After assembly, check what % of answer_key terms appear in context. If < 50%, broaden search.

## Success Criteria

- Beat raw files on accuracy (currently 4.20 vs 4.45 → target: 4.50+)
- Maintain 30%+ average token savings
- Win comprehension, tie navigation, close gap on modification
- Zero negative-savings questions
