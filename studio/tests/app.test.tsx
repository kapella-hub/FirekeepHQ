// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MissionSnapshot } from "../src/core/mission.js";
import type { StudioSnapshot } from "../src/core/studio-service.js";
import type { StudioSessionSummary } from "../src/core/session-store.js";
import type { RuntimeDescriptor, RuntimeEvent, RuntimeModel } from "../src/core/runtime.js";
import { App } from "../src/renderer/src/App.js";
import type { StudioAction, StudioActionResult, StudioBridge, StudioPushEvent, StudioUpdateState } from "../src/shared/ipc.js";
import type { DecisionBoardDocument } from "../src/shared/decision-board.js";
import type { KeepItem, KeepStatus } from "../src/shared/keep.js";

const runtimes: RuntimeDescriptor[] = [
  { id: "alpha", displayName: "Alpha", description: "Test agent", transport: "test-rpc", capabilities: ["chat", "review", "models", "reasoning", "tools", "firekeep-memory", "firekeep-hooks"], loginMethods: ["browser"], accent: "#ff7a2f" },
  { id: "beta", displayName: "Beta", description: "Test reviewer", transport: "test-acp", capabilities: ["chat", "review"], loginMethods: ["device"], accent: "#8f83ff" },
];

function state(primaryRuntimeId: string | null = null): StudioSnapshot {
  return {
    version: 1,
    activeSessionId: "session-1",
    workspacePath: null,
    primaryRuntimeId,
    reviewerRuntimeIds: [],
    reviewerMode: "off",
    selectedModels: {},
    selectedEfforts: {},
    permissionModes: {},
    nativeSessionIds: {},
    tokenBudget: null,
    theme: "system",
    recentWorkspaces: [],
    keepPanelOpen: true,
    activeRunId: null,
    eventCount: 0,
    usage: { tokens: 0, freshTokens: 0, cachedTokens: 0, costUsd: 0, runs: 0, totalRuns: 0, measuredRuns: 0, byRuntime: {} },
    mission: null,
  };
}

function draftMission(checks: MissionSnapshot["checks"] = []): MissionSnapshot {
  return {
    version: 1,
    id: "mission-1",
    goal: "Make the behavior demonstrably correct",
    phase: "draft",
    createdAt: "2026-08-24T00:00:00.000Z",
    updatedAt: "2026-08-24T00:00:00.000Z",
    startedAt: null,
    completedAt: null,
    workspacePath: "C:\\workspace",
    primaryRuntimeId: "alpha",
    reviewerRuntimeIds: ["beta"],
    runtimeSettings: {},
    checks,
    tokenBudget: 50_000,
    maxRepairAttempts: 1,
    attempt: 0,
    nextAction: null,
    nextReviewerIndex: 0,
    measuredTokensAtStart: 0,
    measuredTokens: 0,
    lastPrimaryText: "",
    manualRepairNote: null,
    verificationPassed: null,
    checkReceipts: [],
    reviewReceipts: [],
    outcome: null,
    blockReason: null,
    executionApprovedAt: null,
  };
}

let pushStudioEvent: ((event: StudioPushEvent) => void) | null = null;

