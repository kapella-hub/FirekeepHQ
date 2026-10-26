import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowDownToLine,
  CheckCircle2,
  ChevronDown,
  Cpu,
  ExternalLink,
  FileDiff,
  KeyRound,
  Palette,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";
import type { CommandCompletion } from "../../core/slash-commands";
import type { SessionColor, StudioSessionSummary } from "../../core/session-store";
import type { RuntimeDiagnostic, StudioSnapshot } from "../../core/studio-service";
import type { LoginMethod, RuntimeDescriptor, RuntimeEffort, RuntimeEvent, RuntimeModel, RuntimeUsage } from "../../core/runtime";
import type { BootstrapResult, StudioAction, StudioActionResult, StudioUpdateState } from "../../shared/ipc";
import type { DecisionAnswers, DecisionBoardDocument, DecisionEmbed, DecisionQuestion } from "../../shared/decision-board";
import { findDecisionBoardUrl } from "../../shared/decision-board";
import type { KeepHighlights, KeepStatus } from "../../shared/keep";
import { StartCard } from "./start/StartCard.js";
import { FirekeepMark } from "./FirekeepMark.js";
import { StudioShell } from "./layout/StudioShell.js";
import { TopBar } from "./layout/TopBar.js";
import { ignore } from "./util.js";
import { CopyButton } from "./CopyButton.js";
import { SettingsSheet, type SettingsSection } from "./settings/SettingsSheet.js";
import { KeepPanel } from "./keep/KeepPanel.js";
import { CommandPalette } from "./palette/CommandPalette.js";
import { SessionRail } from "./rail/SessionRail.js";
import { sessionAccentStyle } from "./rail/session-style.js";
import { RichMarkdown } from "./RichMarkdown.js";
import { RenderBoundary } from "./RenderBoundary.js";
import { AgentChip } from "./composer/AgentChip.js";
import { DoneWhenChip } from "./composer/DoneWhenChip.js";
import { Composer } from "./composer/Composer.js";
import { coalesceRuntimeEvents } from "./runtime-event-buffer.js";
import { CommandResultCard, type CommandCard } from "./conversation/CommandResultCard.js";
import { KeepReceiptCard } from "./conversation/KeepReceiptCard.js";
import { SecondOpinionChooser } from "./conversation/SecondOpinion.js";
import { MissionStepper } from "./conversation/MissionStepper.js";
import { buildTimeline, groupTimeline, type TimelineItem, type TimelineRun } from "./timeline";

interface ConnectDialog { readonly runtimeId: string; readonly method: LoginMethod }
interface ModelRefresh { readonly runtimeId: string; readonly state: "loading" | "success" | "error"; readonly message: string }

