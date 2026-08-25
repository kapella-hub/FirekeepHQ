import { describe, expect, it } from "vitest";
import { withStudioVisualHint } from "../src/core/visuals.js";

describe("Studio visual intent", () => {
  it("adds a compact Mermaid hint only when the user asks for a visual", () => {
    expect(withStudioVisualHint("Explain the session model")).toBe("Explain the session model");
    const visual = withStudioVisualHint("Create an example graph of the session model");
    expect(visual).toContain("fenced `mermaid` block");
    expect(visual).toContain("do not substitute ASCII art");
  });

  it("does not duplicate an explicit Mermaid request", () => {
    const prompt = "Render this:\n```mermaid\ngraph TD\n A-->B\n```";
    expect(withStudioVisualHint(prompt)).toBe(prompt);
  });
});
