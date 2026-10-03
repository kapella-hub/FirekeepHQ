import { describe, expect, it } from 'vitest';
// @ts-expect-error -- plain .mjs CI script, no type declarations
import { evaluate } from '../scripts/audit-gate.mjs';

const advisory = (pkg: string, id: string, severity = 'high') => ({
  vulnerabilities: {
    [pkg]: { severity, via: [{ name: pkg, severity, url: `https://github.com/advisories/${id}`, title: 't' }] },
    'depends-on-it': { severity, via: [pkg] },
  },
});
const entry = { id: 'GHSA-ch52-4w7c-c8xp', reason: 'build-only path', expires: '2026-11-03' };

describe('studio audit gate', () => {
  it('passes when every advisory at or above moderate is allowlisted and unexpired', () => {
    const r = evaluate(advisory('http-cache-semantics', 'GHSA-ch52-4w7c-c8xp'), [entry], '2026-10-03');
    expect(r.ok).toBe(true);
    expect(r.waived.map((a: { id: string }) => a.id)).toEqual(['GHSA-CH52-4W7C-C8XP']);
  });

  it('fails on an advisory that is not allowlisted', () => {
    const r = evaluate(advisory('left-pad', 'GHSA-aaaa-bbbb-cccc'), [entry], '2026-10-03');
    expect(r.ok).toBe(false);
    expect(r.blocking[0].id).toBe('GHSA-AAAA-BBBB-CCCC');
  });

  it('fails once an allowlist entry has expired, so the waiver gets re-checked', () => {
    const r = evaluate(advisory('http-cache-semantics', 'GHSA-ch52-4w7c-c8xp'), [entry], '2026-11-04');
    expect(r.ok).toBe(false);
    expect(r.problems[0]).toMatch(/expired/);
  });

  it('refuses an allowlist entry without a reason or expiry', () => {
    const r = evaluate({ vulnerabilities: {} }, [{ id: 'GHSA-x' }], '2026-10-03');
    expect(r.ok).toBe(false);
  });

  it('ignores advisories below moderate, like npm audit --audit-level=moderate', () => {
    const r = evaluate(advisory('minor', 'GHSA-dddd-eeee-ffff', 'low'), [], '2026-10-03');
    expect(r.ok).toBe(true);
  });
});
