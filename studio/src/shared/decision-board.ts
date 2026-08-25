export interface DecisionEvidence {
  readonly source: string;
  readonly snippet: string;
  readonly ref?: string;
}

export interface DecisionQuestion {
  readonly id: string;
  readonly text: string;
  readonly knowledgeFound: boolean;
  readonly evidence: readonly DecisionEvidence[];
  readonly suggestedAnswers: readonly string[];
  readonly suggestedActions: readonly string[];
}

export interface DecisionEmbedMeta {
  readonly index: number;
  readonly title: string;
  readonly height: number;
}

export interface DecisionBoardSpec {
  readonly boardId: string;
  readonly context?: string;
  readonly questions: readonly DecisionQuestion[];
  readonly degraded: boolean;
  readonly note?: string;
  readonly boardEmbeds: readonly DecisionEmbedMeta[];
  readonly embedsByQuestion: Readonly<Record<string, readonly DecisionEmbedMeta[]>>;
}

export interface DecisionEmbed extends DecisionEmbedMeta {
  readonly html: string;
}

export interface DecisionBoardDocument {
  readonly url: string;
  readonly spec: DecisionBoardSpec;
  readonly embeds: readonly DecisionEmbed[];
}

export interface DecisionAnswer {
  readonly answer: string;
  readonly actions_confirmed: readonly string[];
  readonly skipped: boolean;
}

export type DecisionAnswers = Readonly<Record<string, DecisionAnswer>>;

const BOARD_URL = /http:\/\/127\.0\.0\.1:\d{1,5}\/board\/[A-Za-z0-9_-]{1,128}\/?/i;

export function normalizeDecisionBoardUrl(value: string): string | null {
  try {
    const url = new URL(value);
    if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port) return null;
    if (url.username || url.password || url.search || url.hash) return null;
    const port = Number(url.port);
    if (!Number.isInteger(port) || port < 1 || port > 65_535) return null;
    if (!/^\/board\/[A-Za-z0-9_-]{1,128}\/?$/.test(url.pathname)) return null;
    url.pathname = url.pathname.replace(/\/$/, "");
    return url.toString().replace(/\/$/, "");
  } catch {
    return null;
  }
}

export function findDecisionBoardUrl(value: unknown): string | null {
  const pending: Array<{ readonly value: unknown; readonly depth: number }> = [{ value, depth: 0 }];
  const visited = new Set<object>();
  let inspected = 0;
  while (pending.length && inspected < 2_000) {
    const next = pending.shift();
    if (!next) break;
    inspected += 1;
    if (typeof next.value === "string") {
      const direct = normalizeDecisionBoardUrl(next.value);
      if (direct) return direct;
      const match = BOARD_URL.exec(next.value);
      if (match) return normalizeDecisionBoardUrl(match[0]);
      continue;
    }
    if (!next.value || typeof next.value !== "object" || next.depth >= 8 || visited.has(next.value)) continue;
    visited.add(next.value);
    for (const child of Object.values(next.value as Record<string, unknown>)) {
      pending.push({ value: child, depth: next.depth + 1 });
    }
  }
  return null;
}
