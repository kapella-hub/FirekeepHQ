import { afterEach, describe, expect, it, vi } from "vitest";
import { StudioDecisionBoardReceiver } from "../src/main/decision-board-receiver.js";

const receivers: StudioDecisionBoardReceiver[] = [];
afterEach(async () => { await Promise.all(receivers.splice(0).map((receiver) => receiver.close())); });

describe("Studio Decision Board receiver", () => {
  it("accepts one authenticated loopback notification and deduplicates it", async () => {
    const onBoard = vi.fn(async () => undefined);
    const receiver = new StudioDecisionBoardReceiver(onBoard);
    receivers.push(receiver);
    const environment = await receiver.start();
    const boardUrl = "http://127.0.0.1:43123/board/native-board";
    const request = () => fetch(environment.FIREKEEP_DECISION_NOTIFY_URL, {
      method: "POST",
      headers: { Authorization: `Bearer ${environment.FIREKEEP_DECISION_NOTIFY_TOKEN}`, "Content-Type": "application/json" },
      body: JSON.stringify({ board_url: boardUrl }),
    });

    expect((await request()).status).toBe(202);
    expect((await request()).status).toBe(202);
    await vi.waitFor(() => expect(onBoard).toHaveBeenCalledTimes(1));
    expect(onBoard).toHaveBeenCalledWith(boardUrl);
  });

  it("rejects missing credentials and non-board URLs", async () => {
    const onBoard = vi.fn(async () => undefined);
    const receiver = new StudioDecisionBoardReceiver(onBoard);
    receivers.push(receiver);
    const environment = await receiver.start();

    const unauthorized = await fetch(environment.FIREKEEP_DECISION_NOTIFY_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const invalid = await fetch(environment.FIREKEEP_DECISION_NOTIFY_URL, { method: "POST", headers: { Authorization: `Bearer ${environment.FIREKEEP_DECISION_NOTIFY_TOKEN}`, "Content-Type": "application/json" }, body: JSON.stringify({ board_url: "http://example.com/board/x" }) });

    expect(unauthorized.status).toBe(401);
    expect(invalid.status).toBe(400);
    expect(onBoard).not.toHaveBeenCalled();
  });
});
