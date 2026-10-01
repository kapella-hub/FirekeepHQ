# Release Signing — key custody runbook

**Status: keys minted and armed (2026-08-12).** Key ID `7D6D83D1240D4A61`; the
private half lives in the `FIREKEEP_SIGNING_KEY` Actions secret and offline at
the operator's key directory (keep a password-manager copy too), the public half
is pinned in `client/firekeep_client/signing.py`. Releases from client 0.1.42 on
publish a `SHA256SUMS.minisig` that the workflow byte-verifies against the served
copy. **Enforcement is on by default since 2026-10-01** (step 5 below): with
`[dist] require_signed` defaulting to `true`, a client refuses any release whose
signature it cannot verify. The flip waited for production evidence — at the
flip, every version served by the release host (1.5.0 through 1.6.1) verified
against the pinned key. The cost it accepts: a release published **unsigned**
(the CI secret missing) is now refused by every client and stalls the fleet's
updates until it is re-published signed — see step 2. Since 2026-10-01 the release workflow passes `make_release.py --require-signing`,
so a missing secret fails the release job instead of publishing that unsigned release;
local and smoke builds omit the flag and stay unsigned on purpose.

## What is signed, and what that protects

Every release publishes `<version>/SHA256SUMS`, which checksums the `uv`
binaries, every shipped wheel, **and both bootstrap scripts**. Signing that one file with
an Ed25519 key (a detached, minisign-format `SHA256SUMS.minisig`) transitively
covers everything the update path executes:

```
key pinned in the INSTALLED client (previous release)
  └─ verifies <version>/SHA256SUMS.minisig            (firekeep update, client-side)
       └─ SHA256SUMS is then trusted
            ├─ anchors install.sh / install.ps1 hash   (cross-checked against the
            │    └─ the bootstrap firekeep update executes    unsigned latest.json)
            └─ anchors uv-* and *.whl hashes
                 └─ what the bootstrap fetches and installs
```

On the `firekeep update` path the verified bytes are **threaded through, not
re-fetched**: the client writes the signature-verified `SHA256SUMS` to a private
(0600) file and hands its path to the bootstrap as `FIREKEEP_SUMS_FILE`; the
bootstrap then makes **no** `SHA256SUMS`/`.minisig` network fetch at all and
verifies `uv` + the wheels against exactly the bytes the client verified. (The
two-fetch split this closes: the client's fetch and the bootstrap's used to be
separate requests, trivially distinguishable by user agent, so a malicious host
could serve honest bytes to the verifier and attacker bytes to the installer.)
The hand-off is honoured only together with `FIREKEEP_VERSION` — the shape only
the client's re-exec produces — so a manual `curl | sh` fetches exactly as
before. The client also exports its own pinned key as `FIREKEEP_SIGNING_PUB`, so
on any path where the bootstrap does verify, it verifies against the INSTALLED
client's anchor rather than only the host-baked one. `firekeep update --to X.Y.Z`
verifies the **target** version's sums (what actually gets installed) and, when
the target is not the latest, additionally the latest version's sums (which
anchor the `latest/` bootstrap script being executed); under `require_signed`
(the default) an unsigned target release fails the update, and the error gives
the exact opt-out line.

Threat displaced: a compromised **release host** can serve only bytes the signing
key signed. Threats NOT displaced (stated, not papered over):

- **First install (`curl | sh`) is trust-on-first-use.** The bootstrap comes from
  the host itself; no key it delivers can authenticate it. A cautious first
  installer can pin out of band: `FIREKEEP_SIGNING_PUB=<pubkey> curl ... | sh`
  (with `minisign` installed). `latest/signing.pub` is published for
  transparency/out-of-band comparison — it is **not** a trust anchor.
- **Absence is refused by default — unless an operator opts out.** Since
  2026-10-01 `[dist] require_signed` defaults to `true`: an unsigned (or
  unverifiable) release fails the update with an error that names the override.
  An operator who sets `require_signed = false` under `[dist]` in
  `~/.firekeep/config` re-opens the old migration-window hole on that machine:
  an attacker with host write access need not forge a signature, they can
  simply publish *unsigned*, and the opt-out tolerates it with a one-line
  warning. There is deliberately no environment-variable override — the config
  file is `0600` and owned by the user; a process environment is not. Neither
  outcome is invisible when the update ran detached (the background auto-update,
  stderr on DEVNULL): the client persists a one-shot marker — "installed without
  a verified signature" under the opt-out, "update REFUSED: …" under the default
  — and the **next session-start briefing prints it** once.
