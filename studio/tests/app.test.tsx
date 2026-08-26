// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StudioSnapshot } from "../src/core/studio-service.js";
import type { StudioSessionSummary } from "../src/core/session-store.js";
import type { RuntimeDescriptor, RuntimeEvent, RuntimeModel } from "../src/core/runtime.js";
import { App } from "../src/renderer/src/App.js";
import type { StudioAction, StudioActionResult, StudioBridge, StudioPushEvent, StudioUpdateState, VoiceInputOutcome } from "../src/shared/ipc.js";
import type { DecisionBoardDocument } from "../src/shared/decision-board.js";

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
    voiceEnabled: false,
    theme: "system",
    activeRunId: null,
    eventCount: 0,
    usage: { tokens: 0, freshTokens: 0, cachedTokens: 0, costUsd: 0, runs: 0, totalRuns: 0, measuredRuns: 0, byRuntime: {} },
    mission: null,
  };
}

let pushStudioEvent: ((event: StudioPushEvent) => void) | null = null;

function installBridge(options: { readonly snapshot?: StudioSnapshot; readonly sessions?: readonly StudioSessionSummary[]; readonly models?: readonly RuntimeModel[] | (() => readonly RuntimeModel[]); readonly dashboardAvailable?: boolean; readonly events?: readonly RuntimeEvent[]; readonly decisionBoard?: DecisionBoardDocument; readonly pushEvents?: readonly StudioPushEvent[]; readonly clipboardText?: string; readonly voiceInput?: VoiceInputOutcome | (() => Promise<VoiceInputOutcome>); readonly update?: StudioUpdateState; readonly updateResult?: StudioUpdateState } = {}): ReturnType<typeof vi.fn<(action: StudioAction) => Promise<StudioActionResult>>> {
  let snapshot = options.snapshot ?? state();
  let sessions = [...(options.sessions ?? [{ id: "session-1", name: "UI test", color: "ember" as const, createdAt: "2026-08-24T00:00:00.000Z", updatedAt: "2026-08-24T00:00:00.000Z", eventCount: 0, nativeSessionIds: {} }])];
  const invoke = vi.fn(async (action: StudioAction): Promise<StudioActionResult> => {
    if (action.type === "bootstrap") return { type: "bootstrap", appName: "Firekeep Studio", version: "0.4.0", dashboardAvailable: options.dashboardAvailable ?? true, update: options.update ?? { phase: "idle", currentVersion: "0.4.0", availableVersion: null, progressPercent: null, automatic: true, detail: "Studio checks for verified updates automatically." }, snapshot, runtimes, events: options.events ?? [], sessions };
    if (action.type === "runtime.probe") return { type: "diagnostics", items: runtimes.map((runtime) => ({ runtimeId: runtime.id, connection: { state: "ready", detail: "Ready" }, auth: { state: "connected", label: "Connected" } })) };
    if (action.type === "runtime.models") return { type: "models", runtimeId: action.runtimeId, items: typeof options.models === "function" ? options.models() : options.models ?? [] };
    if (action.type === "command.complete") return { type: "completions", items: action.input === "/" ? [{ value: "/doctor", label: "/doctor", description: "Check runtimes" }] : [] };
    if (action.type === "decision.load" && options.decisionBoard) return { type: "decision", board: options.decisionBoard };
    if (action.type === "decision.submit") return { type: "decision-submitted", url: action.url };
    if (action.type === "clipboard.read") return { type: "clipboard-read", text: options.clipboardText ?? "" };
    if (action.type === "clipboard.write") return { type: "clipboard-written" };
    if (action.type === "voice.input.start") {
      const result = typeof options.voiceInput === "function" ? await options.voiceInput() : options.voiceInput ?? { state: "unavailable", text: "", detail: "Voice input unavailable" };
      return { type: "voice-input", ...result };
    }
    if (action.type === "voice.input.stop") return { type: "voice-input", state: "cancelled", text: "", detail: "Voice input stopped." };
    if (action.type === "update.check" || action.type === "update.install") return { type: "update", state: options.updateResult ?? options.update ?? { phase: "current", currentVersion: "0.4.0", availableVersion: null, progressPercent: null, automatic: true, detail: "Firekeep Studio 0.4.0 is current." } };
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
    expect(await screen.findByText("0 fresh tokens")).toBeInTheDocument();
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

  it("shows slash completion and appends typed local voice input", async () => {
    const invoke = installBridge({ voiceInput: { state: "complete", text: "dictated text", detail: "local" } });
    render(<App />);
    await screen.findByRole("heading", { name: "Agents come and go. The Keep stays." });
    const composer = screen.getByRole("textbox");
    fireEvent.change(composer, { target: { value: "/" } });
    expect(await screen.findByRole("option", { name: /doctor/i })).toBeInTheDocument();
    fireEvent.change(composer, { target: { value: "Draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Start voice input" }));

    await waitFor(() => expect(composer).toHaveValue("Draft dictated text"));
    expect(invoke).toHaveBeenCalledWith({ type: "voice.input.start", language: expect.any(String) });
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

  it("shows an actionable voice error when the local recognizer is unavailable", async () => {
    installBridge({ voiceInput: { state: "unavailable", text: "", detail: "Install a Windows speech language to use voice input." } });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Start voice input" }));

    expect(await screen.findByText(/Install a Windows speech language/i)).toBeInTheDocument();
  });

  it("lets a second microphone click cancel the active local listener", async () => {
    let finish: ((result: VoiceInputOutcome) => void) | undefined;
    const invoke = installBridge({ voiceInput: () => new Promise<VoiceInputOutcome>((resolve) => { finish = resolve; }) });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Start voice input" }));
    expect(await screen.findByText(/Listening · speak naturally/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop voice input" }));

    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "voice.input.stop" }));
    finish?.({ state: "cancelled", text: "", detail: "Voice input stopped." });
    expect(await screen.findByRole("button", { name: "Start voice input" })).toBeInTheDocument();
  });

  it("makes the mission harness discoverable without exposing a process bridge", async () => {
    installBridge();
    render(<App />);
    await screen.findByText("Give an agent a goal and verify the result.");

    fireEvent.click(screen.getByRole("button", { name: "Start a mission" }));

    expect(screen.getByRole("textbox")).toHaveValue('/mission new "');
  });

  it("shows active work prominently on the Firekeep logo", async () => {
    installBridge({ snapshot: { ...state("alpha"), activeRunId: "run-1" } });

    render(<App />);

    expect(await screen.findByLabelText("Agent working")).toHaveClass("brand-icon");
  });

  it("keeps usage in the session rail and moves runtime management out of the inspector", async () => {
    installBridge();
    render(<App />);

    const usage = await screen.findByRole("region", { name: "Session usage" });
    expect(usage.closest(".session-rail")).toBeInTheDocument();
    expect(usage.closest(".inspector")).not.toBeInTheDocument();

    const inspector = document.querySelector(".inspector");
    expect(inspector).not.toBeNull();
    expect(within(inspector as HTMLElement).queryByText("Agent runtimes")).not.toBeInTheDocument();

    const center = await openRuntimeCenter();
    expect(center).toHaveTextContent("Alpha");
    expect(center).toHaveTextContent("Beta");
    expect(center.closest(".inspector")).not.toBeInTheDocument();
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

  it("shows a clean in-use runtime state and makes the full inspector easy to hide", async () => {
    const invoke = installBridge({ snapshot: state("alpha") });
    render(<App />);

    const center = await openRuntimeCenter();
    const selected = within(center).getByRole("button", { name: "Alpha is in use" });
    expect(selected).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(within(center).getByRole("button", { name: "Use Beta as primary" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "primary.set", runtimeId: "beta" }));

    fireEvent.click(within(center).getByRole("button", { name: "Add Beta as reviewer" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "reviewer.add", runtimeId: "beta" }));

    fireEvent.click(within(center).getByRole("button", { name: "Disconnect Beta" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "runtime.logout", runtimeId: "beta" }));

    fireEvent.click(within(center).getByRole("button", { name: "Close Runtime Center" }));

    fireEvent.click(screen.getByRole("button", { name: "Hide inspector" }));
    expect(document.querySelector(".inspector")).not.toBeInTheDocument();
    const hiddenCenter = await openRuntimeCenter();
    expect(within(hiddenCenter).getByRole("button", { name: "Beta is in use" })).toBeInTheDocument();
    fireEvent.click(within(hiddenCenter).getByRole("button", { name: "Close Runtime Center" }));
    fireEvent.click(screen.getByRole("button", { name: "Show inspector" }));
    expect(document.querySelector(".inspector")).toBeInTheDocument();
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

  it("separates fresh usage from cached context and opens the configured dashboard", async () => {
    const invoke = installBridge({ snapshot: { ...state("alpha"), usage: { tokens: 5_387_632, freshTokens: 246_387, cachedTokens: 5_141_245, costUsd: 0, runs: 3, totalRuns: 4, measuredRuns: 3, byRuntime: { claude: { tokens: 5_387_632, freshTokens: 246_387, cachedTokens: 5_141_245, costUsd: 0, runs: 3 } } } } });
    render(<App />);

    const usage = await screen.findByRole("region", { name: "Session usage" });
    expect(usage).toHaveTextContent("246,387 fresh tokens");
    expect(usage).toHaveTextContent("5,141,245 cached");
    expect(usage).toHaveTextContent("5,387,632 total traffic");

    fireEvent.click(screen.getByRole("button", { name: "Open Firekeep dashboard" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "dashboard.open" }));
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

  it("opens a selectable agent grid and sends the composer to the active pane", async () => {
    const invoke = installBridge({ snapshot: { ...state("alpha"), reviewerRuntimeIds: ["beta"] } });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Open agent grid" }));
    expect(screen.getByLabelText("Agent grid")).toHaveAttribute("data-count", "2");
    expect(screen.getByRole("region", { name: "Alpha agent pane" })).toBeInTheDocument();
    const betaPane = screen.getByRole("region", { name: "Beta agent pane" });
    fireEvent.click(betaPane);

    const composer = screen.getByRole("textbox");
    expect(composer).toHaveAttribute("placeholder", expect.stringMatching(/Message Beta pane/i));
    fireEvent.change(composer, { target: { value: "ask beta directly" } });
    fireEvent.click(screen.getByTitle("Send"));

    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "message.sendTo", runtimeId: "beta", text: "ask beta directly" }));
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
