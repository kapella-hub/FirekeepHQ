// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { findSecret, memoryDraftProblem, parseTags, skillDraftProblem } from "../src/core/keep/save.js";
import { KeepPanel } from "../src/renderer/src/keep/KeepPanel.js";
import { SaveToKeepDialog } from "../src/renderer/src/keep/SaveToKeepDialog.js";
import type { StudioAction, StudioActionResult } from "../src/shared/ipc.js";

afterEach(cleanup);

// Made-up values for the detector under test, assembled at run time so the repository's own secret scan sees no secret-shaped literal.
const fakeValue = ["abcdef", "123456"].join("");

describe("Keep save validation", () => {
  it("recognises common secret shapes and leaves ordinary prose alone", () => {
    expect(findSecret("-----BEGIN OPENSSH PRIVATE KEY-----")).toBe("a private key");
    expect(findSecret("use sk-" + "a1B2".repeat(8))).toBe("an API key");
    expect(findSecret("AKIA" + "ABCDEFGHIJKLMNOP")).toBe("an AWS access key");
    expect(findSecret("postgres://app:s3cretpw@db.internal:5432/x")).toBe("a connection string with a password");
    expect(findSecret(`api_key: ${fakeValue}`)).toBe("a password or token assignment");
    expect(findSecret("Rotated the API key and moved the password into the vault under db.prod")).toBeNull();
    expect(findSecret("See https://firekeep.ai/docs.html and ssh deploy@10.0.0.4")).toBeNull();
  });

  it("requires the core fields, caps length and cleans tags", () => {
    expect(memoryDraftProblem({ action: "a", outcome: "b", tags: [] })).toBeNull();
    expect(memoryDraftProblem({ action: " ", outcome: "b", tags: [] })).toBe("What was done is required");
    expect(memoryDraftProblem({ action: "a", outcome: "x".repeat(4_001), tags: [] })).toMatch(/longer than 4000/);
    expect(skillDraftProblem({ trigger: "a", symptoms: "b", steps: "" })).toBe("Steps is required");
    expect(skillDraftProblem({ trigger: "a", symptoms: "b", steps: "c", gotchas: `token=${fakeValue}` })).toMatch(/Gotchas looks like it contains/);
    expect(parseTags("Ops, #Backup  ops, night shift!")).toEqual(["ops", "backup", "night", "shift"]);
  });
});

describe("SaveToKeepDialog", () => {
  function mount(reply?: (action: StudioAction) => Promise<StudioActionResult>) {
    const invoke = vi.fn(reply ?? (async (): Promise<StudioActionResult> => ({ type: "keep-saved", kind: "memory", detail: "Stored" })));
    const onClose = vi.fn();
    render(<SaveToKeepDialog invoke={invoke} onClose={onClose} />);
    return { invoke, onClose };
  }

  it("saves a memory with cleaned tags", async () => {
    const { invoke, onClose } = mount();
    expect(screen.getByRole("button", { name: "Save memory" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("What was done"), { target: { value: " Moved the backup " } });
    fireEvent.change(screen.getByLabelText("What came of it"), { target: { value: "No overlap" } });
    fireEvent.change(screen.getByLabelText(/^Tags/), { target: { value: "Ops, backup" } });
    fireEvent.click(screen.getByRole("button", { name: "Save memory" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "keep.learn", action: "Moved the backup", outcome: "No overlap", tags: ["ops", "backup"] }));
    expect(await screen.findByRole("status")).toHaveTextContent("Your team can recall this memory now");
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("saves a skill as a draft and says it waits for review", async () => {
    const { invoke } = mount();
    fireEvent.click(screen.getByRole("radio", { name: "Skill draft" }));
    fireEvent.change(screen.getByLabelText("When it applies"), { target: { value: "Deploy fails" } });
    fireEvent.change(screen.getByLabelText("Symptoms"), { target: { value: "Exit 1" } });
    fireEvent.change(screen.getByLabelText("Steps"), { target: { value: "1. Retry" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "keep.skillDraft", trigger: "Deploy fails", symptoms: "Exit 1", steps: "1. Retry" }));
    expect(await screen.findByRole("status")).toHaveTextContent("review queue");
  });

  it("refuses a secret without calling the Keep, and shows the Keep's own failure", async () => {
    const { invoke } = mount(async () => { throw new Error("This Firekeep kit cannot save from Studio yet."); });
    fireEvent.change(screen.getByLabelText("What was done"), { target: { value: "Set password=hunter2hunter2" } });
    fireEvent.change(screen.getByLabelText("What came of it"), { target: { value: "Works" } });
    fireEvent.click(screen.getByRole("button", { name: "Save memory" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("store secrets in the vault");
    expect(invoke).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("What was done"), { target: { value: "Rotated the credential" } });
    fireEvent.click(screen.getByRole("button", { name: "Save memory" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("cannot save from Studio yet");
  });
});

describe("KeepPanel save entry", () => {
  const props = { receipts: [], invoke: vi.fn(async (): Promise<StudioActionResult> => ({ type: "keep-items", items: [] })), onInsert: vi.fn(), onOpenKeepSettings: vi.fn(), onRetry: vi.fn() };

  it("opens the dialog when connected and is disabled in personal mode", () => {
    const { rerender } = render(<KeepPanel {...props} status={{ state: "connected", agentId: "a", personalMode: false, kitVersion: "1.9.1" }} />);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByRole("dialog", { name: "Save to the Keep" })).toBeInTheDocument();
    rerender(<KeepPanel {...props} status={{ state: "connected", agentId: "a", personalMode: true, kitVersion: "1.9.1" }} />);
    expect(screen.queryByRole("dialog", { name: "Save to the Keep" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    rerender(<KeepPanel {...props} status={{ state: "missing" }} />);
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
  });
});