- **Downgrade/freeze.** `latest.json` is unsigned; a compromised host can replay
  an older signed release. It cannot introduce new code.
- **Signing-key or CI compromise.** Signing moves trust from the host to the key.
  Guard the key accordingly (below).
- **The shell bootstraps verify best-effort only**: with a `minisign` binary
  present and a baked/provided key they verify (and an invalid signature is
  fatal); a bare machine's protection is the checksum chain + TLS. On the
  `firekeep update` re-exec path the in-script check does not even run — the client
  verified the sums itself and hands the verified bytes through
  `FIREKEEP_SUMS_FILE` (above), which is stronger than re-checking a re-fetch.

Format choice: minisign, not a bespoke signature file — `minisign -Vm SHA256SUMS
-P <pubkey>` verifies our releases and `minisign -G -W` keys sign them. The
client verifies with `client/firekeep_client/signing.py` (pure stdlib — the kit's
import boundary forbids third-party crypto libs; RFC 8032 vectors pin it).

## Enabling signing (one-time)

1. **Mint the keypair OFFLINE** (a machine you trust, ideally not the CI host):

   ```bash
   python client/scripts/generate_signing_key.py /path/to/offline/dir
   # or, equivalently, with standard tooling:
   #   minisign -G -W -p firekeep-signing.pub -s firekeep-signing.key
   ```

   The secret is written UNENCRYPTED (`-W`): the CI secret store is the
   encryption layer, and CI cannot answer an scrypt password prompt. Keep the
   originals somewhere durable and offline (password manager entry or offline
   media). A password-protected key is refused by the tooling with instructions.

2. **Add the private key as a CI secret** named `FIREKEEP_SIGNING_KEY`, value =
   the full content of `firekeep-signing.key` (both lines):
   - GitHub: repo → Settings → Secrets and variables → Actions →
     `FIREKEEP_SIGNING_KEY`. `.github/workflows/release.yml` already passes it to
     the assemble step.
   - GitLab (office pipeline, when that path is active): Settings → CI/CD →
     Variables → `FIREKEEP_SIGNING_KEY`, masked, protected; export it into the
     environment of the job that runs `make_release.py`.

   CI signing **skips gracefully** while the secret is absent: the release builds
   unsigned and the log says `UNSIGNED (FIREKEEP_SIGNING_KEY is not set)`. A
   secret that is set but unusable **fails the release** — misconfiguration must
   never silently ship unsigned. **Since enforcement became the default
   (step 5), "skips gracefully" is no longer graceful downstream:** every client
   refuses an unsigned release, so publishing one stalls the fleet's updates
   (each client's briefing shows the refusal) until the version is re-published
   signed. Treat a missing secret as a release blocker.

3. **Pin the public key in the client**: paste the base64 line of
   `firekeep-signing.pub` into `PINNED_PUBLIC_KEY` in
   `client/firekeep_client/signing.py` and commit. This constant is the trust
   anchor `firekeep update` verifies against; it also gets baked into the published
   bootstraps (make_release derives it from the signing key) and published as
   `latest/signing.pub`.

4. **Cut a release.** From this release on: `SHA256SUMS.minisig` is published in
   the version directory, and every client that installs this (or any later)
   version verifies all subsequent updates.

5. **Flip enforcement — DONE 2026-10-01.** `require_signed` is now the shipped
   default (`updater.require_signed` returns `true` when `[dist]` omits it or
   leaves it blank). Evidence at the flip: every version on the release host
   (1.5.0, 1.5.1, 1.5.2, 1.5.4–1.5.8, 1.6.0, 1.6.1) verified against the pinned
   key through `updater.fetch_signed_sums(..., require_signed=True)`; nothing
   older is served. Enforcement lives in the INSTALLED client, so it protects
   updates from the first release that carries the flip. To opt a machine out,
   add `require_signed = false` under `[dist]` in `~/.firekeep/config`. An
   INVALID signature is always fatal regardless of the flag.

## Verifying by hand (anyone, any release)

```bash
curl -O <base>/<version>/SHA256SUMS -O <base>/<version>/SHA256SUMS.minisig
minisign -Vm SHA256SUMS -P "$(curl -fsSL <base>/latest/signing.pub | tail -1)"
# or against the source-of-truth pin:
#   python -c "from firekeep_client import signing; print(signing.PINNED_PUBLIC_KEY)"
```

## Rotation (planned, key NOT compromised)

The invariant: **each release must be signed by the key pinned in the release
clients update FROM.** Rotation is therefore two releases:

1. Mint the new keypair (as above). Do not touch CI yet.
2. **Release N**: `PINNED_PUBLIC_KEY` = NEW public key; CI still signs with the
   OLD key. Clients on N-1 (pinning OLD) verify N, install it, and now pin NEW.
   (make_release prints a NOTICE when the signing key and the repo pin differ —
   expected during exactly this release.)
3. Swap the CI secret to the NEW private key.
4. **Release N+1** onward: signed with NEW, pinned NEW. Retire the old secret.

Skipping releases across a rotation (e.g. N-3 → N+1) fails verification; those
clients re-run the bootstrap (`firekeep update --to N` first, or reinstall).

## Suspected key compromise

An attacker with the private key AND release-host control can sign malicious
releases that verify. Move fast, in this order:

1. **Remove `FIREKEEP_SIGNING_KEY` from CI** and revoke CI access as warranted —
   stops further signatures under the stolen key.
2. **Take the release host's write path offline** (revoke the deploy key for the
   dist repo / registry token). The signature only matters if the host can serve
   attacker bytes.