function installBridge(options: { readonly snapshot?: StudioSnapshot; readonly sessions?: readonly StudioSessionSummary[]; readonly models?: readonly RuntimeModel[] | (() => readonly RuntimeModel[]); readonly dashboardAvailable?: boolean; readonly events?: readonly RuntimeEvent[]; readonly decisionBoard?: DecisionBoardDocument; readonly pushEvents?: readonly StudioPushEvent[]; readonly clipboardText?: string; readonly update?: StudioUpdateState; readonly updateResult?: StudioUpdateState; readonly keepStatus?: KeepStatus; readonly testCommand?: string | null; readonly keepItems?: readonly KeepItem[]; readonly actionError?: { readonly actionType: StudioAction["type"]; readonly message: string } } = {}): ReturnType<typeof vi.fn<(action: StudioAction) => Promise<StudioActionResult>>> {
  let snapshot = options.snapshot ?? state();
  let sessions = [...(options.sessions ?? [{ id: "session-1", name: "UI test", color: "ember" as const, createdAt: "2026-08-24T00:00:00.000Z", updatedAt: "2026-08-24T00:00:00.000Z", eventCount: 0, nativeSessionIds: {} }])];
  const invoke = vi.fn(async (action: StudioAction): Promise<StudioActionResult> => {
    if (options.actionError?.actionType === action.type) return { type: "error", message: options.actionError.message };
    if (action.type === "bootstrap") return { type: "bootstrap", appName: "Firekeep Studio", version: "0.4.0", dashboardAvailable: options.dashboardAvailable ?? true, update: options.update ?? { phase: "idle", currentVersion: "0.4.0", availableVersion: null, progressPercent: null, automatic: true, detail: "Studio checks for verified updates automatically." }, snapshot, runtimes, events: options.events ?? [], sessions };
    if (action.type === "runtime.probe") return { type: "diagnostics", items: runtimes.map((runtime) => ({ runtimeId: runtime.id, connection: { state: "ready", detail: "Ready" }, auth: { state: "connected", label: "Connected" } })) };
    if (action.type === "runtime.models") return { type: "models", runtimeId: action.runtimeId, items: typeof options.models === "function" ? options.models() : options.models ?? [] };
    if (action.type === "command.complete") return { type: "completions", items: action.input === "/" ? [{ value: "/doctor", label: "/doctor", description: "Check runtimes" }] : [] };
    if (action.type === "decision.load" && options.decisionBoard) return { type: "decision", board: options.decisionBoard };
    if (action.type === "decision.submit") return { type: "decision-submitted", url: action.url };
    if (action.type === "clipboard.read") return { type: "clipboard-read", text: options.clipboardText ?? "" };
    if (action.type === "clipboard.write") return { type: "clipboard-written" };
    if (action.type === "update.check" || action.type === "update.install") return { type: "update", state: options.updateResult ?? options.update ?? { phase: "current", currentVersion: "0.4.0", availableVersion: null, progressPercent: null, automatic: true, detail: "Firekeep Studio 0.4.0 is current." } };
    if (action.type === "keep.status" || action.type === "keep.retry") return { type: "keep-status", status: options.keepStatus ?? { state: "missing" } };
    if (action.type === "workspace.detectTest") return { type: "test-command", command: options.testCommand ?? null };
    if (action.type === "keep.skills") return { type: "keep-items", items: options.keepItems ?? [] };
    if (action.type === "settings.keepPanel") snapshot = { ...snapshot, keepPanelOpen: action.open };
    if (action.type === "workspace.set") snapshot = { ...snapshot, workspacePath: action.path };
    if (action.type === "session.update") sessions = sessions.map((session) => session.id === action.sessionId ? { ...session, name: action.name, color: action.color } : session);
    if (action.type === "primary.set") snapshot = { ...snapshot, primaryRuntimeId: action.runtimeId };
    return { type: "state", snapshot, ...(action.type.startsWith("session.") ? { sessions } : {}) };
  });
  const bridge: StudioBridge = { invoke, subscribe: (listener) => {
    pushStudioEvent = listener;
    for (const event of options.pushEvents ?? []) queueMicrotask(() => listener(event));
    return () => { if (pushStudioEvent === listener) pushStudioEvent = null; };
  } };
  Object.defineProperty(window, "firekeepStudio", { configurable: true, value: bridge });
  Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
  return invoke;
}

afterEach(() => {
  pushStudioEvent = null;
  delete document.documentElement.dataset.theme;
  cleanup();
});

async function openRuntimeCenter(): Promise<HTMLElement> {
  fireEvent.click(await screen.findByRole("button", { name: /Primary runtime:/i }));
  fireEvent.click(screen.getByRole("button", { name: "Manage runtimes" }));
  return screen.getByRole("dialog", { name: "Runtime Center" });
}

