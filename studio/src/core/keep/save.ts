/**
 * What a human may save to the Keep from Studio: a memory (what was done and what came of
 * it) or a skill draft (a procedure a teammate reviews before the team relies on it).
 *
 * Pure validation only. The Keep is team-visible and is not a secret store, so anything
 * that looks like a credential is refused here, before it leaves the machine.
 */
export interface MemoryDraft {
  readonly action: string;
  readonly outcome: string;
  readonly resolution?: string;
  readonly tags: readonly string[];
}

export interface SkillDraft {
  readonly trigger: string;
  readonly symptoms: string;
  readonly steps: string;
  readonly gotchas?: string;
  readonly domain?: string;
}

export const KEEP_TEXT_LIMIT = 4_000;
export const KEEP_TAG_LIMIT = 8;

const SECRET_PATTERNS: readonly { readonly name: string; readonly pattern: RegExp }[] = [
  { name: "a private key", pattern: /-----BEGIN [A-Z ]*PRIVATE KEY-----/ },
  { name: "an API key", pattern: /\b(?:sk|pk|rk)-(?:[A-Za-z]+-)?[A-Za-z0-9_-]{20,}\b/ },
  { name: "a GitHub token", pattern: /\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b/ },
  { name: "an AWS access key", pattern: /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/ },
  { name: "a Slack token", pattern: /\bxox[abprs]-[A-Za-z0-9-]{10,}\b/ },
  { name: "a signed token", pattern: /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/ },
  { name: "a connection string with a password", pattern: /\b[a-z][a-z0-9+.-]*:\/\/[^\s:@/]+:[^\s@/]{3,}@/i },
  // The value must contain a digit, so prose such as "password: changed" is not refused.
  { name: "a password or token assignment", pattern: /\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*["']?(?=[^\s"']*\d)[^\s"']{6,}/i },
];

/** Names the kind of secret a text appears to contain, or null. */
export function findSecret(text: string): string | null {
  for (const { name, pattern } of SECRET_PATTERNS) if (pattern.test(text)) return name;
  return null;
}

/** Splits a comma- or space-separated tag field into clean, unique, lower-case tags. */
export function parseTags(input: string): string[] {
  const tags: string[] = [];
  for (const raw of input.split(/[,\s]+/)) {
    const tag = raw.trim().toLowerCase().replace(/^#/, "").replace(/[^a-z0-9._-]/g, "").slice(0, 40);
    if (tag && !tags.includes(tag)) tags.push(tag);
    if (tags.length === KEEP_TAG_LIMIT) break;
  }
  return tags;
}

function check(fields: readonly (readonly [label: string, value: string | undefined, required: boolean])[]): string | null {
  for (const [label, value, required] of fields) {
    const text = value?.trim() ?? "";
    if (required && !text) return `${label} is required`;
    if (text.length > KEEP_TEXT_LIMIT) return `${label} is longer than ${KEEP_TEXT_LIMIT} characters`;
    const secret = findSecret(text);
    if (secret) return `${label} looks like it contains ${secret}. The Keep is shared with your team; store secrets in the vault instead.`;
  }
  return null;
}

/** The reason a memory cannot be saved, or null when it can. */
export function memoryDraftProblem(draft: MemoryDraft): string | null {
  return check([["What was done", draft.action, true], ["What came of it", draft.outcome, true], ["How it was resolved", draft.resolution, false]]);
}

/** The reason a skill draft cannot be saved, or null when it can. */
export function skillDraftProblem(draft: SkillDraft): string | null {
  return check([["When it applies", draft.trigger, true], ["Symptoms", draft.symptoms, true], ["Steps", draft.steps, true], ["Gotchas", draft.gotchas, false], ["Domain", draft.domain, false]]);
}
