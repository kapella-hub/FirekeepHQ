import type { AgentRuntime, RuntimeCapability } from "./runtime.js";

export class RuntimeRegistry {
  readonly #runtimes = new Map<string, AgentRuntime>();

  constructor(runtimes: readonly AgentRuntime[] = []) {
    for (const runtime of runtimes) this.register(runtime);
  }

  register(runtime: AgentRuntime): void {
    const id = runtime.descriptor.id.trim();
    if (!id) throw new Error("runtime id cannot be empty");
    if (this.#runtimes.has(id)) throw new Error(`duplicate runtime id: ${id}`);
    this.#runtimes.set(id, runtime);
  }

  list(): AgentRuntime[] {
    return [...this.#runtimes.values()].sort((left, right) =>
      left.descriptor.displayName.localeCompare(right.descriptor.displayName),
    );
  }

  require(id: string): AgentRuntime {
    const runtime = this.#runtimes.get(id);
    if (!runtime) throw new Error(`unknown runtime: ${id}`);
    return runtime;
  }

  supports(id: string, capability: RuntimeCapability): boolean {
    return this.require(id).descriptor.capabilities.includes(capability);
  }

  requireCapability(id: string, capability: RuntimeCapability): AgentRuntime {
    const runtime = this.require(id);
    if (!runtime.descriptor.capabilities.includes(capability)) {
      throw new Error(`${runtime.descriptor.displayName} does not support ${capability}`);
    }
    return runtime;
  }
}