export function App(): React.JSX.Element {
  const [bootstrap, setBootstrap] = useState<BootstrapResult | null>(null);
  const [updateState, setUpdateState] = useState<StudioUpdateState | null>(null);
  const [snapshot, setSnapshot] = useState<StudioSnapshot | null>(null);
  const [events, setEvents] = useState<RuntimeEvent[]>([]);
  const [sessions, setSessions] = useState<StudioSessionSummary[]>([]);
  const [diagnostics, setDiagnostics] = useState<Record<string, RuntimeDiagnostic>>({});
  const [models, setModels] = useState<Record<string, readonly RuntimeModel[]>>({});
  const [modelRefresh, setModelRefresh] = useState<ModelRefresh | null>(null);
  const [composer, setComposer] = useState("");
  const [doneChecks, setDoneChecks] = useState<readonly string[]>([]);
  const [completions, setCompletions] = useState<readonly CommandCompletion[]>([]);
  const [completionIndex, setCompletionIndex] = useState(0);
  const [commandCards, setCommandCards] = useState<CommandCard[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [keepStatus, setKeepStatus] = useState<KeepStatus | null>(null);
  const [highlights, setHighlights] = useState<KeepHighlights | null>(null);
  const [settingsSection, setSettingsSection] = useState<SettingsSection | null>(null);
  const [connectDialog, setConnectDialog] = useState<ConnectDialog | null>(null);
  const [secret, setSecret] = useState("");
  const [decisionBoard, setDecisionBoard] = useState<DecisionBoardDocument | null>(null);
  const [decisionLoading, setDecisionLoading] = useState<string | null>(null);
  const [followingTail, setFollowingTail] = useState(true);
  const [sessionEditorId, setSessionEditorId] = useState<string | null>(null);
  const [sessionSaving, setSessionSaving] = useState(false);
  // The last section used this app session, and the element to hand focus back to when the sheet closes.
  const lastSettingsSection = useRef<SettingsSection>("agents");
  const settingsOpener = useRef<HTMLElement | null>(null);
  // The last kit command run from Settings > Keep connection; it survives tab switches and closing the sheet.
  const [kitCard, setKitCard] = useState<CommandCard | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteCompletions, setPaletteCompletions] = useState<readonly CommandCompletion[]>([]);
  const [keepSearchRequest, setKeepSearchRequest] = useState<{ readonly query: string; readonly nonce: number } | null>(null);
  const paletteOpener = useRef<HTMLElement | null>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const followingTailRef = useRef(true);
  const modelRequestRef = useRef<Record<string, number>>({});
  // Bumped by every pushed keep-status; a poll/retry reply older than the latest push is dropped.
  const keepSeq = useRef(0);
  const surfacedBoardsRef = useRef(new Set<string>());
  const seenEventIdsRef = useRef(new Set<string>());
  const pendingEventsRef = useRef<RuntimeEvent[]>([]);
  const eventFrameRef = useRef<number | null>(null);

  const invoke = useCallback(async (action: StudioAction): Promise<StudioActionResult> => {
    try {
      setError(null);
      const result = await window.firekeepStudio.invoke(action);
      if (result.type === "error") throw new Error(result.message);
      return result;
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : String(caught);
      setError(message.replace(/^Error invoking remote method '[^']+':\s*/i, ""));
      throw caught;
    }
  }, []);

  /** Best-effort test-command detection for the Done-when chip; failures are silent (the field just stays empty). */
  const detectTestCommand = useCallback(async (): Promise<string | null> => {
    try {
      const result = await window.firekeepStudio.invoke({ type: "workspace.detectTest" });
      return result.type === "test-command" ? result.command : null;
    } catch { return null; }
  }, []);

  const replaceEvents = useCallback((next: readonly RuntimeEvent[]): void => {
    if (eventFrameRef.current !== null) window.cancelAnimationFrame(eventFrameRef.current);
    eventFrameRef.current = null;
    pendingEventsRef.current = [];
    seenEventIdsRef.current = new Set(next.map((event) => event.id));
    setEvents(coalesceRuntimeEvents([], next));
  }, []);

  const enqueueEvent = useCallback((event: RuntimeEvent): boolean => {
    if (seenEventIdsRef.current.has(event.id)) return false;
    seenEventIdsRef.current.add(event.id);
    pendingEventsRef.current.push(event);
    if (eventFrameRef.current === null) {
      eventFrameRef.current = window.requestAnimationFrame(() => {
        eventFrameRef.current = null;
        const batch = pendingEventsRef.current;
        pendingEventsRef.current = [];
        if (batch.length) setEvents((current) => coalesceRuntimeEvents(current, batch));
      });
    }
    return true;
  }, []);

  const applyResult = useCallback((result: StudioActionResult): void => {
    if (result.type === "state") {
      setSnapshot(result.snapshot);
      if (result.sessions) setSessions([...result.sessions]);
      if (result.events) replaceEvents(result.events);
    } else if (result.type === "command") {
      setSnapshot(result.snapshot);
      setSessions([...result.sessions]);
      replaceEvents(result.events);
    } else if (result.type === "update") {
      setUpdateState(result.state);
    }
  }, [replaceEvents]);

  const openDecisionBoard = useCallback(async (url: string): Promise<void> => {
    setDecisionLoading(url);
    try {
      const result = await invoke({ type: "decision.load", url });
      if (result.type !== "decision") throw new Error("Decision Board returned an unexpected response");
      setDecisionBoard(result.board);
    } finally {
      setDecisionLoading((current) => current === url ? null : current);
    }
  }, [invoke]);

  const refreshDiagnostics = useCallback(async (): Promise<void> => {
    try {
      const result = await invoke({ type: "runtime.probe" });
      if (result.type === "diagnostics") setDiagnostics(Object.fromEntries(result.items.map((item) => [item.runtimeId, item])));
    } catch { /* invoke surfaced the actionable error. */ }
  }, [invoke]);

  const loadModels = useCallback(async (runtimeId: string): Promise<void> => {
    const request = (modelRequestRef.current[runtimeId] ?? 0) + 1;
    modelRequestRef.current[runtimeId] = request;
    setModelRefresh({ runtimeId, state: "loading", message: "Refreshing live models…" });
    try {
      const result = await invoke({ type: "runtime.models", runtimeId });
      if (modelRequestRef.current[runtimeId] !== request) return;
      if (result.type !== "models") throw new Error("runtime returned an unexpected model response");
      setModels((current) => ({ ...current, [runtimeId]: result.items }));
      setModelRefresh({ runtimeId, state: "success", message: result.items.length
        ? `${result.items.length} live model${result.items.length === 1 ? "" : "s"} updated`
        : "Provider reports no live models" });
    } catch {
      if (modelRequestRef.current[runtimeId] === request) setModelRefresh({ runtimeId, state: "error", message: "Live model refresh failed" });
    }
  }, [invoke]);


  useEffect(() => {
    let active = true;
    const unsubscribe = window.firekeepStudio.subscribe((event) => {
      if (!active) return;
      if (event.type === "runtime.event") {
        if (!enqueueEvent(event.event)) return;
        const payload = event.event.payload;
        if (payload.kind === "tool.completed") {
          const boardUrl = findDecisionBoardUrl(payload.output);
          if (boardUrl && !surfacedBoardsRef.current.has(boardUrl)) {
            surfacedBoardsRef.current.add(boardUrl);
            ignore(openDecisionBoard(boardUrl));
          }
        }
      } else if (event.type === "snapshot") setSnapshot(event.snapshot);
      else if (event.type === "update") setUpdateState(event.state);
      else if (event.type === "keep-status") { keepSeq.current += 1; setKeepStatus(event.status); }
      else if (event.type === "decision.available") {
        surfacedBoardsRef.current.add(event.board.url);
        setDecisionBoard(event.board);
      } else if (event.type === "sessions") setSessions([...event.sessions]);
    });
    ignore(invoke({ type: "bootstrap" }).then((result) => {
      if (!active || result.type !== "bootstrap") return;
      setBootstrap(result);
      setUpdateState(result.update);
      setSnapshot(result.snapshot);
      replaceEvents(result.events);
      setSessions([...result.sessions]);
      ignore(refreshDiagnostics());
      const seq = keepSeq.current;
      ignore(invoke({ type: "keep.status" }).then((status) => { if (active && seq === keepSeq.current && status.type === "keep-status") setKeepStatus(status.status); }));
      if (result.snapshot.primaryRuntimeId) ignore(loadModels(result.snapshot.primaryRuntimeId));
    }));
    return () => {
      active = false;
      unsubscribe();
      if (eventFrameRef.current !== null) window.cancelAnimationFrame(eventFrameRef.current);
      eventFrameRef.current = null;
      pendingEventsRef.current = [];
    };
  }, [enqueueEvent, invoke, loadModels, openDecisionBoard, refreshDiagnostics, replaceEvents]);

  useEffect(() => {
    document.documentElement.dataset.theme = snapshot?.theme ?? "system";
  }, [snapshot?.theme]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (!composer.trimStart().startsWith("/")) { setCompletions([]); return; }
      const query = composer;
      ignore(invoke({ type: "command.complete", input: query }).then((result) => {
        if (result.type === "completions" && composerRef.current?.value === query) {
          setCompletions(result.items);
          setCompletionIndex(0);
        }
      }));
    }, 70);
    return () => window.clearTimeout(timer);
  }, [composer, invoke]);

  useEffect(() => {
    followingTailRef.current = true;
    setFollowingTail(true);
  }, [snapshot?.activeSessionId]);

  useEffect(() => {
    const transcript = transcriptRef.current;
    if (!transcript || !followingTailRef.current) return;
    transcript.scrollTo({ top: transcript.scrollHeight, behavior: "auto" });
  }, [events, commandCards.length, snapshot?.activeSessionId]);

  const hasPrimaryResponse = events.some((event) => event.payload.kind === "message.completed" && event.payload.role === "assistant");

  const runReview = useCallback(async (): Promise<void> => {
    if (busy || !snapshot?.reviewerRuntimeIds.length || !hasPrimaryResponse) return;
    setBusy(true);
    try { applyResult(await invoke({ type: "review.run" })); }
    catch { /* invoke surfaced the actionable error. */ }
    finally { setBusy(false); }
  }, [applyResult, busy, hasPrimaryResponse, invoke, snapshot?.reviewerRuntimeIds.length]);

  const runSecondOpinion = useCallback(async (runtimeId: string, focus: string): Promise<void> => {
    if (busy) return;
    setBusy(true);
    try { applyResult(await invoke({ type: "review.run", runtimeId, ...(focus ? { focus } : {}) })); }
    catch { /* invoke surfaced the actionable error. */ }
    finally { setBusy(false); }
  }, [applyResult, busy, invoke]);

  const openSettings = useCallback((section?: SettingsSection): void => {
    if (settingsSection === null) settingsOpener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const next = section ?? lastSettingsSection.current;
    lastSettingsSection.current = next;
    setSettingsSection(next);
    ignore(refreshDiagnostics());
  }, [refreshDiagnostics, settingsSection]);
  const chooseSettingsSection = useCallback((section: SettingsSection): void => { lastSettingsSection.current = section; setSettingsSection(section); }, []);
  const closeSettings = useCallback((): void => {
    setSettingsSection(null);
    const opener = settingsOpener.current;
    settingsOpener.current = null;
    if (opener?.isConnected) opener.focus();
    else composerRef.current?.focus();
  }, []);
  /** Runs a kit slash command for Settings > Keep connection; the card renders inside the sheet, not the transcript. */
  const runKitCommand = useCallback(async (input: string): Promise<void> => {
    try {
      // Straight to the bridge: a failure becomes a card in the sheet, not the global banner hidden behind it.
      const result = await window.firekeepStudio.invoke({ type: "command.execute", input });
      if (result.type === "error") throw new Error(result.message);
      applyResult(result);
      if (result.type === "command") setKitCard({ id: crypto.randomUUID(), input, result: result.result });
    } catch (caught) {
      const message = (caught instanceof Error ? caught.message : String(caught)).replace(/^Error invoking remote method '[^']+':\s*/i, "");
      setKitCard({ id: crypto.randomUUID(), input, result: { kind: "notice", title: "Command failed", body: message, tone: "danger" } });
    }
  }, [applyResult]);

  const openPalette = useCallback((): void => {
    paletteOpener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setPaletteOpen(true);
    ignore(invoke({ type: "command.complete", input: "/" }).then((result) => {
      if (result.type === "completions") setPaletteCompletions(result.items);
    }));
  }, [invoke]);
  const closePalette = useCallback((restoreFocus: boolean): void => {
    setPaletteOpen(false);
    const opener = paletteOpener.current;
    paletteOpener.current = null;
    if (!restoreFocus) return;
    if (opener?.isConnected) opener.focus();
    else composerRef.current?.focus();
  }, []);
  const searchKeepFromPalette = useCallback((query: string): void => {
    closePalette(false);
    // Only a Keep that is connected (or still starting) can take the search; otherwise the panel's state card is the feedback.
    if (keepStatus?.state === "connected" || keepStatus?.state === "starting") setKeepSearchRequest((previous) => ({ query, nonce: (previous?.nonce ?? 0) + 1 }));
    if (!(snapshot?.keepPanelOpen ?? true)) ignore(invoke({ type: "settings.keepPanel", open: true }).then(applyResult));
  }, [applyResult, closePalette, invoke, keepStatus, snapshot?.keepPanelOpen]);
  const clearKeepSearchRequest = useCallback(() => setKeepSearchRequest(null), []);

  useEffect(() => {
    const keyboard = (event: KeyboardEvent): void => {
      // While Settings or the palette is open it owns the keyboard; the global shortcuts must not fire behind it.
      if ((settingsSection !== null || paletteOpen) && (event.ctrlKey || event.metaKey)) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (event.repeat) return;
        openPalette();
      } else if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === "r") {
        event.preventDefault();
        if (!busy) ignore(runReview());
      } else if ((event.ctrlKey || event.metaKey) && !event.altKey && event.code === "Backslash") {
        event.preventDefault();
        if (event.repeat) return;
        const open = !(snapshot?.keepPanelOpen ?? true);
        ignore(invoke({ type: "settings.keepPanel", open }).then(applyResult));
      } else if (event.key === "Escape") {
        if (connectDialog) setConnectDialog(null);
        else if (sessionEditorId) setSessionEditorId(null);
        else if (snapshot?.activeRunId) ignore(invoke({ type: "run.cancel", runId: snapshot.activeRunId }));
      }
    };
    window.addEventListener("keydown", keyboard);
    return () => window.removeEventListener("keydown", keyboard);
  }, [applyResult, busy, connectDialog, invoke, openPalette, paletteOpen, runReview, sessionEditorId, settingsSection, snapshot?.activeRunId, snapshot?.keepPanelOpen]);

  const submit = async (): Promise<void> => {
    const input = composer.trim();
    if (!input || busy) return;
    if (doneChecks.length > 0 && !input.startsWith("/")) {
      // Checks set: Send creates and runs a mission (the main process still asks for confirmation).
      setComposer("");
      setCompletions([]);
      setBusy(true);
      try {
        applyResult(await invoke({ type: "mission.quick", goal: input, checks: [...doneChecks] }));
        setDoneChecks([]);
      } catch {
        setComposer(input);
      } finally {
        setBusy(false);
        composerRef.current?.focus();
      }
      return;
    }
    setComposer("");
    setCompletions([]);
    setBusy(true);
    try {
      if (input.startsWith("/")) {
        const result = await invoke({ type: "command.execute", input });
        applyResult(result);
        if (result.type === "command") setCommandCards((current) => [...current, { id: crypto.randomUUID(), input, result: result.result }]);
      } else applyResult(await invoke({ type: "message.send", text: input }));
    } catch {
      if (!input.startsWith("/")) setComposer(input);
    } finally {
      setBusy(false);
      composerRef.current?.focus();
    }
  };

  const choosePrimary = async (runtimeId: string): Promise<void> => {
    try {
      applyResult(await invoke({ type: "primary.set", runtimeId }));
      ignore(loadModels(runtimeId));
    } catch { /* invoke surfaced the actionable error. */ }
  };

  const pasteClipboard = async (): Promise<void> => {
    const textarea = composerRef.current;
    const start = textarea?.selectionStart ?? composer.length;
    const end = textarea?.selectionEnd ?? start;
    const result = await invoke({ type: "clipboard.read" });
    if (result.type !== "clipboard-read") throw new Error("clipboard returned an unexpected response");
    if (!result.text) { textarea?.focus(); return; }
    setComposer((value) => `${value.slice(0, start)}${result.text}${value.slice(end)}`);
    window.requestAnimationFrame(() => {
      const caret = start + result.text.length;
      composerRef.current?.focus();
      composerRef.current?.setSelectionRange(caret, caret);
    });
  };

  const updateTranscriptFollow = (): void => {
    const transcript = transcriptRef.current;
    if (!transcript) return;
    const nearTail = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight <= 80;
    followingTailRef.current = nearTail;
    setFollowingTail(nearTail);
  };

  const jumpToLatest = (): void => {
    const transcript = transcriptRef.current;
    if (!transcript) return;
    followingTailRef.current = true;
    setFollowingTail(true);
    transcript.scrollTo({ top: transcript.scrollHeight, behavior: "smooth" });
  };

  const chooseWorkspace = async (): Promise<void> => {
    if (busy) return;
    setBusy(true);
    try { applyResult(await invoke({ type: "workspace.choose" })); }
    catch { /* invoke surfaced the actionable error. */ }
    finally { setBusy(false); }
  };

  const controlMission = async (action: StudioAction): Promise<void> => {
    const cancelling = action.type === "mission.cancel";
    if (busy && !cancelling) return;
    if (!cancelling) setBusy(true);
    try { applyResult(await invoke(action)); }
    catch { /* invoke surfaced the actionable error. */ }
    finally { if (!cancelling) setBusy(false); }
  };

  const openConnect = (runtimeId: string): void => {
    const method = bootstrap?.runtimes.find((runtime) => runtime.id === runtimeId)?.loginMethods?.[0] ?? "browser";
    setSecret("");
    setConnectDialog({ runtimeId, method });
  };

  const connect = async (): Promise<void> => {
    if (!connectDialog) return;
    try {
      const result = await invoke({ type: "runtime.login", runtimeId: connectDialog.runtimeId, method: connectDialog.method, ...(secret ? { secret } : {}) });
      setSecret("");
      setConnectDialog(null);
      if (result.type === "login") setCommandCards((current) => [...current, { id: crypto.randomUUID(), input: `/connect ${connectDialog.runtimeId}`, result: { kind: "notice", title: "Account connection", body: result.result.message, tone: "success" } }]);
      ignore(refreshDiagnostics());
    } catch { /* Keep the dialog open so the user can retry. */ }
  };

  const saveSession = async (sessionId: string, name: string, color: SessionColor): Promise<void> => {
    setSessionSaving(true);
    try {
      applyResult(await invoke({ type: "session.update", sessionId, name, color }));
      setSessionEditorId((current) => current === sessionId ? null : current);
    } catch { /* invoke surfaced the actionable error. */ }
    finally { setSessionSaving(false); }
  };
  const updateStudio = async (): Promise<void> => {
    if (!updateState || updateState.phase === "disabled" || updateState.phase === "checking" || updateState.phase === "downloading") return;
    try {
      applyResult(await invoke({ type: updateState.phase === "ready" || updateState.phase === "available" ? "update.install" : "update.check" }));
    } catch { /* invoke surfaced the actionable error. */ }
  };
  const timeline = useMemo(() => buildTimeline(events), [events]);
  const timelineRuns = useMemo(() => groupTimeline(timeline), [timeline]);
  const keepConnected = keepStatus?.state === "connected";
  const startVisible = timeline.length === 0 && commandCards.length === 0;
  // Panel reads fail inline; they must not raise or clear the global error banner the way `invoke` does.
  const keepRead = useCallback(async (action: StudioAction): Promise<StudioActionResult> => {
    const result = await window.firekeepStudio.invoke(action);
    if (result.type === "error") throw new Error(result.message);
    return result;
  }, []);
  useEffect(() => {
    if (!keepConnected) { setHighlights(null); return; }
    if (!startVisible) return;
    let active = true;
    // Highlights are a background read: a failure leaves the Start card without them, never a global banner.
    ignore(keepRead({ type: "keep.highlights" }).then((result) => { if (active && result.type === "keep-highlights") setHighlights(result.highlights); }));
    return () => { active = false; };
  }, [keepRead, keepConnected, startVisible]);
  const runGroups = useMemo(() => {
    const groups: Array<{ run: TimelineRun; reviews: TimelineRun[] }> = [];
    for (const run of timelineRuns) {
      const reviewOnly = run.messages.length > 0 && run.messages.every((message) => message.role === "reviewer");
      const last = groups.at(-1);
      if (reviewOnly && last) last.reviews.push(run);
      else groups.push({ run, reviews: [] });
    }
    return groups;
  }, [timelineRuns]);
  const newestReceiptRunId = useMemo(() => [...timelineRuns].reverse().find((run) => run.receipt)?.runId ?? null, [timelineRuns]);
  const keepReceipts = useMemo(() => [...timelineRuns].reverse().flatMap((run) => run.receipt ? [run.receipt] : []), [timelineRuns]);
  const retryKeep = useCallback(() => {
    const seq = keepSeq.current;
    // Retry is a user action, so a failure surfaces in the global banner rather than vanishing.
    ignore(invoke({ type: "keep.retry" }).then((result) => { if (seq === keepSeq.current && result.type === "keep-status") setKeepStatus(result.status); }));
  }, [invoke]);
  const insertQuote = useCallback((text: string) => setComposer((value) => `${value}${value && !value.endsWith("\n") ? "\n" : ""}${text}`), []);
  const lastAnswerId = useMemo(() => {
    for (let index = timeline.length - 1; index >= 0; index -= 1) {
      const item = timeline[index] as TimelineItem;
      if (item.kind === "message" && item.role === "assistant" && item.complete) return item.id;
    }
    return null;
  }, [timeline]);
  const primary = bootstrap?.runtimes.find((runtime) => runtime.id === snapshot?.primaryRuntimeId);
  const currentSession = sessions.find((session) => session.id === snapshot?.activeSessionId);
  const primaryRuntimeId = snapshot?.primaryRuntimeId ?? null;
  const primaryModels = primaryRuntimeId ? models[primaryRuntimeId] ?? [] : [];
  const selectedModel = primaryRuntimeId
    ? primaryModels.find((model) => model.id === snapshot?.selectedModels[primaryRuntimeId]) ?? primaryModels.find((model) => model.isDefault)
    : undefined;
  const liveEfforts = selectedModel?.efforts ?? [...new Set(primaryModels.flatMap((model) => model.efforts ?? []))];
  const selectedEffort = primaryRuntimeId && liveEfforts.includes(snapshot?.selectedEfforts[primaryRuntimeId] as RuntimeEffort)
    ? snapshot?.selectedEfforts[primaryRuntimeId] as RuntimeEffort
    : "";
  const agentWorking = Boolean(snapshot?.activeRunId) || ["running", "verifying", "repairing", "reviewing"].includes(snapshot?.mission?.phase ?? "");

  if (!bootstrap || !snapshot) return <LaunchScreen error={error} />;

  const receiptActions: ReceiptActions = {
    // The service refuses retrying turn N while turn N+1 is pending, so retry is offered only when idle.
    newestRunId: snapshot.activeRunId ? null : newestReceiptRunId,
    onFeedback: async (memoryIds, useful) => {
      // Bypasses `invoke` on purpose: a failed thumb shows its own inline retry, not the global error banner.
      const result = await window.firekeepStudio.invoke({ type: "keep.feedback", memoryIds: [...memoryIds], useful });
      if (result.type === "error") throw new Error(result.message);
    },
    onRetry: (runId) => ignore(invoke({ type: "keep.receipt.retry", runId }).then(applyResult)),
  };

  return (
    <StudioShell
      busy={agentWorking}
      keepPanelOpen={snapshot.keepPanelOpen}
      topBar={<TopBar
        workspacePath={snapshot.workspacePath}
        recentWorkspaces={snapshot.recentWorkspaces}
        onChooseFolder={() => ignore(chooseWorkspace())}
        onSwitchWorkspace={(path) => ignore(invoke({ type: "workspace.set", path }).then(applyResult))}
        onOpenSettings={() => openSettings()}
        update={updateState}
        onUpdate={() => ignore(updateStudio())}
        keepPanelOpen={snapshot.keepPanelOpen}
        onToggleKeep={() => ignore(invoke({ type: "settings.keepPanel", open: !snapshot.keepPanelOpen }).then(applyResult))}
        agentWorking={agentWorking}
        center={currentSession ? <div className="session-title-display" style={sessionAccentStyle(currentSession.color)}><span className="session-color-dot" /><span className="session-title">{currentSession.name}</span><button type="button" className="session-title-customize" aria-label={`Customize current session ${currentSession.name}`} aria-expanded={sessionEditorId === currentSession.id} title="Edit session name and color" onClick={() => setSessionEditorId((current) => current === currentSession.id ? null : currentSession.id)}><Palette size={11} /></button></div> : <span className="session-title">New session</span>}
      />}
      rail={<SessionRail
        sessions={sessions}
        activeSessionId={snapshot.activeSessionId}
        onNew={() => ignore(invoke({ type: "session.new" }).then(applyResult))}
        onResume={(sessionId) => ignore(invoke({ type: "session.resume", sessionId }).then(applyResult))}
        onEdit={saveSession}
        keepWarning={keepWarning(keepStatus)}
        onKeepWarning={() => openSettings("keep")}
        editingId={sessionEditorId}
        saving={sessionSaving}
        onToggleEdit={(sessionId) => setSessionEditorId((current) => current === sessionId ? null : sessionId)}
        onCloseEdit={() => setSessionEditorId(null)}
      />}
      conversation={
      <main className="conversation">
        <div className="transcript-frame">
          <div className="transcript" ref={transcriptRef} aria-live="polite" onScroll={updateTranscriptFollow}>
            {startVisible ? <StartCard runtimes={bootstrap.runtimes} diagnostics={diagnostics} primaryRuntimeId={snapshot.primaryRuntimeId} recentWorkspaces={snapshot.recentWorkspaces} workspacePath={snapshot.workspacePath} highlights={highlights} keepStatus={keepStatus} onPrimary={(id) => void choosePrimary(id)} onConnect={openConnect} onChooseFolder={() => ignore(chooseWorkspace())} onSwitchWorkspace={(path) => ignore(invoke({ type: "workspace.set", path }).then(applyResult))} onOpenSession={(goal) => { setComposer(`Continue: ${goal}`); composerRef.current?.focus(); }} /> : null}
            {runGroups.map(({ run, reviews }) => <RunTimeline
              key={run.id}
              run={run}
              reviews={reviews}
              runtime={bootstrap.runtimes.find((runtime) => runtime.id === run.runtimeId)}
              runtimes={bootstrap.runtimes}
              resolveApproval={(approvalId, decision) => ignore(invoke({ type: "approval.resolve", approvalId, decision }))}
              openDecision={(url) => ignore(openDecisionBoard(url))}
              decisionLoading={decisionLoading}
              receipts={receiptActions}
              secondOpinion={snapshot.activeRunId || busy ? null : { messageId: lastAnswerId, primaryRuntimeId: snapshot.primaryRuntimeId, onRun: (runtimeId, focus) => ignore(runSecondOpinion(runtimeId, focus)) }}
            />)}
            {snapshot.mission ? <MissionStepper mission={snapshot.mission} workspacePath={snapshot.workspacePath} primaryRuntimeId={snapshot.primaryRuntimeId} busy={busy} onAction={(action) => ignore(controlMission(action))} onCommand={(value) => { setComposer(value); composerRef.current?.focus(); }} /> : null}
            {commandCards.map((card) => <CommandResultCard key={card.id} card={card} />)}
            {error ? <div className="error-banner"><AlertTriangle size={16} /><span>{error}</span><button onClick={() => setError(null)}><X size={14} /></button></div> : null}
          </div>
          {!followingTail ? <button type="button" className="jump-latest" aria-label="Jump to latest response" onClick={jumpToLatest}><ArrowDownToLine size={14} /> Latest</button> : null}
        </div>

        <Composer
          value={composer}
          onChange={setComposer}
          onSubmit={() => ignore(submit())}
          busy={busy}
          canStop={Boolean(snapshot.activeRunId)}
          onStop={() => ignore(invoke({ type: "run.cancel", runId: snapshot.activeRunId! }))}
          onPaste={() => ignore(pasteClipboard())}
          placeholder={snapshot.primaryRuntimeId ? `Message ${primary?.displayName ?? "your agent"}, or type / for commands…` : "Choose a primary runtime, or type /doctor…"}
          textareaRef={composerRef}
          completions={completions}
          completionIndex={completionIndex}
          onCompletionIndex={setCompletionIndex}
          onPickCompletion={(value) => { setComposer(value); setCompletions([]); composerRef.current?.focus(); }}
          usage={{ ...snapshot.usage, tokenBudget: snapshot.tokenBudget ?? null }}
          chips={<><AgentChip
            runtimes={bootstrap.runtimes}
            diagnostics={diagnostics}
            primaryRuntimeId={snapshot.primaryRuntimeId}
            models={primaryModels}
            selectedModel={primaryRuntimeId ? snapshot.selectedModels[primaryRuntimeId] ?? "" : ""}
            selectedEffort={selectedEffort}
            liveEfforts={liveEfforts}
            permissionMode={primaryRuntimeId ? snapshot.permissionModes[primaryRuntimeId] ?? "standard" : "standard"}
            onPrimary={(runtimeId) => void choosePrimary(runtimeId)}
            onModel={(modelId) => { if (primaryRuntimeId) ignore(invoke({ type: "model.set", runtimeId: primaryRuntimeId, modelId }).then(applyResult)); }}
            onEffort={(effort) => { if (primaryRuntimeId) ignore(invoke({ type: "effort.set", runtimeId: primaryRuntimeId, effort }).then(applyResult)); }}
            onPermission={(mode) => { if (primaryRuntimeId) ignore(invoke({ type: "permission.set", runtimeId: primaryRuntimeId, mode }).then(applyResult)); }}
            onLoadModels={() => { if (primaryRuntimeId && !models[primaryRuntimeId]) ignore(loadModels(primaryRuntimeId)); }}
            onRefreshModels={() => { if (primaryRuntimeId) ignore(loadModels(primaryRuntimeId)); }}
            refresh={modelRefresh?.runtimeId === primaryRuntimeId ? modelRefresh : null}
          /><DoneWhenChip checks={doneChecks} onChange={setDoneChecks} detect={detectTestCommand} /></>}
        />
      </main>
      }
      keepPanel={<KeepPanel
        status={keepStatus}
        receipts={keepReceipts}
        invoke={keepRead}
        onInsert={insertQuote}
        onOpenKeepSettings={() => openSettings("keep")}
        onRetry={retryKeep}
        searchRequest={keepSearchRequest}
        onSearchRequestHandled={clearKeepSearchRequest}
        feedbackInvite={!snapshot.feedbackInviteDismissed}
        onFeedback={(choice) => ignore(invoke(choice === "dismiss" ? { type: "feedback.dismiss" } : { type: "feedback.open", target: choice }).then(applyResult))}
      />}
    >

      <CommandPalette
        open={paletteOpen}
        onClose={() => closePalette(true)}
        completions={paletteCompletions}
        onRun={(command) => { closePalette(false); setComposer(`${command.value} `); setCompletions([]); composerRef.current?.focus(); }}
        onSearchKeep={searchKeepFromPalette}
      />
      {settingsSection ? <SettingsSheet
        section={settingsSection}
        onSection={chooseSettingsSection}
        onClose={closeSettings}
        suspendEscape={connectDialog !== null}
        runtimes={bootstrap.runtimes}
        diagnostics={diagnostics}
        primaryRuntimeId={snapshot.primaryRuntimeId}
        reviewerRuntimeIds={snapshot.reviewerRuntimeIds}
        refresh={() => ignore(refreshDiagnostics())}
        choosePrimary={(runtimeId) => ignore(choosePrimary(runtimeId))}
        toggleReviewer={(runtimeId, reviewer) => ignore(invoke({ type: reviewer ? "reviewer.remove" : "reviewer.add", runtimeId }).then(applyResult))}
        connect={openConnect}
        disconnect={(runtimeId) => ignore(invoke({ type: "runtime.logout", runtimeId }).then(() => refreshDiagnostics()))}
        keepStatus={keepStatus}
        dashboardAvailable={bootstrap.dashboardAvailable}
        runCommand={runKitCommand}
        kitCard={kitCard}
        onOpenDashboard={() => ignore(invoke({ type: "dashboard.open" }))}
        reviewerMode={snapshot.reviewerMode}
        onReviewerMode={(mode) => ignore(invoke({ type: "reviewer.mode", mode }).then(applyResult))}
        theme={snapshot.theme}
        onTheme={(theme) => ignore(invoke({ type: "theme.set", theme }).then(applyResult))}
        update={updateState}
        onUpdate={() => ignore(updateStudio())}
      /> : null}
      {connectDialog ? <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && setConnectDialog(null)}><form className="modal" role="dialog" aria-modal="true" aria-labelledby="connect-runtime-title" onSubmit={(event) => { event.preventDefault(); ignore(connect()); }}><button type="button" className="modal-close" aria-label="Close connection dialog" onClick={() => setConnectDialog(null)}><X size={17} /></button><span className="modal-icon"><KeyRound size={22} /></span><h2 id="connect-runtime-title">Connect {runtimeName(bootstrap.runtimes, connectDialog.runtimeId)}</h2><p>Authentication stays with the provider. An API key is handed straight to the provider's own login and Studio does not store it.</p><label>Method<select value={connectDialog.method} onChange={(event) => setConnectDialog({ ...connectDialog, method: event.target.value as LoginMethod })}>{loginMethods(bootstrap.runtimes.find((runtime) => runtime.id === connectDialog.runtimeId)).map((method) => <option key={method} value={method}>{methodLabel(method)}</option>)}</select></label>{connectDialog.method === "api-key" ? <label>API key<input autoFocus type="password" autoComplete="off" value={secret} onChange={(event) => setSecret(event.target.value)} placeholder="Passed to the provider; Studio does not keep it" /></label> : null}<div className="modal-actions"><button type="button" onClick={() => setConnectDialog(null)}>Cancel</button><button className="primary-action" type="submit" disabled={connectDialog.method === "api-key" && !secret}>Continue securely</button></div></form></div> : null}
      {decisionBoard ? <DecisionBoardModal board={decisionBoard} close={() => setDecisionBoard(null)} submit={async (answers) => {
        const result = await invoke({ type: "decision.submit", url: decisionBoard.url, answers });
        if (result.type !== "decision-submitted") throw new Error("Decision Board returned an unexpected submission response");
        setDecisionBoard(null);
      }} /> : null}
    </StudioShell>
  );
}

