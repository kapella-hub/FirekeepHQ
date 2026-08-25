import { EventEmitter } from "node:events";
import { describe, expect, it, vi } from "vitest";
import {
  JsonLineDecoder,
  JsonlRpcPeer,
  type JsonlTransport,
} from "../src/main/runtime/jsonl-rpc.js";

class FakeTransport implements JsonlTransport {
  readonly events = new EventEmitter();
  readonly writes: string[] = [];
  killed = false;

  write(line: string): void { this.writes.push(line); }
  end(line?: string): void { if (line !== undefined) this.writes.push(line); }
  kill(): void { this.killed = true; }
  onStdout(listener: (chunk: Uint8Array | string) => void): () => void { this.events.on("stdout", listener); return () => this.events.off("stdout", listener); }
  onStderr(listener: (chunk: Uint8Array | string) => void): () => void { this.events.on("stderr", listener); return () => this.events.off("stderr", listener); }
  onExit(listener: (code: number | null, signal: NodeJS.Signals | null) => void): () => void { this.events.on("exit", listener); return () => this.events.off("exit", listener); }
  onError(listener: (error: Error) => void): () => void { this.events.on("error", listener); return () => this.events.off("error", listener); }
}

describe("JsonLineDecoder", () => {
  it("frames partial and multiple lines", () => {
    const decoder = new JsonLineDecoder();
    expect(decoder.push('{"a":1')).toEqual([]);
    expect(decoder.push('}\n{"b":2}\n{"c"')).toEqual([{ a: 1 }, { b: 2 }]);
    expect(decoder.push(':3}\r\n')).toEqual([{ c: 3 }]);
  });

  it("reports malformed lines and continues", () => {
    const malformed = vi.fn();
    const decoder = new JsonLineDecoder({ onMalformed: malformed });
    expect(decoder.push("nope\n{\"ok\":true}\n")).toEqual([{ ok: true }]);
    expect(malformed).toHaveBeenCalledOnce();
  });
});

describe("JsonlRpcPeer", () => {
  it("correlates responses and forwards unknown notifications", async () => {
    const transport = new FakeTransport();
    const peer = new JsonlRpcPeer(transport);
    const notification = vi.fn();
    peer.onNotification(notification);

    const response = peer.request<{ value: number }>("thing/read", { id: "x" });
    const sent = JSON.parse(transport.writes[0] ?? "null") as { id: number };
    transport.events.emit("stdout", `${JSON.stringify({ id: sent.id, result: { value: 7 } })}\n${JSON.stringify({ method: "future/event", params: { x: 1 } })}\n`);

    await expect(response).resolves.toEqual({ value: 7 });
    expect(notification).toHaveBeenCalledWith("future/event", { x: 1 }, expect.anything());
  });

  it("handles server requests and returns a correlated response", async () => {
    const transport = new FakeTransport();
    const peer = new JsonlRpcPeer(transport);
    peer.onRequest("permission/ask", async (params) => ({ accepted: params === "safe" }));

    transport.events.emit("stdout", `${JSON.stringify({ id: 88, method: "permission/ask", params: "safe" })}\n`);
    await vi.waitFor(() => expect(transport.writes).toHaveLength(1));

    expect(JSON.parse(transport.writes[0] ?? "null")).toEqual({ id: 88, result: { accepted: true } });
  });

  it("rejects requests on timeout, abort, and child exit", async () => {
    const timeoutTransport = new FakeTransport();
    const timeoutPeer = new JsonlRpcPeer(timeoutTransport, { requestTimeoutMs: 5 });
    await expect(timeoutPeer.request("slow")).rejects.toThrow(/timed out/i);

    const abortTransport = new FakeTransport();
    const abortPeer = new JsonlRpcPeer(abortTransport);
    const controller = new AbortController();
    const aborted = abortPeer.request("abortable", undefined, { signal: controller.signal });
    controller.abort();
    await expect(aborted).rejects.toThrow(/aborted/i);

    const exitTransport = new FakeTransport();
    const exitPeer = new JsonlRpcPeer(exitTransport);
    const exited = exitPeer.request("pending");
    exitTransport.events.emit("exit", 3, null);
    await expect(exited).rejects.toThrow(/exited.*3/i);
  });

  it("bounds retained stderr and surfaces malformed protocol input", () => {
    const transport = new FakeTransport();
    const protocolError = vi.fn();
    const peer = new JsonlRpcPeer(transport, { stderrLimit: 8, onProtocolError: protocolError });
    transport.events.emit("stderr", "abcdefgh");
    transport.events.emit("stderr", "ijkl");
    transport.events.emit("stdout", "broken-json\n");

    expect(peer.stderr()).toBe("efghijkl");
    expect(protocolError).toHaveBeenCalledOnce();
  });
});
