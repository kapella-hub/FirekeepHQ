import { RuntimeRegistry } from "../../core/runtime-registry.js";
import type { SecretStore } from "../../core/settings-store.js";
import { ClaudeRuntime } from "./claude-runtime.js";
import { CodexRuntime } from "./codex-runtime.js";
import { GrokRuntime } from "./grok-runtime.js";
import { KiroRuntime } from "./kiro-runtime.js";

export function createRuntimeRegistry(secrets: SecretStore, appVersion: string): RuntimeRegistry {
  return new RuntimeRegistry([
    new CodexRuntime({ appVersion }),
    new ClaudeRuntime(),
    new KiroRuntime({ appVersion }),
    new GrokRuntime({ secrets }),
  ]);
}
