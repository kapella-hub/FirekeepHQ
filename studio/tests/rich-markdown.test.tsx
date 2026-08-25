// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RichMarkdown } from "../src/renderer/src/RichMarkdown.js";

const renderDiagram = vi.fn();
const invoke = vi.fn();
vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    render: (...args: unknown[]) => renderDiagram(...args),
  },
}));

beforeEach(() => {
  renderDiagram.mockReset();
  invoke.mockReset();
  invoke.mockResolvedValue({ type: "clipboard-written" });
  renderDiagram.mockResolvedValue({ svg: '<svg aria-label="safe graph"><g><text>Alpha</text></g><script>alert(1)</script><foreignObject>bad</foreignObject></svg>' });
  Object.defineProperty(window, "firekeepStudio", { configurable: true, value: { invoke, subscribe: () => () => undefined } });
});

describe("rich agent responses", () => {
  it("renders fenced Mermaid as a sanitized native visual", async () => {
    const { container } = render(<div className="markdown"><RichMarkdown>{"Architecture:\n\n```mermaid\ngraph TD\n A-->B\n```"}</RichMarkdown></div>);

    expect(await screen.findByLabelText("Rendered Mermaid diagram")).toBeInTheDocument();
    await waitFor(() => expect(container.querySelector("svg")).toBeInTheDocument());
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("foreignObject")).toBeNull();
    expect(renderDiagram).toHaveBeenCalledWith(expect.stringMatching(/^firekeep-diagram-/), "graph TD\n A-->B");

    fireEvent.click(screen.getByRole("button", { name: "Copy Mermaid source" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "clipboard.write", text: "graph TD\n A-->B" }));
  });

  it("keeps invalid Mermaid readable instead of losing the response", async () => {
    renderDiagram.mockRejectedValueOnce(new Error("Parse error on line 1"));
    render(<RichMarkdown>{"```mermaid\nnot a graph\n```"}</RichMarkdown>);

    expect(await screen.findByText("Diagram could not be rendered")).toBeInTheDocument();
    expect(screen.getByText("not a graph")).toBeInTheDocument();
  });
});
