// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RenderBoundary } from "../src/renderer/src/RenderBoundary.js";

function Broken(): React.JSX.Element { throw new Error("bad renderer"); }

describe("RenderBoundary", () => {
  it("contains one broken response and keeps the surrounding transcript alive", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(<div><span>Earlier answer</span><RenderBoundary><Broken /></RenderBoundary><span>Later answer</span></div>);

    expect(screen.getByText("Earlier answer")).toBeInTheDocument();
    expect(screen.getByText("This response could not be rendered.")).toBeInTheDocument();
    expect(screen.getByText("Later answer")).toBeInTheDocument();
  });
});
