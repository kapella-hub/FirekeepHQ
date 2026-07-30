# Key-ID Resolution Hardening

**Status:** design, approved 2026-07-30
**Sequencing:** standalone. Lands **before**
`2026-07-30-client-enrollment-join-codes-design.md`, and may land in parallel with
`2026-07-30-single-connection-config-design.md` — this change is confined to `auth/`,
that one is confined to `client/`.
**Severity:** unsound now, not an emergency. See §1.3.

## Problem

`auth/keys.py` resolves a short `key_id` back to a credential record by **glob prefix
match**, and acts on the first result without checking it is the right one.

```python
async def revoke_key(key_id: str) -> bool:
    async for full_key in _redis.scan_iter(f"{_KEY_PREFIX}{key_id}*", count=10):
        await _redis.delete(full_key)          # first match wins
        await _redis.zrem(_KEY_INDEX, key_id)
        return True                            # True regardless of which record it hit
    return False
```

`list_keys` (`keys.py:239-249`) does the same and `break`s on the first match.

Both assume a 16-hex prefix is unique. Nothing enforces that. `create_key` derives
`key_id = key_hash[:16]` (`keys.py:203`) and — this is the second half of the defect —
**stores that same prefix as the sorted-set member**:

```python
await _redis.zadd(_KEY_INDEX, {key_hash[:16]: now.timestamp()})   # keys.py:218
```

So the index cannot represent two credentials sharing a prefix at all. A second `zadd`
with an existing member updates its score rather than adding a row.

### 1.1 What a collision actually does

Given two credentials whose hashes share their first 16 hex characters:

| Path | Behaviour |
|---|---|
| `validate_key` | **Correct.** Looks up the full hash (`keys.py:288-290`); both credentials authenticate normally |
| `list_keys` | Reports **one** row for two credentials — the index holds one member, and the glob `break`s on the first match |
| `revoke_key` | Deletes whichever record SCAN returns first, `zrem`s the single index member, returns **`True`** |

The composite outcome is worse than any single row. After a collided revoke the survivor
has no index entry and no listing row, but its `auth:key:<full-hash>` record still exists
and still validates. **It is invisible to every enumeration path and unrevocable by
`key_id`, while remaining a working credential** — recoverable only by scanning
`auth:key:*` directly in Redis. And the operator was told the revocation succeeded.

### 1.2 Why it matters more after enrollment

Under server-minted keys the precondition is a 64-bit prefix collision between two
randomly generated hashes: ~2⁻⁶⁴ per pair, i.e. it does not happen.

`2026-07-30-client-enrollment-join-codes-design.md` has the **client** choose its
credential secret, and therefore the hash, and therefore the prefix. The precondition
stops being a collision and becomes a choice — with no privileged knowledge required,
because a teammate legitimately enrolling a second device (that design's §1.10 tells them
to run `invite` twice) can compute their own device 1 `key_id` and submit a device 2
hash sharing it. Revoking device 1 then reports success while device 1 keeps working.

### 1.3 Priority

Not an emergency and should not be described as one. Under today's server-minted keys
there is no remote exploit — the precondition is a random 64-bit collision. But the code
is **unsound as written**: it guesses when it should verify, and reports success when it
has not verified anything. It is fixed now, separately, so that the enrollment design
lands on a sound base rather than shipping the mitigation and the exposure together.

---

## 2. The immediate patch

Two independent fixes. Neither changes the storage format, so this patch is safe to ship
on its own and requires no migration.

**`revoke_key`** — collect **all** prefix matches rather than the first.

- Exactly one match whose stored `key_id` field equals the requested id → delete it,
  `zrem` the index member, return `True`.
- Zero matches → return `False`, unchanged.
- A match whose stored `key_id` differs from the requested id → not this credential;
  ignore it. (A prefix match is not an identity.)
- **Two or more matches → delete nothing, `zrem` nothing, and raise.** An ambiguous
  revocation must fail loudly. Deleting one of two credentials while telling the operator
  the revocation succeeded is the failure this patch exists to remove, and silently
  deleting both would be worse.

**`list_keys`** — resolve each index member to **all** matching records, verify each
record's stored `key_id`, and emit one row per verified record. Where a member resolves
to more than one record, emit every one of them and mark the set ambiguous. Under-
reporting inventory is how a credential goes unnoticed; a listing must never hide a live
credential behind a prefix.

Both paths log at `CRITICAL` on ambiguity, naming the colliding prefix and the full
hashes, because on a server-minted deployment it should be unreachable and its presence
means either a client-chosen hash arrived early or something is wrong with key
generation.

### 2.1 What this patch deliberately does not do

It does not change `key_id`'s derivation, the index member, or the storage layout.
Those are replaced by `auth:cred:<credential_id>` in the enrollment design (§1.8 there),
where `credential_id` is minted independently of the hash and the client cannot influence
it. This patch makes the **current** representation behave correctly; the enrollment
design removes the fragile representation. Doing the representation change here would
couple a security fix to a schema migration and delay both.

---

## 3. Testing

`auth/tests/test_key_id_resolution.py` — new:

- Two records deliberately sharing a 16-hex prefix (constructed directly in Redis, since
  `create_key` cannot produce them): `revoke_key` deletes **neither** and raises; both
  still validate afterwards.
- `list_keys` emits **both** records for a colliding prefix and marks the set ambiguous.
- A prefix match whose stored `key_id` differs from the requested id is not revoked and
  is not counted — a prefix match is not an identity.
- Single-match revoke, unknown-id revoke (`False`), and normal listing are unchanged —
  the regression surface is the whole point.
- After the patch, the invariant that fails without it: **no reachable sequence of
  `revoke_key` calls leaves a credential that `validate_key` accepts but `list_keys`
  does not show.**

---

## 4. Scope

`auth/keys.py` only: `list_keys` (`:230-250`), `revoke_key` (`:253-263`), and the
`CRITICAL` logging. `DELETE /auth/keys/{key_id}` (`auth/api.py:69-78`) gains the
ambiguity response — 409, not 500, since the request is well-formed and the server state
is the problem. `deploy/firekeep-admin keys revoke` (added by the enrollment design)
surfaces the same message.

No client changes. No storage migration. No behaviour change on any deployment that does
not already hold a prefix collision — which, on server-minted keys, is all of them.
