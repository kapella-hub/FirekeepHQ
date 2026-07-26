---
name: self-review
description: Review your recent code changes against the indexed codebase. Use after completing edits to verify callers are updated, conventions are followed, and no symbols are broken.
---

Run a self-review of recent changes using FirekeepSymdex:

1. Identify all files you modified in this session (check git status or recall from conversation)
2. Call get_review_context(repo, changed_files=[<list of modified files>]) to find:
   - Changed symbols and their callers (are callers updated?)
   - Dependencies of changed symbols (are they still valid?)
   - Related test files (do tests exist for your changes?)
3. Call extract_conventions(repo) and verify your changes follow the codebase's naming and structural patterns
4. Report findings: what looks good, what might need attention, and any callers that may need updating