describe("Firekeep Studio renderer", () => {
  it("boots the runtime-neutral desk and uses the same typed action for a primary selection", async () => {
    const invoke = installBridge();
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Agents come and go. The Keep stays." })).toBeInTheDocument();
    expect(screen.getByText("FIREKEEP STUDIO")).toBeInTheDocument();
    expect(screen.queryByText("A NEW KIND OF AGENT DESK")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Choose a workspace/i }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "workspace.choose" }));
    fireEvent.click(screen.getAllByRole("button", { name: /Alpha/i })[0] as HTMLElement);

    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "primary.set", runtimeId: "alpha" }));
    expect(await screen.findByText(/0 fresh · 0 total/)).toBeInTheDocument();
    expect(screen.getByText("No reviewers")).toBeInTheDocument();
  });

  it("applies the persisted appearance and announces the next theme", async () => {
    const invoke = installBridge({ snapshot: { ...state(), theme: "light" } });
    render(<App />);

    await screen.findByRole("heading", { name: "Agents come and go. The Keep stays." });
    await waitFor(() => expect(document.documentElement).toHaveAttribute("data-theme", "light"));

    fireEvent.click(screen.getByRole("button", { name: "Appearance: Light. Switch to system theme" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "theme.set", theme: "system" }));
  });

  it("shows slash completion", async () => {
    const invoke = installBridge();
    render(<App />);
    await screen.findByRole("heading", { name: "Agents come and go. The Keep stays." });
    const composer = screen.getByRole("textbox");
    fireEvent.change(composer, { target: { value: "/" } });
    expect(await screen.findByRole("option", { name: /doctor/i })).toBeInTheDocument();
    expect(invoke).toHaveBeenCalledWith({ type: "command.complete", input: "/" });
  });

  it("keeps row clicks for selection and opens name and color editing from the palette", async () => {
    const invoke = installBridge({ sessions: [
      { id: "session-1", name: "UI test", color: "ember", createdAt: "2026-08-24T00:00:00.000Z", updatedAt: "2026-08-24T00:00:00.000Z", eventCount: 0, nativeSessionIds: {} },
      { id: "session-2", name: "Research", color: "slate", createdAt: "2026-08-23T00:00:00.000Z", updatedAt: "2026-08-23T00:00:00.000Z", eventCount: 3, nativeSessionIds: {} },
    ] });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: /Research.*3 events/i }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "session.resume", sessionId: "session-2" }));
    expect(screen.queryByRole("textbox", { name: "Session name" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Customize Research" }));
    const name = screen.getByRole("textbox", { name: "Session name" });
    expect(name).toHaveValue("Research");
    fireEvent.change(name, { target: { value: "Release plan" } });
    fireEvent.click(screen.getByRole("radio", { name: "Ocean" }));
    fireEvent.click(screen.getByRole("button", { name: "Save session" }));

    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "session.update", sessionId: "session-2", name: "Release plan", color: "ocean" }));
    expect(await screen.findByText("Release plan")).toBeInTheDocument();
  });

  it("keeps the titlebar session label passive and edits only from its palette", async () => {
    installBridge();
    render(<App />);

    await screen.findByRole("button", { name: "Customize current session UI test" });
    const title = document.querySelector(".titlebar-center .session-title");
    expect(title).toHaveTextContent("UI test");
    expect(title.closest("button")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Customize current session UI test" }));
    expect(screen.getByRole("textbox", { name: "Session name" })).toHaveValue("UI test");
  });

  it("shows a quiet verified-update control and promotes it to restart when ready", async () => {
    const ready: StudioUpdateState = { phase: "ready", currentVersion: "0.4.0", availableVersion: "0.4.1", progressPercent: 100, automatic: true, detail: "Studio 0.4.1 is verified and ready." };
    const invoke = installBridge({ updateResult: ready });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: /Check for Studio updates/i }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "update.check" }));
    fireEvent.click(await screen.findByRole("button", { name: "Restart to install Firekeep Studio 0.4.1" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "update.install" }));
  });

  it("makes the mission harness discoverable without exposing a process bridge", async () => {
    installBridge();
    render(<App />);
    await screen.findByText("Give an agent a goal and verify the result.");

    fireEvent.click(screen.getByRole("button", { name: "Start a mission" }));

    expect(screen.getByRole("textbox")).toHaveValue('/mission new "');
  });

  it("blocks invalid mission and review actions with actionable setup affordances", async () => {
    const invoke = installBridge({
      snapshot: {
        ...state("alpha"),
        workspacePath: "C:\\workspace",
        reviewerRuntimeIds: ["beta"],
        mission: draftMission(),
      },
    });
    render(<App />);

    const missionRun = await screen.findByRole("button", { name: "Run" });
    expect(missionRun).toBeDisabled();
    expect(missionRun).toHaveAttribute("title", "Add at least one deterministic check before running");
    expect(screen.getByText("Add a deterministic check before running.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Add check" }));
    expect(screen.getByRole("textbox")).toHaveValue("/mission check add -- ");

    const reviewRun = screen.getByRole("button", { name: "Run now" });
    expect(reviewRun).toBeDisabled();
    expect(reviewRun).toHaveAttribute("title", "Send a primary message before running a review");
    fireEvent.keyDown(window, { key: "R", ctrlKey: true, shiftKey: true });
    expect(invoke.mock.calls.some(([action]) => action.type === "mission.run" || action.type === "review.run")).toBe(false);
  });

  it("enables mission and review actions after their prerequisites exist", async () => {
    const base = { runId: "run-1", studioSessionId: "session-1", runtimeId: "alpha", timestamp: "2026-08-24T00:00:00.000Z" };
    const invoke = installBridge({
      snapshot: {
        ...state("alpha"),
        workspacePath: "C:\\workspace",
        reviewerRuntimeIds: ["beta"],
        mission: draftMission([{ id: "check-1", name: "tests", command: "npm test", timeoutMs: 60_000 }]),
      },
      events: [{ ...base, id: "event-1", payload: { kind: "message.completed", messageId: "message-1", role: "assistant", text: "A primary answer" }, raw: null }],
    });
    render(<App />);

    const missionRun = await screen.findByRole("button", { name: "Run" });
    const reviewRun = screen.getByRole("button", { name: "Run now" });
    expect(missionRun).toBeEnabled();
    expect(reviewRun).toBeEnabled();

    fireEvent.click(missionRun);
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "mission.run" }));
    fireEvent.click(reviewRun);
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "review.run" }));
  });

  it("surfaces typed action failures without an Electron handler stack", async () => {
    installBridge({
      snapshot: {
        ...state("alpha"),
        workspacePath: "C:\\workspace",
        mission: draftMission([{ id: "check-1", name: "tests", command: "npm test", timeoutMs: 60_000 }]),
      },
      actionError: { actionType: "mission.run", message: "verification runner unavailable" },
    });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Run" }));

    expect(await screen.findByText("verification runner unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/Error occurred in handler|Error invoking remote method/i)).not.toBeInTheDocument();
  });

  it("shows active work prominently on the Firekeep logo", async () => {
    installBridge({ snapshot: { ...state("alpha"), activeRunId: "run-1" } });

    render(<App />);

    expect(await screen.findByLabelText("Agent working")).toHaveClass("brand-icon");
  });

  it("distinguishes configured Keep integration from provider-direct runtimes", async () => {
    installBridge();
    render(<App />);

    const center = await openRuntimeCenter();
    expect(within(center).getByText("Keep memory + automatic hooks")).toBeInTheDocument();
    expect(within(center).getByText("Provider direct · no Keep memory")).toBeInTheDocument();
  });

  it("derives reasoning choices from the selected live runtime model", async () => {
    const invoke = installBridge({
      snapshot: { ...state("alpha"), selectedModels: { alpha: "live-model" } },
      models: [{ id: "live-model", displayName: "Live model", isDefault: true, efforts: ["low", "high"] }],
    });

    render(<App />);

    const reasoning = await screen.findByLabelText("Reasoning");
    await waitFor(() => expect(reasoning.querySelectorAll("option")).toHaveLength(3));
    expect([...reasoning.querySelectorAll("option")].map((option) => option.textContent)).toEqual(["Provider default", "low", "high"]);
    expect(screen.queryByRole("option", { name: "medium" })).not.toBeInTheDocument();

    fireEvent.change(reasoning, { target: { value: "high" } });
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "effort.set", runtimeId: "alpha", effort: "high" }));
    fireEvent.change(reasoning, { target: { value: "" } });
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "effort.set", runtimeId: "alpha", effort: null }));
  });

  it("visibly refreshes live models and replaces stale provider options", async () => {
    let reads = 0;
    const invoke = installBridge({
      snapshot: state("alpha"),
      models: () => ++reads === 1
        ? [{ id: "old", displayName: "Old model", isDefault: true, efforts: ["low"] }]
        : [{ id: "new", displayName: "New model", isDefault: true, efforts: ["high"] }, { id: "fast", displayName: "Fast model", efforts: ["low"] }],
    });
    render(<App />);

    expect(await screen.findByRole("option", { name: "Old model" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh live model options" }));

    expect(await screen.findByRole("option", { name: "New model" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Old model" })).not.toBeInTheDocument();
    expect(screen.getByText("2 live models updated")).toBeInTheDocument();
    expect(invoke).toHaveBeenCalledWith({ type: "runtime.models", runtimeId: "alpha" });
  });

  it("uses a status-aware primary runtime picker with keyboard selection", async () => {
    const invoke = installBridge({ snapshot: state("alpha") });
    render(<App />);

    const picker = await screen.findByRole("button", { name: /Primary runtime: Alpha, ready/i });
    fireEvent.keyDown(picker, { key: "ArrowDown" });
    expect(screen.getByRole("listbox", { name: "Primary runtime" })).toBeInTheDocument();
    fireEvent.keyDown(picker, { key: "ArrowDown" });
    fireEvent.keyDown(picker, { key: "Enter" });

    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "primary.set", runtimeId: "beta" }));
  });

  it("copies responses through typed IPC and pastes at the composer caret", async () => {
    const base = { runId: "run-1", studioSessionId: "session-1", runtimeId: "alpha", timestamp: "2026-08-24T00:00:00.000Z" };
    const invoke = installBridge({
      snapshot: state("alpha"),
      clipboardText: "clipboard text",
      events: [{ ...base, id: "event-1", payload: { kind: "message.completed", messageId: "message-1", role: "assistant", text: "A useful answer" }, raw: null }],
    });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Copy response" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "clipboard.write", text: "A useful answer" }));

    const composer = screen.getByRole("textbox");
    fireEvent.change(composer, { target: { value: "Before after", selectionStart: 7, selectionEnd: 7 } });
    composer.focus();
    composer.setSelectionRange(7, 7);
    fireEvent.click(screen.getByRole("button", { name: "Paste from clipboard" }));

    await waitFor(() => expect(composer).toHaveValue("Before clipboard textafter"));
    expect(invoke).toHaveBeenCalledWith({ type: "clipboard.read" });
  });

  it("does not yank a reader during streaming and offers a smooth jump to latest", async () => {
    const base = { runId: "run-1", studioSessionId: "session-1", runtimeId: "alpha", timestamp: "2026-08-24T00:00:00.000Z" };
    installBridge({ snapshot: state("alpha"), events: [{ ...base, id: "event-1", payload: { kind: "message.completed", messageId: "message-1", role: "assistant", text: "First answer" }, raw: null }] });
    render(<App />);
    await screen.findByText("First answer");

    const transcript = document.querySelector(".transcript") as HTMLDivElement;
    Object.defineProperties(transcript, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1_200 },
      scrollTop: { configurable: true, writable: true, value: 200 },
    });
    const scrollTo = vi.mocked(transcript.scrollTo);
    fireEvent.scroll(transcript);
    await screen.findByRole("button", { name: "Jump to latest response" });
    scrollTo.mockClear();

    act(() => pushStudioEvent?.({ type: "runtime.event", event: { ...base, id: "event-2", payload: { kind: "message.delta", messageId: "message-2", role: "assistant", text: "Streaming" }, raw: null } }));
    await screen.findByText("Streaming");
    expect(scrollTo).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Jump to latest response" }));
    expect(scrollTo).toHaveBeenCalledWith({ top: 1_200, behavior: "smooth" });
  });

  it("renders three columns with the Keep panel and toggles it with Ctrl+\\", async () => {
    const invoke = installBridge();
    render(<App />);
    expect(await screen.findByRole("complementary", { name: "The Keep" })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "\\", code: "Backslash", ctrlKey: true });
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "The Keep" })).not.toBeInTheDocument());
    expect(invoke).toHaveBeenCalledWith({ type: "settings.keepPanel", open: false });
  });

  it("shows no usage card, workspace card or marketing lines in the rail", async () => {
    installBridge();
    render(<App />);
    await screen.findByRole("button", { name: /Workspace:/ });
    const rail = document.querySelector(".session-rail") as HTMLElement;
    expect(within(rail).queryByText("Session usage")).not.toBeInTheDocument();
    expect(screen.queryByText("Session usage")).not.toBeInTheDocument();
    expect(screen.queryByText("Runtime-neutral core")).not.toBeInTheDocument();
    expect(within(rail).queryByText(/Agents come and go/)).not.toBeInTheDocument();
    expect(screen.queryByText("Client Kit controls ready")).not.toBeInTheDocument();
  });

  it("switches to a recent workspace from the repo picker", async () => {
    const invoke = installBridge({ snapshot: { ...state("alpha"), recentWorkspaces: ["C:\\work\\firekeep", "C:\\work\\site"], workspacePath: "C:\\work\\firekeep", keepPanelOpen: true } as never });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Workspace: firekeep" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /site/ }));
    expect(invoke).toHaveBeenCalledWith({ type: "workspace.set", path: "C:\\work\\site" });
  });

  it("restores Runtime Center actions from the Settings button", async () => {
    const invoke = installBridge({ snapshot: state("alpha") });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    const center = await screen.findByRole("dialog", { name: "Runtime Center" });
    expect(within(center).getByRole("button", { name: "Alpha is in use" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(within(center).getByRole("button", { name: "Use Beta as primary" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "primary.set", runtimeId: "beta" }));
    fireEvent.click(within(center).getByRole("button", { name: "Add Beta as reviewer" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "reviewer.add", runtimeId: "beta" }));
    fireEvent.click(within(center).getByRole("button", { name: "Disconnect Beta" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "runtime.logout", runtimeId: "beta" }));
    fireEvent.click(within(center).getByRole("button", { name: "Close Runtime Center" }));
    expect(screen.queryByRole("dialog", { name: "Runtime Center" })).not.toBeInTheDocument();
  });

  it("closes the repo menu with Escape without cancelling the active run", async () => {
    const invoke = installBridge({ snapshot: { ...state("alpha"), activeRunId: "run-1", workspacePath: "C:\\work\\firekeep", recentWorkspaces: ["C:\\work\\firekeep"] } });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Workspace: firekeep" }));
    expect(screen.getByRole("menu", { name: "Workspaces" })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu", { name: "Workspaces" })).not.toBeInTheDocument());
    expect(invoke).not.toHaveBeenCalledWith({ type: "run.cancel", runId: "run-1" });
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "run.cancel", runId: "run-1" }));
  });

  it("ignores Ctrl+Alt+\ and key repeat for the Keep toggle", async () => {
    const invoke = installBridge();
    render(<App />);
    await screen.findByRole("complementary", { name: "The Keep" });
    fireEvent.keyDown(window, { key: "\\", code: "Backslash", ctrlKey: true, altKey: true });
    fireEvent.keyDown(window, { key: "\\", code: "Backslash", ctrlKey: true, repeat: true });
    expect(invoke).not.toHaveBeenCalledWith(expect.objectContaining({ type: "settings.keepPanel" }));
    expect(screen.getByRole("complementary", { name: "The Keep" })).toBeInTheDocument();
  });

  it.each([
    ["/home/me/proj", "Workspace: proj"],
    ["C:\\work\\site\\","Workspace: site"],
    [null, "Workspace: Home"],
  ])("labels the repo picker for %s", async (path, label) => {
    installBridge({ snapshot: { ...state("alpha"), workspacePath: path } });
    render(<App />);
    expect(await screen.findByRole("button", { name: label })).toBeInTheDocument();
  });

  it("shows only Loading… on the launch screen", async () => {
    Object.defineProperty(window, "firekeepStudio", { configurable: true, value: { invoke: () => new Promise(() => undefined), subscribe: () => () => undefined } });
    render(<App />);
    expect(await screen.findByText(/Loading…/)).toBeInTheDocument();
    expect(screen.queryByText("THE KEEP IS WAKING")).not.toBeInTheDocument();
    expect(screen.queryByText("One calm console for every agent you trust.")).not.toBeInTheDocument();
  });

  it("toggles the Keep panel from the top bar button", async () => {
    const invoke = installBridge();
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Hide the Keep" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "settings.keepPanel", open: false }));
    fireEvent.click(await screen.findByRole("button", { name: "Show the Keep" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "settings.keepPanel", open: true }));
    expect(await screen.findByRole("complementary", { name: "The Keep" })).toBeInTheDocument();
  });

  it("replaces a generic completed Bash row with a concise description", async () => {
    const base = { runId: "run-1", studioSessionId: "session-1", runtimeId: "alpha", timestamp: "2026-08-24T00:00:00.000Z" };
    installBridge({ events: [
      { ...base, id: "event-1", payload: { kind: "tool.started", toolCallId: "tool-1", name: "Bash", input: { command: "npm test" } }, raw: null },
      { ...base, id: "event-2", payload: { kind: "tool.completed", toolCallId: "tool-1", name: "Bash", output: "passed" }, raw: null },
    ] });
    render(<App />);

    expect(await screen.findByText("Ran tests")).toBeInTheDocument();
    expect(screen.queryByText("completed")).not.toBeInTheDocument();
  });

  it("puts the final answer before a collapsed working log", async () => {
    const base = { runId: "run-1", studioSessionId: "session-1", runtimeId: "alpha", timestamp: "2026-08-24T00:00:00.000Z" };
    installBridge({ snapshot: state("alpha"), events: [
      { ...base, id: "event-1", payload: { kind: "tool.started", toolCallId: "tool-1", name: "Bash", input: { command: "npm test" } }, raw: null },
      { ...base, id: "event-2", payload: { kind: "tool.completed", toolCallId: "tool-1", name: "Bash", output: "passed" }, raw: null },
      { ...base, id: "event-3", payload: { kind: "message.completed", messageId: "message-1", role: "assistant", text: "Final answer" }, raw: null },
    ] });

    render(<App />);

    const answer = await screen.findByText("Final answer");
    const workLog = screen.getByText(/Work log · 1 step/i);
    expect(answer.compareDocumentPosition(workLog) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(workLog.closest("details")).not.toHaveAttribute("open");
  });

  it("opens and answers a provider Decision Board inside Studio", async () => {
    const url = "http://127.0.0.1:43123/board/board-1";
    const base = { runId: "run-1", studioSessionId: "session-1", runtimeId: "alpha", timestamp: "2026-08-24T00:00:00.000Z" };
    const invoke = installBridge({
      events: [{ ...base, id: "event-1", payload: { kind: "tool.completed", toolCallId: "tool-1", name: "firekeep-decision/decision_board", output: { status: "pending", board_url: url } }, raw: null }],
      decisionBoard: {
        url,
        spec: {
          boardId: "board-1",
          context: "Choose how to release the visual layer.",
          degraded: false,
          questions: [{ id: "q0", text: "**Ship** the native board?", knowledgeFound: true, evidence: [{ source: "team memory", snippet: "The browser handoff interrupted flow." }], suggestedAnswers: ["Ship it"], suggestedActions: ["Run package smoke"] }],
          boardEmbeds: [],
          embedsByQuestion: {},
        },
        embeds: [],
      },
    });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Open board" }));
    expect(await screen.findByRole("form", { name: "Decision Board" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Ship it" }));
    fireEvent.click(screen.getByLabelText(/Run package smoke/));
    fireEvent.click(screen.getByRole("button", { name: "Send answers" }));

    await waitFor(() => expect(invoke).toHaveBeenCalledWith({
      type: "decision.submit",
      url,
      answers: { q0: { answer: "Ship it", actions_confirmed: ["Run package smoke"], skipped: false } },
    }));
    await waitFor(() => expect(screen.queryByRole("form", { name: "Decision Board" })).not.toBeInTheDocument());
  });

  it("opens an authenticated main-process Decision Board push without waiting for tool completion", async () => {
    const board: DecisionBoardDocument = {
      url: "http://127.0.0.1:43123/board/pushed-board",
      spec: { boardId: "pushed-board", degraded: false, questions: [{ id: "q0", text: "Choose now?", knowledgeFound: false, evidence: [], suggestedAnswers: [], suggestedActions: [] }], boardEmbeds: [], embedsByQuestion: {} },
      embeds: [],
    };
    const invoke = installBridge({ pushEvents: [{ type: "decision.available", board }] });

    render(<App />);

    expect(await screen.findByRole("form", { name: "Decision Board" })).toBeInTheDocument();
    expect(invoke).not.toHaveBeenCalledWith({ type: "decision.load", url: board.url });
  });
});
