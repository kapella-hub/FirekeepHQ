import { describe, expect, it, vi } from "vitest";
import { LoopbackDecisionBoardClient } from "../src/main/decision-board-client.js";
import { findDecisionBoardUrl, normalizeDecisionBoardUrl } from "../src/shared/decision-board.js";

const boardUrl = "http://127.0.0.1:43123/board/abc_DEF-123";

describe("Decision Board bridge", () => {
  it("finds nested provider result shapes but accepts only exact loopback board URLs", () => {
    expect(findDecisionBoardUrl({ content: [{ type: "text", text: `pending: {\"board_url\":\"${boardUrl}\"}` }] })).toBe(boardUrl);
    expect(normalizeDecisionBoardUrl("http://localhost:43123/board/abc")).toBeNull();
    expect(normalizeDecisionBoardUrl("http://127.0.0.1:43123/admin")).toBeNull();
    expect(normalizeDecisionBoardUrl(`${boardUrl}?next=http://example.com`)).toBeNull();
  });

  it("loads and normalizes a board plus its sandboxed visual metadata", async () => {
    const fetcher = vi.fn(async (url: string | URL) => {
      const value = String(url);
      if (value.endsWith("/spec")) return new Response(JSON.stringify({
        board_id: "abc_DEF-123",
        context: "Choose a release shape",
        questions: [{ id: "q0", text: "Ship now?", knowledge_found: true, evidence: [{ source: "memory", snippet: "Prior release was stable" }], suggested_answers: ["Yes"], suggested_actions: ["Run smoke"] }],
        degraded: false,
        embeds: { board: [{ i: 0, title: "Options", height: 240 }], by_question: {} },
      }), { status: 200, headers: { "Content-Type": "application/json" } });
      if (value.endsWith("/embed/0")) return new Response("<!doctype html><svg></svg>", { status: 200 });
      throw new Error(`unexpected URL ${value}`);
    });
    const client = new LoopbackDecisionBoardClient(fetcher as typeof globalThis.fetch);

    const board = await client.load(boardUrl);

    expect(board).toMatchObject({
      url: boardUrl,
      spec: { boardId: "abc_DEF-123", context: "Choose a release shape", questions: [{ id: "q0", knowledgeFound: true, suggestedAnswers: ["Yes"] }] },
      embeds: [{ index: 0, title: "Options", height: 240, html: "<!doctype html><svg></svg>" }],
    });
  });

  it("posts the existing answer contract and rejects non-loopback URLs before fetch", async () => {
    const fetcher = vi.fn(async () => new Response(null, { status: 204 }));
    const client = new LoopbackDecisionBoardClient(fetcher as typeof globalThis.fetch);
    const answers = { q0: { answer: "Ship", actions_confirmed: ["Run smoke"], skipped: false } };

    await client.submit(boardUrl, answers);

    expect(fetcher).toHaveBeenCalledWith(`${boardUrl}/answer`, expect.objectContaining({ method: "POST", body: JSON.stringify({ answers }) }));
    await expect(client.load("http://example.com/board/abc")).rejects.toThrow(/127\.0\.0\.1/);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
