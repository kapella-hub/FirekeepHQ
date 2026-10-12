import { RuntimeRegistry } from "../../core/runtime-registry.js";
import { ClaudeRuntime } from "./claude-runtime.js";
import { CodexRuntime } from "./codex-runtime.js";
import { KiroRuntime } from "./kiro-runtime.js";

export function createRuntimeRegistry(appVersion: string): RuntimeRegistry {
  return new RuntimeRegistry([
    new CodexRuntime({ appVersion }),
    new ClaudeRuntime(),
    new KiroRuntime({ appVersion }),
  ]);
}
