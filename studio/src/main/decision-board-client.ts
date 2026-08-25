import { z } from "zod";
import type { DecisionAnswers, DecisionBoardDocument, DecisionEmbedMeta } from "../shared/decision-board.js";
import { normalizeDecisionBoardUrl } from "../shared/decision-board.js";

const MAX_SPEC_BYTES = 2 * 1024 * 1024;
const MAX_EMBED_BYTES = 512_000;
const MAX_EMBEDS = 16;

const evidenceSchema = z.object({
  source: z.coerce.string().max(4_096).default(""),
  snippet: z.coerce.string().max(100_000).default(""),
  ref: z.string().max(8_192).optional(),
}).passthrough();

const questionSchema = z.object({
  id: z.coerce.string().min(1).max(128),
  text: z.coerce.string().max(100_000).default(""),
  knowledge_found: z.boolean().default(false),
  evidence: z.array(evidenceSchema).max(100).default([]),
  suggested_answers: z.array(z.coerce.string().max(20_000)).max(32).default([]),
  suggested_actions: z.array(z.coerce.string().max(20_000)).max(32).default([]),
}).passthrough();

const embedMetaSchema = z.object({
  i: z.number().int().min(0).max(10_000),
  title: z.coerce.string().max(1_000).default(""),
  height: z.number().finite().min(120).max(2_000).default(360),
}).passthrough();

const specSchema = z.object({
  board_id: z.coerce.string().min(1).max(128),
  context: z.coerce.string().max(200_000).optional(),
  questions: z.array(questionSchema).max(64).default([]),
  degraded: z.boolean().default(false),
  note: z.coerce.string().max(4_096).optional(),
  embeds: z.object({
    board: z.array(embedMetaSchema).max(MAX_EMBEDS).default([]),
    by_question: z.record(z.string().max(128), z.array(embedMetaSchema).max(MAX_EMBEDS)).default({}),
  }).optional(),
}).passthrough();

export interface DecisionBoardTransport {
  load(url: string): Promise<DecisionBoardDocument>;
  submit(url: string, answers: DecisionAnswers): Promise<void>;
}

export class LoopbackDecisionBoardClient implements DecisionBoardTransport {
  readonly #fetch: typeof globalThis.fetch;

  constructor(fetcher: typeof globalThis.fetch = globalThis.fetch) {
    this.#fetch = fetcher;
  }

  async load(value: string): Promise<DecisionBoardDocument> {
    const url = requireBoardUrl(value);
    const specResponse = await this.#fetch(endpoint(url, "spec"), {
      headers: { Accept: "application/json" },
      redirect: "error",
      signal: AbortSignal.timeout(5_000),
    });
    if (!specResponse.ok) throw new Error(`Decision Board is unavailable (HTTP ${specResponse.status})`);
    const specText = await boundedText(specResponse, MAX_SPEC_BYTES, "Decision Board spec");
    let raw: unknown;
    try { raw = JSON.parse(specText); }
    catch { throw new Error("Decision Board returned invalid JSON"); }
    const parsed = specSchema.parse(raw);
    const boardEmbeds = (parsed.embeds?.board ?? []).map(toEmbedMeta);
    const embedsByQuestion = Object.fromEntries(Object.entries(parsed.embeds?.by_question ?? {}).map(([id, items]) => [id, items.map(toEmbedMeta)]));
    const metadata = dedupeEmbeds([...boardEmbeds, ...Object.values(embedsByQuestion).flat()]).slice(0, MAX_EMBEDS);
    const embeds = await Promise.all(metadata.map(async (meta) => {
      try {
        const response = await this.#fetch(endpoint(url, `embed/${meta.index}`), {
          headers: { Accept: "text/html" },
          redirect: "error",
          signal: AbortSignal.timeout(5_000),
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return { ...meta, html: await boundedText(response, MAX_EMBED_BYTES, `Decision Board visual ${meta.index}`) };
      } catch {
        // A decorative visual must never make the questions unanswerable.
        return { ...meta, html: "<!doctype html><style>body{font:14px system-ui;padding:24px;color:#5d625e;background:#fff}</style><p>This visual is unavailable. You can still answer the board.</p>" };
      }
    }));
    return {
      url,
      spec: {
        boardId: parsed.board_id,
        ...(parsed.context ? { context: parsed.context } : {}),
        questions: parsed.questions.map((question) => ({
          id: question.id,
          text: question.text,
          knowledgeFound: question.knowledge_found,
          evidence: question.evidence.map((item) => ({ source: item.source, snippet: item.snippet, ...(item.ref ? { ref: item.ref } : {}) })),
          suggestedAnswers: question.suggested_answers,
          suggestedActions: question.suggested_actions,
        })),
        degraded: parsed.degraded,
        ...(parsed.note ? { note: parsed.note } : {}),
        boardEmbeds,
        embedsByQuestion,
      },
      embeds,
    };
  }

  async submit(value: string, answers: DecisionAnswers): Promise<void> {
    const url = requireBoardUrl(value);
    const response = await this.#fetch(endpoint(url, "answer"), {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ answers }),
      redirect: "error",
      signal: AbortSignal.timeout(5_000),
    });
    if (!response.ok) throw new Error(`Decision Board submission failed (HTTP ${response.status})`);
  }
}

function requireBoardUrl(value: string): string {
  const url = normalizeDecisionBoardUrl(value);
  if (!url) throw new Error("Decision Board URL must be an exact 127.0.0.1 loopback board URL");
  return url;
}

function endpoint(boardUrl: string, suffix: string): string { return `${boardUrl}/${suffix}`; }
function toEmbedMeta(value: z.infer<typeof embedMetaSchema>): DecisionEmbedMeta { return { index: value.i, title: value.title, height: value.height }; }
function dedupeEmbeds(values: readonly DecisionEmbedMeta[]): DecisionEmbedMeta[] {
  return [...new Map(values.map((value) => [value.index, value])).values()];
}

async function boundedText(response: Response, limit: number, label: string): Promise<string> {
  const declared = Number(response.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > limit) throw new Error(`${label} exceeds ${limit} bytes`);
  const text = await response.text();
  if (Buffer.byteLength(text, "utf8") > limit) throw new Error(`${label} exceeds ${limit} bytes`);
  return text;
}
