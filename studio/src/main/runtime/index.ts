import { RuntimeRegistry } from "../../core/runtime-registry.js";
import type { SecretStore } from "../../core/settings-store.js";
import { ClaudeRuntime } from "./claude-runtime.js";
import { CodexRuntime } from "./codex-runtime.js";
import { GrokRuntime } from "./grok-runtime.js";
import { KiroRuntime } from "./kiro-runtime.js";

export function createRuntimeRegistry(secrets: SecretStore): RuntimeRegistry {
  return new RuntimeRegistry([
    new CodexRuntime(),
    new ClaudeRuntime(),
    new KiroRuntime(),
    new GrokRuntime({ secrets }),
  ]);
}
