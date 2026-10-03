#!/usr/bin/env node
// CI's dependency gate for Studio: `npm audit`, minus an explicit, EXPIRING
// allowlist of advisories that cannot be fixed yet and do not apply.
//
// Why not plain `npm audit --audit-level=moderate`: on 2026-10-03 GHSA-ch52-4w7c-c8xp
// (http-cache-semantics <= 4.2.0, no patched release exists) failed every PR. Its only
// path is electron-builder -> @electron/get -> got -> cacheable-request, the packaging
// tool downloading Electron on a build machine; `npm audit fix --force` would DOWNGRADE
// electron-builder. An allowlist entry names one advisory, says why, and expires — after
// that date the gate fails again and somebody has to look. Anything not listed still fails.
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const RANK = { info: 0, low: 1, moderate: 2, high: 3, critical: 4 };
const GHSA = /GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}/i;

/** Pure: decide pass/fail from `npm audit --json` output, the allowlist and today's date. */
export function evaluate(audit, allowlist, today, minSeverity = 'moderate') {
  const allowed = new Map();
  const problems = [];
  for (const entry of allowlist) {
    if (!entry.id || !entry.reason || !entry.expires) {
      problems.push(`allowlist entry missing id/reason/expires: ${JSON.stringify(entry)}`);
      continue;
    }
    if (entry.expires < today) {
      problems.push(`allowlist entry ${entry.id} expired on ${entry.expires} — re-check it and renew or remove`);
      continue;
    }
    allowed.set(entry.id.toUpperCase(), entry);
  }
  const advisories = new Map();
  for (const [pkg, vuln] of Object.entries(audit.vulnerabilities ?? {})) {
    for (const via of vuln.via ?? []) {
      if (typeof via !== 'object') continue; // transitive: the advisory itself is reported on its own package
      if ((RANK[via.severity] ?? 0) < RANK[minSeverity]) continue;
      const id = ((via.url ?? '').match(GHSA)?.[0] ?? `${via.source ?? pkg}`).toUpperCase();
      advisories.set(id, { id, pkg: via.name ?? pkg, severity: via.severity, title: via.title ?? '' });
    }
  }
  const blocking = [...advisories.values()].filter((a) => !allowed.has(a.id));
  const waived = [...advisories.values()].filter((a) => allowed.has(a.id));
  return { ok: blocking.length === 0 && problems.length === 0, blocking, waived, problems };
}

function main() {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const studio = path.resolve(here, '..');
  const allowlist = JSON.parse(readFileSync(path.join(studio, 'audit-allowlist.json'), 'utf8'));
  let raw;
  try {
    raw = execFileSync('npm', ['audit', '--json'], { cwd: studio, encoding: 'utf8', shell: process.platform === 'win32', stdio: ['ignore', 'pipe', 'inherit'] });
  } catch (err) {
    raw = err.stdout; // npm audit exits non-zero when it finds anything; the JSON is still on stdout
  }
  const today = new Date().toISOString().slice(0, 10);
  const result = evaluate(JSON.parse(raw), allowlist, today);
  for (const a of result.waived) console.log(`waived  ${a.id} ${a.severity} ${a.pkg} — ${allowlist.find((e) => e.id.toUpperCase() === a.id).reason}`);
  for (const p of result.problems) console.error(`ERROR   ${p}`);
  for (const a of result.blocking) console.error(`BLOCK   ${a.id} ${a.severity} ${a.pkg} — ${a.title}`);
  console.log(result.ok ? 'audit gate: pass' : 'audit gate: FAIL');
  process.exit(result.ok ? 0 : 1);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