function LaunchScreen({ error }: { readonly error: string | null }): React.JSX.Element {
  return <main className="launch-shell"><section className="launch-card" aria-live="polite"><span className="brand-mark"><FirekeepMark size={30} /></span><h1>Firekeep Studio</h1><div className="launch-status"><span className="pulse-dot" /> Loading…</div>{error ? <p className="launch-error">{error}</p> : null}</section></main>;
}

interface TimelineActions {
  readonly resolveApproval: (id: string, decision: string) => void;
  readonly openDecision: (url: string) => void;
  readonly decisionLoading: string | null;
}

interface ReceiptActions {
  /** The run whose receipt may offer retry (null while a run is active). */
  readonly newestRunId: string | null;
  readonly onFeedback: (memoryIds: readonly string[], useful: boolean) => Promise<void>;
  readonly onRetry: (runId: string) => void;
}

interface SecondOpinionProps {
  readonly messageId: string | null;
  readonly primaryRuntimeId: string | null;
  readonly onRun: (runtimeId: string, focus: string) => void;
}

function RunTimeline({ run, reviews, runtime, runtimes, resolveApproval, openDecision, decisionLoading, receipts, secondOpinion }: {
  readonly run: TimelineRun;
  readonly reviews?: readonly TimelineRun[];
  readonly runtime: RuntimeDescriptor | undefined;
  readonly runtimes: readonly RuntimeDescriptor[];
  readonly receipts: ReceiptActions;
  readonly secondOpinion: SecondOpinionProps | null;
} & TimelineActions): React.JSX.Element {
  const hasFinalAnswer = run.messages.some((item) => item.role !== "user" && item.complete);
  const [activityOpen, setActivityOpen] = useState(!hasFinalAnswer);
  const previousFinal = useRef(hasFinalAnswer);
  useEffect(() => {
    if (hasFinalAnswer && !previousFinal.current) setActivityOpen(false);
    previousFinal.current = hasFinalAnswer;
  }, [hasFinalAnswer]);
  const steps = run.activity.length;
  const working = run.activity.some((item) => item.kind === "tool" && item.status === "running");
  const cardProps = { runtime, resolveApproval, openDecision, decisionLoading };

  const reviewers = runtimes.filter((candidate) => candidate.capabilities.includes("review"));
  const [chooserOpen, setChooserOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const answerHere = Boolean(secondOpinion?.messageId) && run.messages.some((item) => item.id === secondOpinion?.messageId);
  const chooserVisible = chooserOpen && answerHere && reviewers.length > 0;
  useEffect(() => { if (!answerHere) setChooserOpen(false); }, [answerHere]);
  const closeChooser = useCallback(() => { setChooserOpen(false); triggerRef.current?.focus(); }, []);

  return <section className="run-timeline" data-run-id={run.runId}>
    {run.messages.map((item) => <SafeTimelineCard
      key={`${item.kind}:${item.id}`}
      item={item}
      {...cardProps}
      extra={answerHere && item.id === secondOpinion?.messageId && reviewers.length > 0
        ? <button ref={triggerRef} type="button" className="second-opinion-button" aria-label="Second opinion" aria-expanded={chooserVisible} title="Ask another agent to review this answer" onClick={() => setChooserOpen((value) => !value)}><ShieldCheck size={13} /></button>
        : null}
    />)}
    {chooserVisible && secondOpinion ? <SecondOpinionChooser reviewers={reviewers} primaryRuntimeId={secondOpinion.primaryRuntimeId} onRun={secondOpinion.onRun} onClose={closeChooser} /> : null}
    {run.attention.map((item) => <SafeTimelineCard key={`${item.kind}:${item.id}`} item={item} {...cardProps} />)}
    {steps ? <details className="run-activity" open={activityOpen} onToggle={(event) => setActivityOpen(event.currentTarget.open)}><summary><span className={working ? "pulse-dot" : "work-log-dot"} /><span>{working ? "Working" : "Work log"} · {steps} step{steps === 1 ? "" : "s"}</span><ChevronDown size={14} /></summary><div className="run-activity-items">{run.activity.map((item) => <SafeTimelineCard key={`${item.kind}:${item.id}`} item={item} {...cardProps} />)}</div></details> : null}
    {run.receipt ? <KeepReceiptCard receipt={run.receipt} runId={run.runId} canRetry={receipts.newestRunId === run.runId} onFeedback={receipts.onFeedback} onRetry={receipts.onRetry} /> : null}
    {reviews?.map((review) => <div key={review.id} className="second-opinion"><RunTimeline run={review} runtime={runtimes.find((candidate) => candidate.id === review.runtimeId)} runtimes={runtimes} resolveApproval={resolveApproval} openDecision={openDecision} decisionLoading={decisionLoading} receipts={receipts} secondOpinion={null} /></div>)}
  </section>;
}

function SafeTimelineCard(props: { readonly item: TimelineItem; readonly runtime: RuntimeDescriptor | undefined; readonly extra?: React.ReactNode } & TimelineActions): React.JSX.Element {
  const resetKey = props.item.kind === "message" ? props.item.text : props.item.id;
  return <RenderBoundary resetKey={resetKey}><TimelineCard {...props} /></RenderBoundary>;
}

function TimelineCard({ item, runtime, extra, resolveApproval, openDecision, decisionLoading }: { readonly item: TimelineItem; readonly runtime: RuntimeDescriptor | undefined; readonly extra?: React.ReactNode; readonly resolveApproval: (id: string, decision: string) => void; readonly openDecision: (url: string) => void; readonly decisionLoading: string | null }): React.JSX.Element | null {
  const name = runtime?.displayName ?? item.runtimeId;
  if (item.kind === "message") return <article className={`message-card ${item.role}`}><header><span className="runtime-orb" style={{ "--runtime-accent": runtime?.accent ?? "#df7e45" } as React.CSSProperties}>{item.role === "user" ? "Y" : name[0]}</span><div><strong>{item.role === "user" ? "You" : item.role === "reviewer" ? `${name} review` : name}</strong><small>{formatTime(item.timestamp)}{item.complete ? "" : " · streaming"}</small></div>{extra}<CopyButton text={item.text} /></header><div className="markdown"><RichMarkdown>{item.text}</RichMarkdown></div></article>;
  if (item.kind === "reasoning") return <details className="activity-card reasoning"><summary><Sparkles size={14} /> Reasoning <ChevronDown size={14} /></summary><pre>{item.text}</pre></details>;
  if (item.kind === "tool") {
    const boardUrl = findDecisionBoardUrl(item.output);
    if (boardUrl) return <article className="decision-request-card"><span className="modal-icon"><ShieldCheck size={18} /></span><div><strong>Decision requested</strong><small>{name} is waiting for your answers.</small></div><button type="button" disabled={decisionLoading === boardUrl} onClick={() => openDecision(boardUrl)}>{decisionLoading === boardUrl ? "Opening…" : "Open board"}</button></article>;
    return <details className={`activity-card tool ${item.status}`} open={item.status === "running"}><summary>{item.status === "running" ? <span className="pulse-dot" /> : item.status === "failed" ? <AlertTriangle size={14} /> : <CheckCircle2 size={14} />}<strong>{item.summary}</strong>{item.status === "running" ? <span>working</span> : null}<ChevronDown size={14} /></summary>{item.update ? <p>{item.update}</p> : null}{item.input !== undefined ? <><small>Input · {item.name}</small><pre>{pretty(item.input)}</pre></> : null}{item.output !== undefined ? <><small>Output</small><pre>{pretty(item.output)}</pre></> : null}</details>;
  }
  if (item.kind === "diff") return <details className="activity-card diff"><summary><FileDiff size={14} /><strong>Workspace diff</strong><ChevronDown size={14} /></summary><pre>{item.diff}</pre></details>;
  if (item.kind === "approval") return <article className={`approval-card ${item.decision ? "resolved" : ""}`}><header><ShieldCheck size={18} /><div><strong>{item.title}</strong><small>{name} requests permission</small></div></header><pre>{item.detail}</pre>{item.decision ? <p className="approval-decision"><CheckCircle2 size={14} /> {item.decision}</p> : <div className="approval-actions">{item.options.map((option) => <button key={option} onClick={() => resolveApproval(item.approvalId, option)} className={/accept|allow/i.test(option) ? "approve" : ""}>{option}</button>)}</div>}</article>;
  if (item.kind === "usage") return <div className="usage-row"><Cpu size={13} />{formatUsage(item.usage)}</div>;
  if (item.kind === "notice") {
    if (item.level === "info" && /run (started|completed)$/.test(item.message)) return null;
    return <div className={`notice-row ${item.level}`}>{item.level === "error" ? <AlertTriangle size={14} /> : <CheckCircle2 size={14} />}<span>{item.message}{item.detail ? ` · ${item.detail}` : ""}</span></div>;
  }
  return null;
}

function DecisionBoardModal({ board, close, submit }: { readonly board: DecisionBoardDocument; readonly close: () => void; readonly submit: (answers: DecisionAnswers) => Promise<void> }): React.JSX.Element {
  const [answers, setAnswers] = useState<DecisionAnswers>(() => Object.fromEntries(board.spec.questions.map((question) => [question.id, { answer: "", actions_confirmed: [], skipped: false }])));
  const [submitting, setSubmitting] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const update = (questionId: string, value: Partial<DecisionAnswers[string]>): void => {
    setAnswers((current) => ({ ...current, [questionId]: { ...current[questionId]!, ...value } }));
  };
  const send = async (): Promise<void> => {
    setSubmitting(true);
    setFailure(null);
    try { await submit(answers); }
    catch (caught) { setFailure(caught instanceof Error ? caught.message : String(caught)); }
    finally { setSubmitting(false); }
  };
  const embed = (meta: { readonly index: number }): DecisionEmbed | undefined => board.embeds.find((item) => item.index === meta.index);

  return <div className="decision-board-backdrop" role="presentation">
    <form className="decision-board" aria-label="Decision Board" onSubmit={(event) => { event.preventDefault(); void send(); }}>
      <header className="decision-board-header"><span className="modal-icon"><ShieldCheck size={22} /></span><div><p className="eyebrow">FIREKEEP DECISION BOARD</p><h2>Your agent needs direction</h2><span>{board.spec.questions.length} question{board.spec.questions.length === 1 ? "" : "s"} · answers return directly to the active runtime</span></div><button type="button" className="modal-close" aria-label="Close Decision Board" onClick={close}><X size={18} /></button></header>
      <div className="decision-board-scroll">
        {board.spec.degraded ? <div className="decision-degraded"><AlertTriangle size={15} /><span>Retrieval-only board{board.spec.note ? ` · ${board.spec.note}` : ""}</span></div> : null}
        {board.spec.context ? <section className="decision-context"><span className="decision-label">Context</span><div className="markdown"><RichMarkdown>{board.spec.context}</RichMarkdown></div></section> : null}
        {board.spec.boardEmbeds.map((meta) => embed(meta) ? <DecisionEmbedFrame key={meta.index} embed={embed(meta)!} /> : null)}
        {board.spec.questions.map((question, index) => <DecisionQuestionCard key={question.id} question={question} number={index + 1} answer={answers[question.id]!} embeds={board.spec.embedsByQuestion[question.id]?.map(embed).filter((item): item is DecisionEmbed => item !== undefined) ?? []} update={(value) => update(question.id, value)} />)}
        {board.spec.questions.length === 0 ? <p className="decision-empty">This board has no questions.</p> : null}
      </div>
      <footer className="decision-board-footer">{failure ? <span className="decision-submit-error"><AlertTriangle size={14} /> {failure}</span> : <span>Closing without submitting keeps the agent waiting; you can reopen this board from the conversation.</span>}<div><button type="button" onClick={close}>Not now</button><button type="submit" className="primary-action" disabled={submitting || board.spec.questions.length === 0}>{submitting ? "Sending answers…" : "Send answers"}</button></div></footer>
    </form>
  </div>;
}

function DecisionQuestionCard({ question, number, answer, embeds, update }: { readonly question: DecisionQuestion; readonly number: number; readonly answer: DecisionAnswers[string]; readonly embeds: readonly DecisionEmbed[]; readonly update: (value: Partial<DecisionAnswers[string]>) => void }): React.JSX.Element {
  const toggleAction = (action: string, checked: boolean): void => update({ actions_confirmed: checked ? [...answer.actions_confirmed, action] : answer.actions_confirmed.filter((item) => item !== action) });
  return <section className={`decision-question ${answer.skipped ? "skipped" : ""}`}>
    <header><span>Question {number}</span><span className={question.knowledgeFound ? "knowledge-found" : "knowledge-missing"}>{question.knowledgeFound ? "Knowledge found" : "No prior knowledge"}</span></header>
    <div className="decision-question-text markdown"><RichMarkdown>{question.text}</RichMarkdown></div>
    {embeds.map((item) => <DecisionEmbedFrame key={item.index} embed={item} />)}
    {question.evidence.length ? <details className="decision-evidence"><summary>Evidence ({question.evidence.length}) <ChevronDown size={13} /></summary>{question.evidence.map((item, index) => <article key={`${item.source}:${index}`}><strong>{item.source || "Team memory"}</strong><p>{item.snippet.replace(/\s+/g, " ").trim()}</p>{safeHttpUrl(item.ref) ? <a href={item.ref} target="_blank" rel="noreferrer">Open source <ExternalLink size={11} /></a> : null}</article>)}</details> : null}
    {question.suggestedAnswers.length ? <div className="decision-suggestions"><span className="decision-label">Suggested answers</span><div>{question.suggestedAnswers.map((suggestion) => <button type="button" key={suggestion} onClick={() => update({ answer: suggestion, skipped: false })}>{suggestion}</button>)}</div></div> : null}
    {question.suggestedActions.length ? <fieldset className="decision-actions"><legend>Suggested actions · confirm explicitly</legend>{question.suggestedActions.map((action) => <label key={action}><input type="checkbox" checked={answer.actions_confirmed.includes(action)} disabled={answer.skipped} onChange={(event) => toggleAction(action, event.target.checked)} /><span>{action}</span></label>)}</fieldset> : null}
    <label className="decision-answer"><span className="decision-label">Your answer</span><textarea rows={3} disabled={answer.skipped} value={answer.answer} placeholder="Type your answer…" onChange={(event) => update({ answer: event.target.value })} /></label>
    <label className="decision-skip"><input type="checkbox" checked={answer.skipped} onChange={(event) => update({ skipped: event.target.checked })} /> Skip this question</label>
  </section>;
}

function DecisionEmbedFrame({ embed }: { readonly embed: DecisionEmbed }): React.JSX.Element {
  return <figure className="decision-embed"><figcaption>{embed.title || "Decision visual"}</figcaption><iframe title={embed.title || "Decision visual"} height={embed.height} sandbox="allow-scripts" referrerPolicy="no-referrer" loading="lazy" src={sandboxedEmbedUrl(embed.html)} /></figure>;
}

function sandboxedEmbedUrl(html: string): string {
  const csp = "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; media-src data: blob:; font-src data:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'\">";
  const document = /<head(?:\s[^>]*)?>/i.test(html) ? html.replace(/<head(?:\s[^>]*)?>/i, (head) => `${head}${csp}`) : `${csp}${html}`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(document)}`;
}

function safeHttpUrl(value: string | undefined): boolean {
  if (!value) return false;
  try { return ["http:", "https:"].includes(new URL(value).protocol); }
  catch { return false; }
}

function keepWarning(status: KeepStatus | null): string | null {
  if (!status || status.state === "starting" || status.state === "connected") return null;
  if (status.state === "missing") return "Keep not installed";
  if (status.state === "unconfigured") return "Keep not configured";
  return "Keep unreachable";
}
function runtimeName(runtimes: readonly RuntimeDescriptor[], id: string): string { return runtimes.find((runtime) => runtime.id === id)?.displayName ?? id; }
function pretty(value: unknown): string { return typeof value === "string" ? value : JSON.stringify(value, null, 2); }
function formatTime(value: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
function formatUsage(usage: RuntimeUsage): string { const pieces = [usage.inputTokens !== undefined ? `${usage.inputTokens.toLocaleString()} in` : "", usage.cacheCreationInputTokens !== undefined ? `${usage.cacheCreationInputTokens.toLocaleString()} cache write` : "", usage.cachedInputTokens !== undefined ? `${usage.cachedInputTokens.toLocaleString()} cached` : "", usage.outputTokens !== undefined ? `${usage.outputTokens.toLocaleString()} out` : "", usage.reasoningTokens !== undefined ? `${usage.reasoningTokens.toLocaleString()} reasoning` : "", usage.costUsd !== undefined ? `$${usage.costUsd.toFixed(4)}` : "", usage.durationMs !== undefined ? `${(usage.durationMs / 1_000).toFixed(1)}s` : ""].filter(Boolean); return pieces.join(" · ") || `${usage.totalTokens?.toLocaleString() ?? 0} tokens`; }
function loginMethods(runtime: RuntimeDescriptor | undefined): readonly LoginMethod[] { return runtime?.loginMethods?.length ? runtime.loginMethods : ["browser"]; }
function methodLabel(method: LoginMethod): string { return method === "api-key" ? "API key" : method === "device" ? "Device code" : method[0]?.toUpperCase() + method.slice(1); }