3. **Audit what was served**: compare every published `<version>/` artifact
   against CI-built artifacts (hashes in the CI logs) for the exposure window.
4. **Mint a new keypair** and ship an emergency release per the rotation dance —
   except the compromised key must NOT sign it. That breaks the automated trust
   chain for one release, unavoidably: announce out of band, publish the new
   public key through a second channel (not just the release host), and tell
   users to reinstall via the bootstrap after verifying `signing.pub` against the
   announcement. `firekeep update` on machines that never fetched a malicious
   version keeps failing safe (invalid/mismatched signatures are fatal), which is
   the point.
5. Rotate the enrollment/API credentials of any machine that installed a
   release you cannot account for — treat those machines as compromised
   (the wheel becomes the PreToolUse hook that runs before every edit).

## Where things live

| What | Where |
|---|---|
| Trust anchor (pinned public key) | `client/firekeep_client/signing.py` → `PINNED_PUBLIC_KEY` |
| Verifier (client) | `client/firekeep_client/signing.py` + `updater.fetch_signed_sums` / `bootstrap_sha256` |
| Signer (CI) | `client/scripts/make_release.py` (env `FIREKEEP_SIGNING_KEY`) |
| Keygen | `client/scripts/generate_signing_key.py` (or `minisign -G -W`) |
| Bootstrap best-effort check | `client/bootstrap/install.sh` step 3b / `install.ps1` step 2b (baked key placeholder `__FIREKEEP_SIGNING_PUB_DEFAULT__`, override `FIREKEEP_SIGNING_PUB` — exported by `firekeep update` from the pinned key) |
| Verified-sums hand-off | `FIREKEEP_SUMS_FILE` (written 0600 by `cli._write_verified_sums`; honoured by the bootstraps only alongside `FIREKEEP_VERSION`; no network sums fetch under it) |
| Unsigned-update / refusal notice | `state.note_unsigned_update` → printed once by the next session-start briefing (covers the detached auto-update, whose stderr is DEVNULL); written for an unsigned install under the opt-out, and for an `UnverifiedReleaseError` refusal under the default |
| Published signature | `<base>/<version>/SHA256SUMS.minisig` (CI's verify step polls it and byte-compares against the built signature) |
| Transparency copy of the public key | `<base>/latest/signing.pub` (not a trust anchor) |
| Enforcement flag | `~/.firekeep/config` → `[dist] require_signed` (default `true` since 2026-10-01; `false` opts out; no env-var override) |
| Threat-model entry | `docs/THREAT-MODEL.md` §5.6, ranked item #2 |
