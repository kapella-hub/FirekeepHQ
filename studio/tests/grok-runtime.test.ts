import { describe, expect, it, vi } from "vitest";
import { MemorySecretStore } from "../src/core/settings-store.js";
import type { RunRequest, RuntimeEventPayload } from "../src/core/runtime.js";
import { GrokRuntime, SseDecoder } from "../src/main/runtime/grok-runtime.js";

function request(overrides: Partial<RunRequest> = {}): RunRequest {
  return {
    runId: "run-1",
    studioSessionId: "studio-1",
    prompt: "Hello Grok",
    mode: "primary",
    cwd: "C:\\work",
    permissionMode: "standard",
    ...overrides,
  };
}

function sseResponse(events: readonly unknown[]): Response {
  const encoded = new TextEncoder().encode(events.map((event) => `event: message\ndata: ${JSON.stringify(event)}\n\n`).join("") + "data: [DONE]\n\n");
  return new Response(new ReadableStream({ start(controller) { controller.enqueue(encoded.subarray(0, 17)); controller.enqueue(encoded.subarray(17)); controller.close(); } }), {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

describe("SseDecoder", () => {
  it("frames partial multi-line server-sent events", () => {
    const decoder = new SseDecoder();
    expect(decoder.push("event: x\ndata: {\"a\":")).toEqual([]);
    expect(decoder.push("1}\n\ndata: [DONE]\n\n")).toEqual([{ a: 1 }]);
  });
});

describe("GrokRuntime", () => {
  it("stores API-key auth outside settings and removes it", async () => {
    const secrets = new MemorySecretStore();
    const runtime = new GrokRuntime({ secrets, fetch: vi.fn() });

    await expect(runtime.authStatus()).resolves.toMatchObject({ state: "disconnected" });
    await expect(runtime.login({ method: "api-key", secret: "xai-key" })).resolves.toMatchObject({ state: "complete" });
    await expect(runtime.authStatus()).resolves.toMatchObject({ state: "connected" });
    await runtime.logout();
    expect(await secrets.get("grok.api-key")).toBeNull();
  });

  it("discovers current models and streams a Responses API turn", async () => {
    const secrets = new MemorySecretStore();
    await secrets.set("grok.api-key", "xai-key");
    const fetch = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/language-models")) return new Response(JSON.stringify({ models: [
        { id: "grok-4.6", object: "model", input_modalities: ["text", "image"], output_modalities: ["text"] },
        { id: "grok-image", object: "model", input_modalities: ["text"], output_modalities: ["image"] },
      ] }), { status: 200, headers: { "content-type": "application/json" } });
      expect(init?.headers).toMatchObject({ Authorization: "Bearer xai-key" });
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      expect(body).toMatchObject({ model: "grok-4.6", input: "Hello Grok", previous_response_id: "response-old", stream: true, reasoning: { effort: "high" } });
      return sseResponse([
        { type: "response.reasoning_summary_text.delta", delta: "Checking" },
        { type: "response.output_text.delta", item_id: "m1", delta: "Hi " },
        { type: "response.output_text.delta", item_id: "m1", delta: "there" },
        { type: "response.completed", response: { id: "response-new", usage: { input_tokens: 9, output_tokens: 3, total_tokens: 12, input_tokens_details: { cached_tokens: 2 }, output_tokens_details: { reasoning_tokens: 1 } } } },
      ]);
    });
    const runtime = new GrokRuntime({ secrets, fetch });
    const events: RuntimeEventPayload[] = [];

    const models = await runtime.listModels();
    const result = await runtime.run(request({ nativeSessionId: "response-old", model: "grok-4.6", effort: "high" }), (event) => events.push(event), new AbortController().signal);

    expect(models).toEqual([expect.objectContaining({ id: "grok-4.6", efforts: ["low", "medium", "high", "xhigh"], inputModalities: ["text", "image"] })]);
    expect(result).toEqual({ nativeSessionId: "response-new", finalText: "Hi there", usage: { inputTokens: 9, cachedInputTokens: 2, outputTokens: 3, reasoningTokens: 1, totalTokens: 12 } });
    expect(events.map((event) => event.kind)).toEqual(expect.arrayContaining(["reasoning.delta", "message.delta", "message.completed", "usage.updated"]));
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("fails with a useful error when no key is connected", async () => {
    const runtime = new GrokRuntime({ secrets: new MemorySecretStore(), fetch: vi.fn() });
    await expect(runtime.run(request(), () => undefined, new AbortController().signal)).rejects.toThrow(/connect Grok/i);
  });
});
