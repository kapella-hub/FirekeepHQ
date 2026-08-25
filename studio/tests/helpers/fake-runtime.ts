import type {
  AgentRuntime,
  LoginRequest,
  LoginResult,
  RunRequest,
  RunResult,
  RuntimeAuthStatus,
  RuntimeConnection,
  RuntimeEventSink,
  RuntimeModel,
  RuntimeUsage,
} from "../../src/core/runtime.js";

export class FakeRuntime implements AgentRuntime {
  readonly runs: RunRequest[] = [];

  constructor(
    readonly descriptor: AgentRuntime["descriptor"],
    private readonly answer = `response from ${descriptor.id}`,
    private readonly usage?: RuntimeUsage,
  ) {}

  async probe(): Promise<RuntimeConnection> {
    return { state: "ready", version: "1.0.0", detail: "Fake runtime ready" };
  }

  async authStatus(): Promise<RuntimeAuthStatus> {
    return { state: "connected", label: `${this.descriptor.displayName} test account` };
  }

  async login(_request: LoginRequest): Promise<LoginResult> {
    return { state: "complete", message: "Connected" };
  }

  async logout(): Promise<void> {}

  async listModels(): Promise<RuntimeModel[]> {
    return [{ id: `${this.descriptor.id}-default`, displayName: "Default", isDefault: true, efforts: ["low", "medium", "high", "xhigh", "max"] }];
  }

  async run(
    request: RunRequest,
    sink: RuntimeEventSink,
    _signal: AbortSignal,
  ): Promise<RunResult> {
    this.runs.push(request);
    sink({ kind: "message.delta", messageId: "answer", role: request.mode === "review" ? "reviewer" : "assistant", text: this.answer });
    sink({ kind: "message.completed", messageId: "answer", role: request.mode === "review" ? "reviewer" : "assistant", text: this.answer });
    if (this.usage) sink({ kind: "usage.updated", usage: this.usage });
    return { nativeSessionId: `${this.descriptor.id}-session`, finalText: this.answer, ...(this.usage ? { usage: this.usage } : {}) };
  }
}
