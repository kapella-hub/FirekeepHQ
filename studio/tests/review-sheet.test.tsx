// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { WorkspaceChanges } from "../src/core/workspace-changes.js";
import { ReviewSheet } from "../src/renderer/src/review/ReviewSheet.js";
import type { StudioAction, StudioActionResult } from "../src/shared/ipc.js";

afterEach(cleanup);

const changes: WorkspaceChanges = {
  state: "ok",
  branch: "feature/inbox",
  truncated: false,
  files: [
    { path: "src/a.ts", status: "modified", accepted: false, additions: 2, deletions: 1 },
    { path: "notes.md", status: "untracked", accepted: false, additions: null, deletions: null },
  ],
};
const DIFF = "diff --git a/src/a.ts b/src/a.ts\n--- a/src/a.ts\n+++ b/src/a.ts\n@@ -1,3 +1,4 @@\n one\n-two\n+2\n three\n+four\n";

function mount(overrides: { changes?: WorkspaceChanges; agentWorking?: boolean; respond?: (action: StudioAction) => StudioActionResult | undefined } = {}) {
  let current = overrides.changes ?? changes;
  const request = vi.fn(async (action: StudioAction): Promise<StudioActionResult> => {
    const custom = overrides.respond?.(action);
    if (custom) return custom;
    if (action.type === "changes.diff") return { type: "change-diff", diff: { path: action.path, diff: action.path === "src/a.ts" ? DIFF : "--- /dev/null\n+++ b/notes.md\n@@ -0,0 +1,1 @@\n+draft\n", binary: false, truncated: false } };
    if (action.type === "changes.accept") current = { ...current, files: current.files.map((file) => file.path === action.path ? { ...file, accepted: true } : file) };
    if (action.type === "changes.revert") current = { ...current, files: current.files.filter((file) => file.path !== action.path) };
    return { type: "changes", changes: current };
  });
  const onSend = vi.fn();
  const onClose = vi.fn();
  function Host(): React.JSX.Element {
    const [value, setValue] = React.useState(current);
    return <ReviewSheet changes={value} request={request} onChanges={setValue} onSend={onSend} onClose={onClose} agentWorking={overrides.agentWorking ?? false} />;
  }
  render(<Host />);
  return { request, onSend, onClose };
}

describe("ReviewSheet", () => {
  it("shows the changed files and the selected file's numbered diff", async () => {
    const { request } = mount();
    expect(screen.getByText("2 changed files · 0 accepted · feature/inbox")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Comment on line 2" })).toBeInTheDocument();
    expect(screen.getByText("four")).toBeInTheDocument();
    expect(request).toHaveBeenCalledWith({ type: "changes.list" });
    expect(request).toHaveBeenCalledWith({ type: "changes.diff", path: "src/a.ts" });

    fireEvent.click(screen.getByRole("button", { name: /notes\.md/ }));
    expect(await screen.findByText("draft")).toBeInTheDocument();
  });

  it("accepts a file and removes a reverted one from the list", async () => {
    const { request } = mount();
    await screen.findByRole("button", { name: "Comment on line 2" });
    fireEvent.click(screen.getByRole("button", { name: /Accept/ }));
    await waitFor(() => expect(request).toHaveBeenCalledWith({ type: "changes.accept", path: "src/a.ts" }));
    expect(await screen.findByText("2 changed files · 1 accepted · feature/inbox")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Unaccept/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Revert/ }));
    await waitFor(() => expect(request).toHaveBeenCalledWith({ type: "changes.revert", path: "src/a.ts" }));
    expect(await screen.findByText("1 changed file · 0 accepted · feature/inbox")).toBeInTheDocument();
    expect(await screen.findByText("draft")).toBeInTheDocument();
  });

  it("collects line and file comments and sends them as one message", async () => {
    const { onSend } = mount();
    expect(screen.getByRole("button", { name: /Send to agent/ })).toBeDisabled();
    fireEvent.click(await screen.findByRole("button", { name: "Comment on line 2" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Comment on line 2" }), { target: { value: "Spell it out." } });
    fireEvent.click(screen.getByRole("button", { name: "Add comment" }));
    fireEvent.click(screen.getByRole("button", { name: "Comment" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Comment on the whole file" }), { target: { value: "Add a test." } });
    fireEvent.click(screen.getByRole("button", { name: "Add comment" }));

    expect(screen.getByText("2 comments ready to send")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Send to agent/ }));
    expect(onSend).toHaveBeenCalledTimes(1);
    const prompt = onSend.mock.calls[0]?.[0] as string;
    expect(prompt).toContain("### `src/a.ts`");
    expect(prompt).toContain("- **Whole file**: Add a test.");
    expect(prompt).toContain("- **Line 2** (`2`): Spell it out.");
  });

  it("blocks revert and send while the agent works, and shows failures inline", async () => {
    mount({ agentWorking: true, respond: (action) => action.type === "changes.accept" ? { type: "error", message: "Could not accept src/a.ts: index is locked" } : undefined });
    await screen.findByRole("button", { name: "Comment on line 2" });
    expect(screen.getByRole("button", { name: /Revert/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /Accept/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("index is locked");
  });

  it("explains an empty or unreadable workspace, and Escape closes without reaching the app", async () => {
    const appEscape = vi.fn();
    window.addEventListener("keydown", appEscape);
    const { onClose } = mount({ changes: { state: "not-a-repo", branch: null, files: [], truncated: false } });
    expect(await screen.findByText(/not a git repository/)).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
    expect(appEscape).not.toHaveBeenCalled();
    window.removeEventListener("keydown", appEscape);
  });
});
