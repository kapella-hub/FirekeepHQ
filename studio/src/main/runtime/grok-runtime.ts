import type { SecretStore } from "../../core/settings-store.js";
import type {
  AgentRuntime,
  LoginRequest,
  LoginResult,
  RunRequest,
  RunResult,
  RuntimeAuthStatus,
  RuntimeConnection,
  RuntimeEffort,
  RuntimeEventSink,
  RuntimeModel,
  RuntimeUsage,
} from "../../core/runtime.js";

const SECRET_KEY = "grok.api-key";

interface GrokRuntimeOptions {
  readonly secrets: SecretStore;
  readonly fetch?: typeof globalThis.fetch;
  readonly baseUrl?: string;
}

interface JsonObject { readonly [key: string]: unknown }

export class SseDecoder {
  readonly #decoder = new TextDecoder();
  #buffer = "";

  push(chunk: Uint8Array | string): unknown[] {
    this.#buffer += typeof chunk === "string" ? chunk : this.#decoder.decode(chunk, { stream: true });
    const frames = this.#buffer.split(/\r?\n\r?\n/);
    this.#buffer = frames.pop() ?? "";
    const values: unknown[] = [];
    for (const frame of frames) {
      const data = frame.split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      if (!data || data === "[DONE]") continue;
      try { values.push(JSON.parse(data)); } catch { /* Unknown future events are ignored, not fatal. */ }
    }
    return values;
  }
}

export class GrokRuntime implements AgentRuntime {
  readonly descriptor = {
    id: "grok",
    displayName: "Grok",
    description: "xAI Grok through the Responses API",
    transport: "https-responses",
    capabilities: ["chat", "review", "streaming", "resume", "models", "images", "usage", "reasoning"],
    loginMethods: ["api-key"],
    accent: "#f0f2f0",
  } as const;
  readonly #secrets: SecretStore;
  readonly #fetch: typeof globalThis.fetch;
  readonly #baseUrl: string;

  constructor(options: GrokRuntimeOptions) {
    this.#secrets = options.secrets;
    this.#fetch = options.fetch ?? globalThis.fetch;
    this.#baseUrl = (options.baseUrl ?? "https://api.x.ai/v1").replace(/\/$/, "");
  }

  async probe(): Promise<RuntimeConnection> {
    return (await this.#secrets.get(SECRET_KEY))
      ? { state: "ready", detail: "xAI API key is stored with operating-system encryption" }
      : { state: "disconnected", detail: "Connect an xAI API key to use Grok" };
  }

  async authStatus(): Promise<RuntimeAuthStatus> {
    try {
      return (await this.#secrets.get(SECRET_KEY))
        ? { state: "connected", label: "Encrypted API key", methods: ["api-key"] }
        : { state: "disconnected", detail: "No xAI API key stored", methods: ["api-key"] };
    } catch (error) {
      return { state: "error", detail: errorMessage(error) };
    }
  }

  async login(request: LoginRequest): Promise<LoginResult> {
    if (request.method && request.method !== "api-key") throw new Error("Grok supports API-key connection in Studio");
    if (!request.secret) throw new Error("Enter an xAI API key to connect Grok");
    await this.#secrets.set(SECRET_KEY, request.secret);
    return { state: "complete", message: "Grok API key stored with operating-system encryption." };
  }

  async logout(): Promise<void> { await this.#secrets.delete(SECRET_KEY); }

  async listModels(): Promise<RuntimeModel[]> {
    const key = await this.#requireKey();
    const response = await this.#fetch(`${this.#baseUrl}/language-models`, { headers: { Authorization: `Bearer ${key}`, Accept: "application/json" } });
    await assertResponse(response, "xAI model discovery");
    const payload = object(await response.json());
    const models = array(payload.models).map(object).flatMap((model): RuntimeModel[] => {
      const id = string(model.id);
      if (!id) return [];
      const outputs = array(model.output_modalities).filter((value): value is string => typeof value === "string");
      if (outputs.length && !outputs.includes("text")) return [];
      const inputModalities = array(model.input_modalities).filter(isInputModality);
      const efforts = grokEfforts(id);
      return [{
        id,
        displayName: id,
        ...(efforts.length ? { efforts } : {}),
        ...(inputModalities.length ? { inputModalities } : {}),
      }];
    });
    return models.map((model, index) => index === 0 ? { ...model, isDefault: true } : model);
  }

  async run(request: RunRequest, sink: RuntimeEventSink, signal: AbortSignal): Promise<RunResult> {
    const key = await this.#requireKey();
    const model = request.model ?? await this.#defaultModel();
    const body = {
      model,
      input: request.prompt,
      stream: true,
      store: true,
      ...(request.nativeSessionId && (request.mode === "primary" || request.mode === "handoff") ? { previous_response_id: request.nativeSessionId } : {}),
      ...(request.effort ? { reasoning: { effort: request.effort } } : {}),
    };
    const response = await this.#fetch(`${this.#baseUrl}/responses`, {
      method: "POST",
      headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
      signal,
    });
    await assertResponse(response, "Grok response");
    if (!response.body) throw new Error("Grok returned no response stream");

    const decoder = new SseDecoder();
    const reader = response.body.getReader();
    let finalText = "";
    let responseId: string | undefined;
    let usage: RuntimeUsage | undefined;
    for (;;) {
      const next = await reader.read();
      if (next.done) break;
      for (const raw of decoder.push(next.value)) {
        const event = object(raw);
        const type = string(event.type);
        if (type === "response.output_text.delta") {
          const delta = string(event.delta) ?? "";
          finalText += delta;
          sink({ kind: "message.delta", messageId: string(event.item_id) ?? "grok-message", role: role(request), text: delta }, raw);
        } else if (type === "response.reasoning_summary_text.delta" || type === "response.reasoning_text.delta") {
          sink({ kind: "reasoning.delta", itemId: string(event.item_id) ?? "grok-reasoning", text: string(event.delta) ?? "" }, raw);
        } else if (type === "response.completed") {
          const complete = object(event.response);
          responseId = string(complete.id) ?? responseId;
          if (!finalText) finalText = responseText(complete);
          usage = responsesUsage(object(complete.usage));
        } else if (type === "response.failed" || type === "error") {
          const error = object(event.error ?? object(event.response).error);
          throw new Error(string(error.message) ?? "Grok response failed");
        }
      }
    }
    sink({ kind: "message.completed", messageId: "grok-message", role: role(request), text: finalText });
    if (usage) sink({ kind: "usage.updated", usage });
    return { ...(responseId ? { nativeSessionId: responseId } : {}), finalText, ...(usage ? { usage } : {}) };
  }

  async #requireKey(): Promise<string> {
    const key = await this.#secrets.get(SECRET_KEY);
    if (!key) throw new Error("Connect Grok with /connect grok --method api-key before using it");
    return key;
  }

