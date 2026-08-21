# Security Policy

Firekeep is self-hosted: you run it on your own infrastructure, and there is no
Firekeep-operated service holding your data. That shapes everything below — we
cannot patch your deployment for you, so what we owe you is a fast, honest
answer and a release you can apply.

## Reporting a vulnerability

Email **`security@firekeep.ai`** with:

- what you found and where (file, endpoint, or configuration),
- how to reproduce it,
- what an attacker gets, and
- the version you tested (`GET /version` on any service reports it).

Please **do not** open a public issue for a security report.

If you want to encrypt, ask for a key in a first plaintext mail that contains no
details.

### What we commit to

| Stage | Target |
|---|---|
| Acknowledgement that a human has read it | 3 business days |
| Initial assessment — severity, whether we reproduce it | 10 business days |
| Fix or documented mitigation, Critical / High | 30 days from assessment |
| Fix or documented mitigation, Medium / Low | next scheduled release |
| Public advisory | with the fix, or at 90 days, whichever is first |

These are targets for a single-maintainer project, not a contractual SLA. If a
deadline is going to slip you will be told before it slips, not after.

We will credit you in the advisory unless you ask us not to. We do not run a bug
bounty and cannot pay for reports.

### Scope

**In scope** — anything that lets someone read, alter or destroy data they should
not, on a deployment configured as the documentation describes:

- authentication and scope enforcement (`auth/`), on both the REST and MCP surfaces
- the secrets vault (`vault/`) and anything that discloses decrypted values
- the knowledge crawler's SSRF guard (`cortex/app/knowledge/crawler.py`)
- the agent gateway and policy engine, where a `block` decision can be bypassed
- the client kit's update path (signature/checksum verification, `firekeep update`)
- privilege escalation between scopes, or between agent identities
- anything in a default install that is reachable without a key

**Out of scope** — real, but already documented as the operator's decision:

- `AUTH_ENABLED=false`. It is not the default, the installer refuses to set it
  without `--insecure-no-auth`, and it warns loudly. With auth off, everything
  below `admin` is open by design; that is the documented meaning of the setting.
- `BIND_ADDR=0.0.0.0` without a firewall or reverse proxy. Also opt-in, also
  warned about — including the fact that **ufw will not contain a published
  Docker port** (see `docs/DEPLOYMENT.md`).
- Anything requiring a valid `admin` key. Admin is total by design.
- Vulnerabilities in Neo4j, Qdrant, Redis or Ollama themselves — report those
  upstream. Do tell us if *our* configuration of them is the problem.
- Denial of service by resource exhaustion on a single-tenant deployment you
  control.
- Missing hardening headers on the dashboard, which is not intended to face the
  internet.

## Supported versions

| Version | Supported |
|---|---|
| `main` | Yes — security fixes land here first |
| Latest tagged release | Yes |
| Older tagged releases | No |

There are no server `vX.Y.Z` tags yet, so today "supported" means current `main`.
This table becomes meaningful with the first tagged release and must be updated
then. Client-kit releases (`client-vX.Y.Z`) follow the same rule: current only.

## What we do on our side

- **Dependency CVEs** — `pip-audit` runs in CI over each shipped dependency set
  in its own clean virtualenv (`.github/workflows/ci.yml`, job `security`).
- **Licence gate** — a denied-licence list is enforced per shipped wheel, same
  isolation. See `docs/LICENSING.md`.
- **Secret scanning** — CI fails on credential-shaped strings and on a list of
  tokens that must never appear (`scripts/check_forbidden_tokens.py`).
- **SBOM** — a CycloneDX bill of materials is generated per release artifact.
- **Threat model** — `docs/THREAT-MODEL.md`, covering all four services, the
  client kit and the crawler.

None of that finds design flaws. That is what this policy is for.

## Before first sale — outstanding

Tracked here rather than in a private note, because a security policy that
overstates its own maturity is itself a security problem:

1. ~~Confirm `security@firekeep.ai` is actually monitored.~~ **Resolved
   2026-08-09**: the mailbox exists on the domain's mail service and is read by
   the maintainer, alongside `sales@firekeep.ai`.
2. ~~Confirm the SLA targets are ones a solo maintainer can actually hit.~~
   **Reviewed 2026-08-20, kept as written.** Three business days to acknowledge
   an email, ten to assess, thirty to fix a Critical or High from assessment —
   these are deliberately unambitious, and the paragraph under the table already
   says they are targets rather than a contract and that a slip is announced
   before it happens, not after. Lowering them further would buy nothing except
   looking unserious. Revisit if a real report ever misses one.
3. ~~Decide the advisory channel.~~ **Resolved 2026-08-20 by the repository
   going public.** The premise of this item was that GitHub Security Advisories
   on a private repo reach nobody. `kapella-hub/FirekeepHQ` is public, so
   advisories are now a real channel with CVE issuance and automatic Dependabot
   notification to anyone consuming the packages. Advisories are published
   there, and the release notes for the fixing version link to them. Direct
   email to known deployments remains the belt-and-braces path for anything
   Critical, because a self-hosted operator who does not watch the repo will
   never see an advisory.
4. ~~Re-date and re-scope `cortex/docs/SECURITY_REVIEW.md`.~~ **Resolved
   2026-08-20**: the file now opens with a superseded banner naming its scope
   (Cortex only), its date (2026-03-02), and everything shipped since that it
   predates. It is kept as a record of what was reviewed then;
   `docs/THREAT-MODEL.md` is current state.
