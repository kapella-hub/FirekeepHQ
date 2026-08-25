import { describe, expect, it } from "vitest";
import { RuntimeRegistry } from "../src/core/runtime-registry.js";
import { FakeRuntime } from "./helpers/fake-runtime.js";

const alpha = new FakeRuntime({
  id: "alpha",
  displayName: "Alpha",
  description: "Alpha agent",
  transport: "test",
  capabilities: ["chat", "review", "streaming"],
});

describe("RuntimeRegistry", () => {
  it("registers runtimes without provider-specific behavior", () => {
    const beta = new FakeRuntime({
      id: "beta",
      displayName: "Beta",
      description: "Beta agent",
      transport: "test",
      capabilities: ["chat"],
    });
    const registry = new RuntimeRegistry([beta, alpha]);

    expect(registry.list().map((runtime) => runtime.descriptor.id)).toEqual(["alpha", "beta"]);
    expect(registry.require("beta")).toBe(beta);
    expect(registry.supports("alpha", "review")).toBe(true);
    expect(registry.supports("beta", "review")).toBe(false);
  });

  it("rejects duplicate runtime identifiers", () => {
    expect(() => new RuntimeRegistry([alpha, alpha])).toThrow(/duplicate runtime.*alpha/i);
  });

  it("reports unknown runtimes and missing capabilities clearly", () => {
    const registry = new RuntimeRegistry([alpha]);
    expect(() => registry.require("missing")).toThrow(/unknown runtime.*missing/i);
    expect(() => registry.requireCapability("alpha", "audio-input")).toThrow(
      /Alpha.*audio-input/i,
    );
  });
});