  async #defaultModel(): Promise<string> {
    const models = await this.listModels();
    const suitable = models.find((model) => /^grok/i.test(model.id) && !/(image|video|voice|tts)/i.test(model.id)) ?? models[0];
    if (!suitable) throw new Error("xAI returned no models; choose a model explicitly after checking the account");
    return suitable.id;
  }
}

async function assertResponse(response: Response, operation: string): Promise<void> {
  if (response.ok) return;
  const detail = (await response.text()).slice(0, 4_096);
  throw new Error(`${operation} failed with HTTP ${response.status}${detail ? `: ${detail}` : ""}`);
}

function responsesUsage(raw: JsonObject): RuntimeUsage {
  const inputTokens = number(raw.input_tokens);
  const outputTokens = number(raw.output_tokens);
  const totalTokens = number(raw.total_tokens);
  const cachedInputTokens = number(object(raw.input_tokens_details).cached_tokens);
  const reasoningTokens = number(object(raw.output_tokens_details).reasoning_tokens);
  return {
    ...(inputTokens !== undefined ? { inputTokens } : {}),
    ...(cachedInputTokens !== undefined ? { cachedInputTokens } : {}),
    ...(outputTokens !== undefined ? { outputTokens } : {}),
    ...(reasoningTokens !== undefined ? { reasoningTokens } : {}),
    ...(totalTokens !== undefined ? { totalTokens } : {}),
  };
}

function responseText(response: JsonObject): string {
  return array(response.output).map(object).flatMap((item) => array(item.content).map(object)).filter((content) => content.type === "output_text").map((content) => string(content.text) ?? "").join("");
}

function role(request: RunRequest): "assistant" | "reviewer" { return request.mode === "review" ? "reviewer" : "assistant"; }
function grokEfforts(modelId: string): readonly RuntimeEffort[] {
  if (/grok-4\.6|grok-4\.20.*multi-agent/i.test(modelId)) return ["low", "medium", "high", "xhigh"];
  if (/grok-4\.5/i.test(modelId)) return ["low", "medium", "high"];
  return [];
}
function isInputModality(value: unknown): value is "text" | "image" | "audio" { return value === "text" || value === "image" || value === "audio"; }
function object(value: unknown): JsonObject { return value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {}; }
function array(value: unknown): unknown[] { return Array.isArray(value) ? value : []; }
function string(value: unknown): string | undefined { return typeof value === "string" ? value : undefined; }
function number(value: unknown): number | undefined { return typeof value === "number" && Number.isFinite(value) ? value : undefined; }
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : String(error); }
