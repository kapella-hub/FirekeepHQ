import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowDownToLine,
  CheckCircle2,
  ChevronDown,
  ClipboardPaste,
  Code2,
  Copy,
  Cpu,
  Download,
  Eye,
  ExternalLink,
  FileDiff,
  FolderOpen,
  KeyRound,
  LogOut,
  Maximize2,
  Mic,
  MicOff,
  Minimize2,
  Palette,
  PanelRightClose,
  PanelRightOpen,
  PanelsTopLeft,
  Plus,
  RefreshCw,
  Send,
  ShieldCheck,
  Sparkles,
  Square,
  SunMoon,
  Terminal,
  Volume2,
  VolumeX,
  X,
} from "lucide-react";
import type { CommandCompletion, CommandResult } from "../../core/slash-commands";
import type { MissionSnapshot } from "../../core/mission";
import { DEFAULT_SESSION_COLOR, type SessionColor, type StudioSessionSummary } from "../../core/session-store";
import type { RuntimeDiagnostic, StudioSnapshot } from "../../core/studio-service";
import type { LoginMethod, RuntimeDescriptor, RuntimeEffort, RuntimeEvent, RuntimeModel, RuntimeUsage } from "../../core/runtime";
import type { BootstrapResult, StudioAction, StudioActionResult, StudioUpdateState } from "../../shared/ipc";
import type { DecisionAnswers, DecisionBoardDocument, DecisionEmbed, DecisionQuestion } from "../../shared/decision-board";
import { findDecisionBoardUrl } from "../../shared/decision-board";
import { FirekeepMark } from "./FirekeepMark.js";
import { RichMarkdown } from "./RichMarkdown.js";
import { RenderBoundary } from "./RenderBoundary.js";
import { RuntimePicker } from "./RuntimePicker.js";
import { coalesceRuntimeEvents } from "./runtime-event-buffer.js";
import { buildTimeline, groupTimeline, type TimelineItem, type TimelineRun } from "./timeline";

interface CommandCard { readonly id: string; readonly input: string; readonly result: CommandResult }
interface ConnectDialog { readonly runtimeId: string; readonly method: LoginMethod }
interface ModelRefresh { readonly runtimeId: string; readonly state: "loading" | "success" | "error"; readonly message: string }
type StudioView = "conversation" | "agents";
type VoiceInputState = "idle" | "listening" | "stopping";

const SESSION_COLOR_OPTIONS: readonly { readonly id: SessionColor; readonly label: string; readonly value: string }[] = [
  { id: "ember", label: "Ember", value: "#ff7a2f" },
  { id: "gold", label: "Gold", value: "#dca63a" },
  { id: "moss", label: "Moss", value: "#65a86f" },
  { id: "teal", label: "Teal", value: "#3da5a0" },
  { id: "ocean", label: "Ocean", value: "#4b8fdf" },
  { id: "violet", label: "Violet", value: "#8f83ff" },
  { id: "rose", label: "Rose", value: "#d66d91" },
  { id: "slate", label: "Slate", value: "#7f8997" },
];

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
  const [completions, setCompletions] = useState<readonly CommandCompletion[]>([]);
  const [completionIndex, setCompletionIndex] = useState(0);
  const [commandCards, setCommandCards] = useState<CommandCard[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rightOpen, setRightOpen] = useState(true);
  const [runtimeManagerOpen, setRuntimeManagerOpen] = useState(false);
  const [connectDialog, setConnectDialog] = useState<ConnectDialog | null>(null);
  const [secret, setSecret] = useState("");
  const [voiceInputState, setVoiceInputState] = useState<VoiceInputState>("idle");
  const [decisionBoard, setDecisionBoard] = useState<DecisionBoardDocument | null>(null);
  const [decisionLoading, setDecisionLoading] = useState<string | null>(null);
  const [followingTail, setFollowingTail] = useState(true);
  const [view, setView] = useState<StudioView>("conversation");
  const [paneRuntimeIds, setPaneRuntimeIds] = useState<string[]>([]);
  const [activePaneId, setActivePaneId] = useState<string | null>(null);
  const [focusedPaneId, setFocusedPaneId] = useState<string | null>(null);
  const [paneMenuOpen, setPaneMenuOpen] = useState(false);
  const [sessionEditorId, setSessionEditorId] = useState<string | null>(null);
  const [sessionSaving, setSessionSaving] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const followingTailRef = useRef(true);
  const voiceEnabledRef = useRef(false);
  const modelRequestRef = useRef<Record<string, number>>({});
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
        if (payload.kind === "message.completed" && payload.role === "assistant" && voiceEnabledRef.current && "speechSynthesis" in window) {
          window.speechSynthesis.cancel();
          window.speechSynthesis.speak(new SpeechSynthesisUtterance(stripMarkdown(payload.text).slice(0, 8_000)));
        }
      } else if (event.type === "snapshot") setSnapshot(event.snapshot);
      else if (event.type === "update") setUpdateState(event.state);
      else if (event.type === "decision.available") {
        surfacedBoardsRef.current.add(event.board.url);
        setDecisionBoard(event.board);
      } else setSessions([...event.sessions]);
    });
    ignore(invoke({ type: "bootstrap" }).then((result) => {
      if (!active || result.type !== "bootstrap") return;
      setBootstrap(result);
      setUpdateState(result.update);
      setSnapshot(result.snapshot);
      replaceEvents(result.events);
      setSessions([...result.sessions]);
      ignore(refreshDiagnostics());
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
    voiceEnabledRef.current = snapshot?.voiceEnabled ?? false;
    document.documentElement.dataset.theme = snapshot?.theme ?? "system";
  }, [snapshot?.theme, snapshot?.voiceEnabled]);

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

  const showAgentGrid = useCallback((): void => {
    if (!bootstrap || !snapshot) return;
    const chatRuntimes = new Set(bootstrap.runtimes.filter((runtime) => runtime.capabilities.includes("chat")).map((runtime) => runtime.id));
    const suggested = [...new Set([
      snapshot.primaryRuntimeId,
      ...snapshot.reviewerRuntimeIds,
      ...events.map((event) => event.runtimeId),
    ].filter((runtimeId): runtimeId is string => Boolean(runtimeId && chatRuntimes.has(runtimeId))))];
    const fallback = bootstrap.runtimes.find((runtime) => runtime.capabilities.includes("chat"))?.id;
    const next = suggested.length ? suggested : fallback ? [fallback] : [];
    setPaneRuntimeIds((current) => current.length ? current.filter((runtimeId) => chatRuntimes.has(runtimeId)) : next);
    setActivePaneId((current) => current && chatRuntimes.has(current) ? current : snapshot.primaryRuntimeId ?? next[0] ?? null);
    setFocusedPaneId(null);
    setPaneMenuOpen(false);
    setView("agents");
  }, [bootstrap, events, snapshot]);

  const toggleAgentGrid = useCallback((): void => {
    if (view === "agents") {
      setView("conversation");
      setFocusedPaneId(null);
      setPaneMenuOpen(false);
    } else showAgentGrid();
  }, [showAgentGrid, view]);

  useEffect(() => {
    const keyboard = (event: KeyboardEvent): void => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setComposer((value) => value.startsWith("/") ? value : "/");
        composerRef.current?.focus();
      } else if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === "r") {
        event.preventDefault();
        if (!busy) ignore(runReview());
      } else if ((event.ctrlKey || event.metaKey) && event.code === "Backslash") {
        event.preventDefault();
        toggleAgentGrid();
      } else if (event.key === "Escape") {
        if (connectDialog) setConnectDialog(null);
        else if (runtimeManagerOpen) setRuntimeManagerOpen(false);
        else if (sessionEditorId) setSessionEditorId(null);
        else if (focusedPaneId) setFocusedPaneId(null);
        else if (voiceInputState !== "idle") ignore(invoke({ type: "voice.input.stop" }));
        else if (snapshot?.activeRunId) ignore(invoke({ type: "run.cancel", runId: snapshot.activeRunId }));
      }
    };
    window.addEventListener("keydown", keyboard);
    return () => window.removeEventListener("keydown", keyboard);
  }, [busy, connectDialog, focusedPaneId, invoke, runReview, runtimeManagerOpen, sessionEditorId, snapshot?.activeRunId, toggleAgentGrid, voiceInputState]);

  const submit = async (): Promise<void> => {
    const input = composer.trim();
    if (!input || busy) return;
    setComposer("");
    setCompletions([]);
    setBusy(true);
    try {
      if (input.startsWith("/")) {
        const result = await invoke({ type: "command.execute", input });
        applyResult(result);
        if (result.type === "command") setCommandCards((current) => [...current, { id: crypto.randomUUID(), input, result: result.result }]);
      } else if (view === "agents" && activePaneId) {
        applyResult(await invoke({ type: "message.sendTo", runtimeId: activePaneId, text: input }));
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

  const toggleVoiceInput = async (): Promise<void> => {
    if (voiceInputState === "stopping") return;
    if (voiceInputState === "listening") {
      setVoiceInputState("stopping");
      try { await invoke({ type: "voice.input.stop" }); }
      catch { /* invoke surfaced the actionable error. */ }
      return;
    }
    setVoiceInputState("listening");
    try {
      const result = await invoke({ type: "voice.input.start", language: navigator.language || "en-US" });
      if (result.type !== "voice-input") throw new Error("voice input returned an unexpected response");
      if (result.state === "complete" && result.text) {
        setComposer((current) => `${current}${current && !/\s$/.test(current) ? " " : ""}${result.text}`);
      } else if (result.state === "empty" || result.state === "unavailable") setError(result.detail);
    } catch { /* invoke surfaced the actionable error. */ }
    finally {
      setVoiceInputState("idle");
      composerRef.current?.focus();
    }
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
  const primary = bootstrap?.runtimes.find((runtime) => runtime.id === snapshot?.primaryRuntimeId);
  const currentSession = sessions.find((session) => session.id === snapshot?.activeSessionId);
  const budgetPercent = snapshot?.tokenBudget ? Math.min(100, (snapshot.usage.tokens / snapshot.tokenBudget) * 100) : 0;
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
  const paneRuntimes = (bootstrap?.runtimes ?? []).filter((runtime) => paneRuntimeIds.includes(runtime.id) && runtime.capabilities.includes("chat"));
  const availablePaneRuntimes = (bootstrap?.runtimes ?? []).filter((runtime) => runtime.capabilities.includes("chat") && !paneRuntimeIds.includes(runtime.id));
  const activePane = paneRuntimes.find((runtime) => runtime.id === activePaneId) ?? paneRuntimes[0];
  const activeRunRuntimeId = snapshot?.activeRunId ? events.find((event) => event.runId === snapshot.activeRunId)?.runtimeId ?? null : null;

  if (!bootstrap || !snapshot) return <LaunchScreen error={error} />;

  return (
    <div className="studio" data-busy={agentWorking || undefined} data-view={view}>
      <header className="titlebar">
        <div className="brand"><span className="brand-icon" role="status" aria-label={agentWorking ? "Agent working" : "Firekeep Studio idle"}><FirekeepMark size={18} />{agentWorking ? <span className="brand-activity" /> : null}</span><span>Firekeep</span><strong>Studio</strong></div>
        <div className="titlebar-center">
          {currentSession ? <div className="session-title-display" style={sessionAccentStyle(currentSession.color)}><span className="session-color-dot" /><span className="session-title">{currentSession.name}</span><button type="button" className="session-title-customize" aria-label={`Customize current session ${currentSession.name}`} aria-expanded={sessionEditorId === currentSession.id} title="Edit session name and color" onClick={() => setSessionEditorId((current) => current === currentSession.id ? null : currentSession.id)}><Palette size={11} /></button></div> : <span className="session-title">New session</span>}
          <span className="session-id">{snapshot.activeSessionId.slice(0, 12)}</span>
        </div>
        <div className="titlebar-actions">
          {updateState && updateState.phase !== "disabled" ? <StudioUpdateButton state={updateState} onAction={() => ignore(updateStudio())} /> : null}
          <button className="icon-button" title={snapshot.voiceEnabled ? "Turn spoken replies off" : "Turn spoken replies on"} onClick={() => ignore(invoke({ type: "voice.set", enabled: !snapshot.voiceEnabled }).then(applyResult))}>{snapshot.voiceEnabled ? <Volume2 size={17} /> : <VolumeX size={17} />}</button>
          <button className="icon-button appearance-button" aria-label={`Appearance: ${themeLabel(snapshot.theme)}. Switch to ${nextTheme(snapshot.theme)} theme`} title={`Appearance: ${themeLabel(snapshot.theme)} · next ${nextTheme(snapshot.theme)}`} onClick={() => ignore(invoke({ type: "theme.set", theme: nextTheme(snapshot.theme) }).then(applyResult))}><SunMoon size={17} /></button>
          <button className="inspector-toggle" aria-label={rightOpen ? "Hide inspector" : "Show inspector"} title={rightOpen ? "Hide the right inspector" : "Show the right inspector"} onClick={() => setRightOpen((value) => !value)}>{rightOpen ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />}<span>{rightOpen ? "Hide panel" : "Show panel"}</span></button>
        </div>
      </header>

      <aside className="session-rail">
        <div className="rail-heading"><span>Sessions</span><button className="icon-button compact" title="New session" onClick={() => ignore(invoke({ type: "session.new" }).then(applyResult))}><Plus size={15} /></button></div>
        <nav className="session-list" aria-label="Studio sessions">
          {sessions.map((session) => (
            <div key={session.id} className={`session-entry ${session.id === snapshot.activeSessionId ? "active" : ""} ${sessionEditorId === session.id ? "editing" : ""}`} style={sessionAccentStyle(session.color)}>
              <button className="session-row" aria-current={session.id === snapshot.activeSessionId ? "page" : undefined} onClick={() => { if (session.id !== snapshot.activeSessionId) ignore(invoke({ type: "session.resume", sessionId: session.id }).then(applyResult)); }}>
                <span className="session-row-heading"><span className="session-color-dot" /><span className="session-row-title">{session.name}</span></span>
                <span className="session-row-meta">{session.mission ? `${session.mission.phase} · ` : ""}{session.eventCount} events · {relativeTime(session.updatedAt)}</span>
              </button>
              <button type="button" className="session-customize" aria-label={`Customize ${session.name}`} aria-expanded={sessionEditorId === session.id} onClick={() => setSessionEditorId((current) => current === session.id ? null : session.id)}><Palette size={13} /></button>
              {sessionEditorId === session.id ? <SessionEditor session={session} saving={sessionSaving} close={() => setSessionEditorId(null)} save={(name, color) => saveSession(session.id, name, color)} /> : null}
            </div>
          ))}
          {!sessions.length ? <div className="session-list-empty"><Sparkles size={17} /><span>Your next session starts here.</span></div> : null}
        </nav>
        <div className="rail-footer">
          <section className="usage-summary rail-usage" aria-labelledby="session-usage-heading">
            <div className="section-heading"><span id="session-usage-heading">Session usage</span><button className="text-button inline" onClick={() => { setComposer("/budget "); composerRef.current?.focus(); }}>Set guard</button></div>
            <div className="usage-total"><strong>{snapshot.usage.freshTokens.toLocaleString()} fresh tokens</strong></div>
            <div className="usage-breakdown"><span>{snapshot.usage.cachedTokens.toLocaleString()} cached</span><span>{snapshot.usage.tokens.toLocaleString()} total traffic</span></div>
            {snapshot.tokenBudget ? <><div className="budget-track" aria-label={`${budgetPercent.toFixed(0)} percent of token guard used`}><span style={{ width: `${budgetPercent}%` }} /></div><small>{Math.max(0, snapshot.tokenBudget - snapshot.usage.tokens).toLocaleString()} before the next-turn guard</small></> : <small>No token guard · use <code>/budget set 50k</code></small>}
            <small>{snapshot.usage.measuredRuns}/{snapshot.usage.totalRuns} runs reported usage{snapshot.usage.costUsd ? ` · $${snapshot.usage.costUsd.toFixed(4)} reported` : " · provider cost unavailable"}</small>
          </section>
          <button className="workspace-picker" disabled={busy} title={snapshot.workspacePath ?? "Choose a workspace"} onClick={() => ignore(chooseWorkspace())}>
            <FolderOpen size={16} />
            <span><small>Workspace</small><strong>{snapshot.workspacePath ? pathLeaf(snapshot.workspacePath) : "Choose folder"}</strong></span>
          </button>
          <span className="keep-indicator"><ShieldCheck size={14} /> Client Kit controls ready</span>
          <div className="keep-actions"><button className="text-button" onClick={() => { setComposer("/firekeep status"); composerRef.current?.focus(); }}>Manage</button>{bootstrap.dashboardAvailable ? <button className="text-button dashboard-link" aria-label="Open Firekeep dashboard" onClick={() => ignore(invoke({ type: "dashboard.open" }))}>Dashboard <ExternalLink size={11} /></button> : null}</div>
        </div>
      </aside>

      <main className="conversation">
        <div className="conversation-toolbar">
          <RuntimePicker runtimes={bootstrap.runtimes} selectedId={snapshot.primaryRuntimeId} diagnostics={diagnostics} onSelect={(runtimeId) => void choosePrimary(runtimeId)} onManage={() => { setRuntimeManagerOpen(true); ignore(refreshDiagnostics()); }} />
          <div className="reviewer-strip" aria-label="Reviewers">
            <Eye size={15} /><span className="reviewer-label">Reviewers</span>
            {!snapshot.reviewerRuntimeIds.length ? <span className="reviewer-empty">No reviewers</span> : null}
            {snapshot.reviewerRuntimeIds.map((id) => <span className="reviewer-chip" key={id}>{runtimeName(bootstrap.runtimes, id)}<button aria-label={`Remove ${id} reviewer`} onClick={() => ignore(invoke({ type: "reviewer.remove", runtimeId: id }).then(applyResult))}><X size={12} /></button></span>)}
            <button className="review-now" title={!snapshot.reviewerRuntimeIds.length ? "Add a reviewer in Runtime Center" : !hasPrimaryResponse ? "Send a primary message before running a review" : "Run a review now"} disabled={!snapshot.reviewerRuntimeIds.length || !hasPrimaryResponse || busy} onClick={() => ignore(runReview())}>Run now</button>
          </div>
          <div className="layout-controls">
            <button type="button" className={`layout-toggle ${view === "agents" ? "active" : ""}`} aria-label={view === "agents" ? "Close agent grid" : "Open agent grid"} aria-pressed={view === "agents"} title="Toggle agent grid (Ctrl/Cmd+\\)" onClick={toggleAgentGrid}><PanelsTopLeft size={14} /><span>{view === "agents" ? "Conversation" : "Agents"}</span></button>
            {view === "agents" && availablePaneRuntimes.length ? <div className="pane-add">
              <button type="button" className="layout-toggle" aria-label="Add agent pane" aria-expanded={paneMenuOpen} onClick={() => setPaneMenuOpen((value) => !value)}><Plus size={14} /><span>Add</span></button>
              {paneMenuOpen ? <div className="pane-menu" role="menu" aria-label="Available agent panes">{availablePaneRuntimes.map((runtime) => <button type="button" role="menuitem" key={runtime.id} onClick={() => { setPaneRuntimeIds((current) => [...current, runtime.id]); setActivePaneId(runtime.id); setPaneMenuOpen(false); }}><span className="runtime-orb" style={{ "--runtime-accent": runtime.accent ?? "#df7e45" } as React.CSSProperties}>{runtime.displayName[0]}</span><span><strong>{runtime.displayName}</strong><small>{runtime.transport}</small></span></button>)}</div> : null}
            </div> : null}
          </div>
        </div>

        <div className="transcript-frame">
          {view === "conversation" ? <>
            <div className="transcript" ref={transcriptRef} aria-live="polite" onScroll={updateTranscriptFollow}>
              {timeline.length === 0 && commandCards.length === 0 ? <Welcome runtimes={bootstrap.runtimes} workspacePath={snapshot.workspacePath} onWorkspace={() => ignore(chooseWorkspace())} onChoose={(id) => void choosePrimary(id)} onCommand={(value) => { setComposer(value); composerRef.current?.focus(); }} /> : null}
              {timelineRuns.map((run) => <RunTimeline key={run.id} run={run} runtime={bootstrap.runtimes.find((runtime) => runtime.id === run.runtimeId)} resolveApproval={(approvalId, decision) => ignore(invoke({ type: "approval.resolve", approvalId, decision }))} openDecision={(url) => ignore(openDecisionBoard(url))} decisionLoading={decisionLoading} />)}
              {commandCards.map((card) => <CommandResultCard key={card.id} card={card} />)}
              {error ? <div className="error-banner"><AlertTriangle size={16} /><span>{error}</span><button onClick={() => setError(null)}><X size={14} /></button></div> : null}
            </div>
            {!followingTail ? <button type="button" className="jump-latest" aria-label="Jump to latest response" onClick={jumpToLatest}><ArrowDownToLine size={14} /> Latest</button> : null}
          </> : <AgentGrid runtimes={paneRuntimes} runs={timelineRuns} activeRuntimeId={activePane?.id ?? null} focusedRuntimeId={focusedPaneId} primaryRuntimeId={snapshot.primaryRuntimeId} workingRuntimeId={activeRunRuntimeId} diagnostics={diagnostics} commandCards={commandCards} error={error} select={(runtimeId) => setActivePaneId(runtimeId)} close={(runtimeId) => { setPaneRuntimeIds((current) => current.filter((id) => id !== runtimeId)); setActivePaneId((current) => current === runtimeId ? paneRuntimeIds.find((id) => id !== runtimeId) ?? null : current); setFocusedPaneId((current) => current === runtimeId ? null : current); }} focus={(runtimeId) => setFocusedPaneId((current) => current === runtimeId ? null : runtimeId)} makePrimary={(runtimeId) => void choosePrimary(runtimeId)} resolveApproval={(approvalId, decision) => ignore(invoke({ type: "approval.resolve", approvalId, decision }))} openDecision={(url) => ignore(openDecisionBoard(url))} decisionLoading={decisionLoading} clearError={() => setError(null)} />}
        </div>

        <div className="composer-zone">
          {completions.length ? <div className="command-menu" role="listbox">{completions.slice(0, 9).map((item, index) => <button role="option" aria-selected={index === completionIndex} key={`${item.value}:${index}`} className={index === completionIndex ? "selected" : ""} onMouseDown={(event) => { event.preventDefault(); setComposer(item.value); setCompletions([]); composerRef.current?.focus(); }}><span>{item.label}</span><small>{item.description}</small></button>)}</div> : null}
          <div className="composer-shell">
            <textarea ref={composerRef} aria-label="Message composer" value={composer} rows={1} placeholder={view === "agents" && activePane ? `Message ${activePane.displayName} pane, or type / for commands…` : snapshot.primaryRuntimeId ? `Message ${primary?.displayName ?? "your agent"}, or type / for commands…` : "Choose a primary runtime, or type /doctor…"} onChange={(event) => setComposer(event.target.value)} onKeyDown={(event) => {
              if (event.key === "ArrowDown" && completions.length) { event.preventDefault(); setCompletionIndex((value) => Math.min(value + 1, completions.length - 1)); }
              else if (event.key === "ArrowUp" && completions.length) { event.preventDefault(); setCompletionIndex((value) => Math.max(value - 1, 0)); }
              else if (event.key === "Tab" && completions[completionIndex]) { event.preventDefault(); setComposer(completions[completionIndex].value); setCompletions([]); }
              else if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); ignore(submit()); }
            }} />
            <div className="composer-actions">
              <button type="button" className="paste-button" aria-label="Paste from clipboard" title="Paste from clipboard" onClick={() => ignore(pasteClipboard())}><ClipboardPaste size={17} /></button>
              <button type="button" className={`mic-button ${voiceInputState !== "idle" ? "listening" : ""}`} aria-label={voiceInputState === "idle" ? "Start voice input" : voiceInputState === "listening" ? "Stop voice input" : "Stopping voice input"} title={voiceInputState === "idle" ? "Start voice input" : voiceInputState === "listening" ? "Stop voice input" : "Stopping voice input"} disabled={voiceInputState === "stopping"} onClick={() => ignore(toggleVoiceInput())}>{voiceInputState === "idle" ? <Mic size={18} /> : <MicOff size={18} />}</button>
              {snapshot.activeRunId ? <button className="send-button stop" title="Cancel run" onClick={() => ignore(invoke({ type: "run.cancel", runId: snapshot.activeRunId! }))}><Square size={15} fill="currentColor" /></button> : <button className="send-button" title="Send" disabled={!composer.trim() || busy} onClick={() => ignore(submit())}><Send size={17} /></button>}
            </div>
          </div>
          <div className="composer-hint"><span><kbd>Enter</kbd> send · <kbd>Shift Enter</kbd> newline · <kbd>⌘K</kbd> commands</span><span>{voiceInputState !== "idle" ? <><span className="pulse-dot" /> {voiceInputState === "listening" ? "Listening · speak naturally" : "Stopping voice input"}</> : busy ? <><span className="pulse-dot" /> Agent working</> : `${snapshot.usage.freshTokens.toLocaleString()} fresh · ${snapshot.usage.tokens.toLocaleString()} total${snapshot.voiceEnabled ? " · voice replies on" : ""}`}</span></div>
        </div>
      </main>

      {rightOpen ? <aside className="inspector" aria-label="Studio inspector">
        <MissionPanel
          mission={snapshot.mission}
          workspacePath={snapshot.workspacePath}
          primaryRuntimeId={snapshot.primaryRuntimeId}
          busy={busy}
          onAction={(action) => ignore(controlMission(action))}
          onCommand={(value) => { setComposer(value); composerRef.current?.focus(); }}
        />
        <section className="inspector-section">
          <div className="section-heading"><span>Turn controls</span><button className="icon-button compact" disabled={!snapshot.primaryRuntimeId || (modelRefresh?.runtimeId === snapshot.primaryRuntimeId && modelRefresh.state === "loading")} aria-label="Refresh live model options" title="Refresh live model options" onClick={() => { if (snapshot.primaryRuntimeId) ignore(loadModels(snapshot.primaryRuntimeId)); }}><RefreshCw className={modelRefresh?.runtimeId === snapshot.primaryRuntimeId && modelRefresh.state === "loading" ? "spin" : undefined} size={14} /></button></div>
          {snapshot.primaryRuntimeId && modelRefresh?.runtimeId === snapshot.primaryRuntimeId ? <p className={`model-refresh-status ${modelRefresh.state}`} role="status" aria-live="polite">{modelRefresh.message}</p> : null}
          <label>Model<select disabled={!snapshot.primaryRuntimeId} value={snapshot.primaryRuntimeId ? snapshot.selectedModels[snapshot.primaryRuntimeId] ?? "" : ""} onFocus={() => { if (snapshot.primaryRuntimeId && !models[snapshot.primaryRuntimeId]) ignore(loadModels(snapshot.primaryRuntimeId)); }} onChange={(event) => { if (snapshot.primaryRuntimeId) ignore(invoke({ type: "model.set", runtimeId: snapshot.primaryRuntimeId, modelId: event.target.value }).then(applyResult)); }}><option value="">Provider default</option>{primaryModels.filter((model) => !(model.id === "default" && model.isDefault)).map((model) => <option key={model.id} value={model.id}>{model.displayName}</option>)}</select></label>
          <label>Reasoning<select disabled={!snapshot.primaryRuntimeId || !primary?.capabilities.includes("reasoning") || liveEfforts.length === 0} value={selectedEffort} onChange={(event) => { if (snapshot.primaryRuntimeId) ignore(invoke({ type: "effort.set", runtimeId: snapshot.primaryRuntimeId, effort: event.target.value ? event.target.value as RuntimeEffort : null }).then(applyResult)); }}><option value="">Provider default</option>{liveEfforts.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
          <label>Permissions<select disabled={!snapshot.primaryRuntimeId} value={snapshot.primaryRuntimeId ? snapshot.permissionModes[snapshot.primaryRuntimeId] ?? "standard" : "standard"} onChange={(event) => { if (snapshot.primaryRuntimeId) ignore(invoke({ type: "permission.set", runtimeId: snapshot.primaryRuntimeId, mode: event.target.value as "safe" | "standard" | "unrestricted" }).then(applyResult)); }}><option value="safe">Safe · read only</option><option value="standard">Standard · ask</option><option value="unrestricted">Unrestricted · explicit</option></select></label>
          <label>Reviewer mode<select value={snapshot.reviewerMode} onChange={(event) => ignore(invoke({ type: "reviewer.mode", mode: event.target.value as "off" | "manual" | "after-turn" }).then(applyResult))}><option value="off">Off</option><option value="manual">Manual</option><option value="after-turn">After every turn</option></select></label>
        </section>
        <section className="inspector-section compact-info"><div><Cpu size={15} /><span>Runtime-neutral core</span></div><div><ShieldCheck size={15} /><span>Credentials stay provider-owned</span></div><div><Terminal size={15} /><span>Type <code>/help</code> for every control</span></div></section>
      </aside> : null}

      {runtimeManagerOpen ? <RuntimeManagerDialog
        runtimes={bootstrap.runtimes}
        diagnostics={diagnostics}
        primaryRuntimeId={snapshot.primaryRuntimeId}
        reviewerRuntimeIds={snapshot.reviewerRuntimeIds}
        close={() => setRuntimeManagerOpen(false)}
        refresh={() => ignore(refreshDiagnostics())}
        choosePrimary={(runtimeId) => ignore(choosePrimary(runtimeId))}
        toggleReviewer={(runtimeId, reviewer) => ignore(invoke({ type: reviewer ? "reviewer.remove" : "reviewer.add", runtimeId }).then(applyResult))}
        connect={openConnect}
        disconnect={(runtimeId) => ignore(invoke({ type: "runtime.logout", runtimeId }).then(() => refreshDiagnostics()))}
      /> : null}
      {connectDialog ? <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && setConnectDialog(null)}><form className="modal" role="dialog" aria-modal="true" aria-labelledby="connect-runtime-title" onSubmit={(event) => { event.preventDefault(); ignore(connect()); }}><button type="button" className="modal-close" aria-label="Close connection dialog" onClick={() => setConnectDialog(null)}><X size={17} /></button><span className="modal-icon"><KeyRound size={22} /></span><h2 id="connect-runtime-title">Connect {runtimeName(bootstrap.runtimes, connectDialog.runtimeId)}</h2><p>Authentication remains owned by the provider. Studio only stores API keys using your operating system's encrypted credential service.</p><label>Method<select value={connectDialog.method} onChange={(event) => setConnectDialog({ ...connectDialog, method: event.target.value as LoginMethod })}>{loginMethods(bootstrap.runtimes.find((runtime) => runtime.id === connectDialog.runtimeId)).map((method) => <option key={method} value={method}>{methodLabel(method)}</option>)}</select></label>{connectDialog.method === "api-key" ? <label>API key<input autoFocus type="password" autoComplete="off" value={secret} onChange={(event) => setSecret(event.target.value)} placeholder="Stored encrypted; never shown again" /></label> : null}<div className="modal-actions"><button type="button" onClick={() => setConnectDialog(null)}>Cancel</button><button className="primary-action" type="submit" disabled={connectDialog.method === "api-key" && !secret}>Continue securely</button></div></form></div> : null}
      {decisionBoard ? <DecisionBoardModal board={decisionBoard} close={() => setDecisionBoard(null)} submit={async (answers) => {
        const result = await invoke({ type: "decision.submit", url: decisionBoard.url, answers });
        if (result.type !== "decision-submitted") throw new Error("Decision Board returned an unexpected submission response");
        setDecisionBoard(null);
      }} /> : null}
    </div>
  );
}

function RuntimeManagerDialog({
  runtimes,
  diagnostics,
  primaryRuntimeId,
  reviewerRuntimeIds,
  close,
  refresh,
  choosePrimary,
  toggleReviewer,
  connect,
  disconnect,
}: {
  readonly runtimes: readonly RuntimeDescriptor[];
  readonly diagnostics: Readonly<Record<string, RuntimeDiagnostic>>;
  readonly primaryRuntimeId: string | null;
  readonly reviewerRuntimeIds: readonly string[];
  readonly close: () => void;
  readonly refresh: () => void;
  readonly choosePrimary: (runtimeId: string) => void;
  readonly toggleReviewer: (runtimeId: string, reviewer: boolean) => void;
  readonly connect: (runtimeId: string) => void;
  readonly disconnect: (runtimeId: string) => void;
}): React.JSX.Element {
  return <div className="modal-backdrop runtime-manager-backdrop" onMouseDown={(event) => event.target === event.currentTarget && close()}>
    <section className="modal runtime-manager-modal" role="dialog" aria-modal="true" aria-labelledby="runtime-manager-title">
      <button type="button" className="modal-close" aria-label="Close Runtime Center" onClick={close}><X size={17} /></button>
      <header className="runtime-manager-header">
        <span className="modal-icon"><Cpu size={21} /></span>
        <div><p className="eyebrow">AGENT CONTROL</p><h2 id="runtime-manager-title">Runtime Center</h2><p>Switch the primary, assign reviewers, and manage provider accounts without keeping runtime infrastructure in the inspector.</p></div>
        <button type="button" className="runtime-manager-refresh" onClick={refresh}><RefreshCw size={14} /> Refresh status</button>
      </header>
      <div className="runtime-manager-grid">
        {runtimes.map((runtime) => {
          const diagnostic = diagnostics[runtime.id];
          const connected = diagnostic?.auth.state === "connected";
          const isPrimary = runtime.id === primaryRuntimeId;
          const reviewer = reviewerRuntimeIds.includes(runtime.id);
          const canChat = runtime.capabilities.includes("chat");
          const canReview = runtime.capabilities.includes("review");
          const hasKeep = runtime.capabilities.includes("firekeep-memory");
          return <article className={`runtime-card ${isPrimary ? "primary" : ""}`} key={runtime.id} style={{ "--runtime-accent": runtime.accent ?? "#df7e45" } as React.CSSProperties}>
            <div className="runtime-card-top"><span className="runtime-orb">{runtime.displayName[0]}</span><div><strong>{runtime.displayName}</strong><small>{runtime.transport}</small></div><span className={`status-pill ${diagnostic?.connection.state ?? "loading"}`}>{diagnostic?.connection.state ?? "checking"}</span></div>
            <p>{diagnostic?.auth.label ?? diagnostic?.auth.detail ?? runtime.description}</p>
            <div className={`keep-capability ${hasKeep ? "connected" : "absent"}`}>
              {hasKeep ? <ShieldCheck size={11} /> : <AlertTriangle size={11} />}
              <span>{hasKeep ? `Keep memory${runtime.capabilities.includes("firekeep-hooks") ? " + automatic hooks" : " · recall on demand"}` : "Provider direct · no Keep memory"}</span>
            </div>
            <div className="runtime-card-actions">
              {canChat ? <button className={`runtime-use ${isPrimary ? "active" : ""}`} aria-label={isPrimary ? `${runtime.displayName} is in use` : `Use ${runtime.displayName} as primary`} aria-pressed={isPrimary} disabled={isPrimary} onClick={() => choosePrimary(runtime.id)}>{isPrimary ? <><CheckCircle2 size={12} /> In use</> : "Use"}</button> : null}
              {canReview ? <button className={reviewer ? "active" : ""} aria-label={reviewer ? `Remove ${runtime.displayName} reviewer` : `Add ${runtime.displayName} as reviewer`} aria-pressed={reviewer} onClick={() => toggleReviewer(runtime.id, reviewer)}>{reviewer ? "Reviewing" : "Review"}</button> : null}
              {connected ? <button aria-label={`Disconnect ${runtime.displayName}`} title="Disconnect account" onClick={() => disconnect(runtime.id)}><LogOut size={13} /></button> : <button aria-label={`Connect ${runtime.displayName}`} onClick={() => connect(runtime.id)}><KeyRound size={13} /> Connect</button>}
            </div>
          </article>;
        })}
      </div>
      <footer className="runtime-manager-footer"><Terminal size={13} /><span>Model, reasoning, and permission settings follow each selected runtime. Use Turn controls or <code>/model</code>, <code>/effort</code>, and <code>/permissions</code>.</span></footer>
    </section>
  </div>;
}

function MissionPanel({
  mission,
  workspacePath,
  primaryRuntimeId,
  busy,
  onAction,
  onCommand,
}: {
  readonly mission: MissionSnapshot | null;
  readonly workspacePath: string | null;
  readonly primaryRuntimeId: string | null;
  readonly busy: boolean;
  readonly onAction: (action: StudioAction) => void;
  readonly onCommand: (value: string) => void;
}): React.JSX.Element {
  if (!mission) {
    return <section className="inspector-section mission-section">
      <div className="section-heading"><span>Mission</span><span className="status-pill">idle</span></div>
      <div className="mission-empty"><ShieldCheck size={20} /><strong>Give an agent a goal and verify the result.</strong><p>Set the checks, choose the primary agent, and keep a clear record of what passed, what failed, and who reviewed it.</p><button disabled={busy} onClick={() => onCommand('/mission new "')}>Start a mission</button></div>
    </section>;
  }
  const latestChecks = new Map<string, MissionSnapshot["checkReceipts"][number]>();
  for (const receipt of mission.checkReceipts) latestChecks.set(receipt.checkId, receipt);
  const passed = mission.checks.filter((check) => latestChecks.get(check.id)?.passed).length;
  const cancellable = !["succeeded", "partial", "failed", "cancelled"].includes(mission.phase);
  const effectiveWorkspace = workspacePath;
  const effectivePrimary = mission.primaryRuntimeId ?? primaryRuntimeId;
  const runBlocker = mission.phase !== "draft"
    ? null
    : !effectiveWorkspace
      ? "Choose an explicit workspace before running"
      : !effectivePrimary
        ? "Choose a mission primary before running"
        : mission.checks.length === 0
          ? "Add at least one deterministic check before running"
          : null;
  return <section className="inspector-section mission-section">
    <div className="section-heading"><span>Mission</span><span className={`status-pill mission-${mission.phase}`}>{mission.phase}</span></div>
    <article className="mission-card">
      <strong>{mission.goal}</strong>
      <p>{effectivePrimary ?? "No primary"} · attempt {mission.attempt || "—"} · {passed}/{mission.checks.length} checks passed</p>
      <div className="mission-progress"><span style={{ width: `${mission.checks.length ? (passed / mission.checks.length) * 100 : 0}%` }} /></div>
      {mission.blockReason ? <small className="mission-block">{mission.blockReason}</small> : null}
      {runBlocker ? <small className="mission-block">{runBlocker === "Add at least one deterministic check before running" ? "Add a deterministic check before running." : `${runBlocker}.`}</small> : null}
      {mission.outcome ? <small className="mission-outcome">{mission.outcome.taskResult} · {mission.outcome.taskResultSource}</small> : <small>Task result · unknown</small>}
      <div className="mission-actions">
        {mission.phase === "draft" ? <button disabled={busy || Boolean(runBlocker)} title={runBlocker ?? "Run mission"} className="primary-action" onClick={() => onAction({ type: "mission.run" })}>Run</button> : null}
        {mission.phase === "draft" && mission.checks.length === 0 ? <button disabled={busy} onClick={() => onCommand("/mission check add -- ")}>Add check</button> : null}
        {mission.phase === "paused" ? <button disabled={busy} className="primary-action" onClick={() => onAction({ type: "mission.continue" })}>Continue</button> : null}
        {mission.phase === "awaiting-approval" ? <button disabled={busy} className="primary-action" onClick={() => onAction({ type: "mission.complete", taskResult: "success" })}>Approve</button> : null}
        {mission.phase === "awaiting-approval" && mission.attempt - 1 < mission.maxRepairAttempts ? <button disabled={busy} onClick={() => onCommand('/mission repair --note "')}>Repair</button> : null}
        {cancellable ? <button className="danger-action" onClick={() => onAction({ type: "mission.cancel" })}>Cancel</button> : null}
        <button onClick={() => onCommand(mission.phase === "draft" ? "/mission status" : "/mission report")}>{mission.phase === "draft" ? "Inspect" : "Report"}</button>
      </div>
    </article>
  </section>;
}

function LaunchScreen({ error }: { readonly error: string | null }): React.JSX.Element {
  return <main className="launch-shell"><section className="launch-card" aria-live="polite"><span className="brand-mark"><FirekeepMark size={30} /></span><p className="eyebrow">THE KEEP IS WAKING</p><h1>Firekeep Studio</h1><p className="lede">One calm console for every agent you trust.</p><div className="launch-status"><span className="pulse-dot" /> Loading runtime core…</div>{error ? <p className="launch-error">{error}</p> : null}</section></main>;
}

function StudioUpdateButton({ state, onAction }: { readonly state: StudioUpdateState; readonly onAction: () => void }): React.JSX.Element {
  const busy = state.phase === "checking" || state.phase === "downloading";
  const version = state.availableVersion ?? state.currentVersion;
  const label = state.phase === "ready"
    ? `Restart to install Firekeep Studio ${version}`
    : state.phase === "available"
      ? `Download Firekeep Studio ${version}`
      : state.phase === "downloading"
        ? `Downloading Firekeep Studio ${version}, ${state.progressPercent ?? 0} percent`
        : state.phase === "checking"
          ? "Checking for Studio updates"
          : state.phase === "error"
            ? `Retry Studio update check: ${state.detail}`
            : `Check for Studio updates. ${state.detail}`;
  const text = state.phase === "ready"
    ? "Restart to update"
    : state.phase === "available"
      ? `Get ${version}`
      : state.phase === "downloading"
        ? `${state.progressPercent ?? 0}%`
        : null;
  const icon = state.phase === "ready"
    ? <Sparkles size={14} />
    : state.phase === "available" || state.phase === "downloading"
      ? <Download className={state.phase === "downloading" ? "update-download" : undefined} size={14} />
      : state.phase === "error"
        ? <AlertTriangle size={14} />
        : state.phase === "current"
          ? <CheckCircle2 size={14} />
          : <RefreshCw className={state.phase === "checking" ? "spin" : undefined} size={14} />;
  return <button type="button" className={`studio-update-button ${state.phase}`} aria-label={label} title={state.detail} disabled={busy} onClick={onAction}>{icon}{text ? <span>{text}</span> : null}</button>;
}

function Welcome({ runtimes, workspacePath, onWorkspace, onChoose, onCommand }: { readonly runtimes: readonly RuntimeDescriptor[]; readonly workspacePath: string | null; readonly onWorkspace: () => void; readonly onChoose: (id: string) => void; readonly onCommand: (value: string) => void }): React.JSX.Element {
  return <section className="welcome"><span className="welcome-mark"><FirekeepMark size={30} /></span><p className="eyebrow">FIREKEEP STUDIO</p><h1 aria-label="Agents come and go. The Keep stays."><span>Agents come and go.</span><span>The Keep stays.</span></h1><p>Choose any runtime to lead, bring in another to review, and keep the work, context, and handoffs together.</p><button className="welcome-workspace" onClick={onWorkspace}><FolderOpen size={16} /><span>{workspacePath ? pathLeaf(workspacePath) : "Choose a workspace"}</span></button><div className="runtime-choices">{runtimes.map((runtime) => <button key={runtime.id} onClick={() => onChoose(runtime.id)} style={{ "--runtime-accent": runtime.accent ?? "#df7e45" } as React.CSSProperties}><span className="runtime-orb">{runtime.displayName[0]}</span><span><strong>{runtime.displayName}</strong><small>{runtime.transport}</small></span></button>)}</div><div className="quick-commands"><button onClick={() => onCommand('/mission new "')}>/mission</button><button onClick={() => onCommand("/workspace choose")}>/workspace</button><button onClick={() => onCommand("/doctor")}>/doctor</button><button onClick={() => onCommand("/reviewer add ")}>/reviewer</button><button onClick={() => onCommand('/compare --prompt "')}>/compare</button><button onClick={() => onCommand("/firekeep status")}>/firekeep</button><button onClick={() => onCommand("/help")}>/help</button></div></section>;
}

function SessionEditor({ session, saving, close, save }: { readonly session: StudioSessionSummary; readonly saving: boolean; readonly close: () => void; readonly save: (name: string, color: SessionColor) => Promise<void> }): React.JSX.Element {
  const [name, setName] = useState(session.name);
  const [color, setColor] = useState<SessionColor>(session.color ?? DEFAULT_SESSION_COLOR);
  const cleanName = name.trim();
  return (
    <form className="session-editor" aria-label={`Edit ${session.name}`} onSubmit={(event) => { event.preventDefault(); if (cleanName && !saving) ignore(save(cleanName, color)); }}>
      <label><span>Session name</span><input aria-label="Session name" autoFocus maxLength={120} value={name} onChange={(event) => setName(event.target.value)} /></label>
      <fieldset><legend>Color</legend><div className="session-colors" role="radiogroup" aria-label="Session color">{SESSION_COLOR_OPTIONS.map((option) => <button key={option.id} type="button" role="radio" aria-label={option.label} aria-checked={color === option.id} className={color === option.id ? "selected" : ""} style={{ "--choice-color": option.value } as React.CSSProperties} onClick={() => setColor(option.id)}><span /></button>)}</div></fieldset>
      <div className="session-editor-actions"><button type="button" onClick={close}>Cancel</button><button type="submit" className="primary" aria-label="Save session" disabled={!cleanName || saving}>{saving ? "Saving…" : "Save"}</button></div>
    </form>
  );
}

interface TimelineActions {
  readonly resolveApproval: (id: string, decision: string) => void;
  readonly openDecision: (url: string) => void;
  readonly decisionLoading: string | null;
}

function RunTimeline({ run, runtime, resolveApproval, openDecision, decisionLoading }: { readonly run: TimelineRun; readonly runtime: RuntimeDescriptor | undefined } & TimelineActions): React.JSX.Element {
  const hasFinalAnswer = run.messages.some((item) => item.role !== "user" && item.complete);
  const [activityOpen, setActivityOpen] = useState(!hasFinalAnswer);
  const previousFinal = useRef(hasFinalAnswer);
  useEffect(() => {
    if (hasFinalAnswer && !previousFinal.current) setActivityOpen(false);
    previousFinal.current = hasFinalAnswer;
  }, [hasFinalAnswer]);
  const steps = run.activity.length;
  const working = run.activity.some((item) => item.kind === "tool" && item.status === "running");
  return <section className="run-timeline" data-run-id={run.runId}>
    {run.messages.map((item) => <SafeTimelineCard key={`${item.kind}:${item.id}`} item={item} runtime={runtime} resolveApproval={resolveApproval} openDecision={openDecision} decisionLoading={decisionLoading} />)}
    {run.attention.map((item) => <SafeTimelineCard key={`${item.kind}:${item.id}`} item={item} runtime={runtime} resolveApproval={resolveApproval} openDecision={openDecision} decisionLoading={decisionLoading} />)}
    {steps ? <details className="run-activity" open={activityOpen} onToggle={(event) => setActivityOpen(event.currentTarget.open)}><summary><span className={working ? "pulse-dot" : "work-log-dot"} /><span>{working ? "Working" : "Work log"} · {steps} step{steps === 1 ? "" : "s"}</span><ChevronDown size={14} /></summary><div className="run-activity-items">{run.activity.map((item) => <SafeTimelineCard key={`${item.kind}:${item.id}`} item={item} runtime={runtime} resolveApproval={resolveApproval} openDecision={openDecision} decisionLoading={decisionLoading} />)}</div></details> : null}
  </section>;
}

function SafeTimelineCard(props: { readonly item: TimelineItem; readonly runtime: RuntimeDescriptor | undefined } & TimelineActions): React.JSX.Element {
  const resetKey = props.item.kind === "message" ? props.item.text : props.item.id;
  return <RenderBoundary resetKey={resetKey}><TimelineCard {...props} /></RenderBoundary>;
}

interface AgentGridProps extends TimelineActions {
  readonly runtimes: readonly RuntimeDescriptor[];
  readonly runs: readonly TimelineRun[];
  readonly activeRuntimeId: string | null;
  readonly focusedRuntimeId: string | null;
  readonly primaryRuntimeId: string | null;
  readonly workingRuntimeId: string | null;
  readonly diagnostics: Readonly<Record<string, RuntimeDiagnostic>>;
  readonly commandCards: readonly CommandCard[];
  readonly error: string | null;
  readonly select: (runtimeId: string) => void;
  readonly close: (runtimeId: string) => void;
  readonly focus: (runtimeId: string) => void;
  readonly makePrimary: (runtimeId: string) => void;
  readonly clearError: () => void;
}

function AgentGrid(props: AgentGridProps): React.JSX.Element {
  const visible = props.focusedRuntimeId ? props.runtimes.filter((runtime) => runtime.id === props.focusedRuntimeId) : props.runtimes;
  if (!visible.length) return <div className="agent-grid-empty"><PanelsTopLeft size={22} /><strong>No agent panes open</strong><span>Add a runtime from the toolbar.</span></div>;
  return <div className={`agent-grid ${props.focusedRuntimeId ? "focused" : ""}`} data-count={visible.length} aria-label="Agent grid">{visible.map((runtime) => <AgentPane key={runtime.id} {...props} runtime={runtime} runs={props.runs.filter((run) => run.runtimeId === runtime.id)} canClose={props.runtimes.length > 1} />)}</div>;
}

function AgentPane({ runtime, runs, activeRuntimeId, focusedRuntimeId, primaryRuntimeId, workingRuntimeId, diagnostics, commandCards, error, select, close, focus, makePrimary, clearError, resolveApproval, openDecision, decisionLoading, canClose }: AgentGridProps & { readonly runtime: RuntimeDescriptor; readonly runs: readonly TimelineRun[]; readonly canClose: boolean }): React.JSX.Element {
  const scrollRef = useRef<HTMLDivElement>(null);
  const followingRef = useRef(true);
  useEffect(() => {
    const node = scrollRef.current;
    if (node && followingRef.current) node.scrollTo({ top: node.scrollHeight, behavior: "auto" });
  }, [runs]);
  const selected = activeRuntimeId === runtime.id;
  const focused = focusedRuntimeId === runtime.id;
  const working = workingRuntimeId === runtime.id;
  const connection = diagnostics[runtime.id]?.connection;
  const status = working ? "Working" : connection?.state === "ready" ? "Ready" : connection?.detail ?? "Checking";
  return <section role="region" aria-label={`${runtime.displayName} agent pane`} tabIndex={0} className={`agent-pane ${selected ? "active" : ""}`} style={{ "--runtime-accent": runtime.accent ?? "#df7e45" } as React.CSSProperties} onClick={() => select(runtime.id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(runtime.id); } }}>
    <header className="agent-pane-header"><span className="runtime-orb">{runtime.displayName[0]}</span><div><strong>{runtime.displayName}</strong><small>{runtime.id === primaryRuntimeId ? "Primary" : "Agent pane"} · {status}</small></div><span className={`pane-status ${working ? "working" : ""}`} />{runtime.id !== primaryRuntimeId ? <button type="button" title={`Use ${runtime.displayName} as primary`} onClick={(event) => { event.stopPropagation(); makePrimary(runtime.id); }}>Use</button> : null}<button type="button" aria-label={focused ? `Restore ${runtime.displayName} pane` : `Focus ${runtime.displayName} pane`} title={focused ? "Restore grid" : "Focus pane"} onClick={(event) => { event.stopPropagation(); focus(runtime.id); }}>{focused ? <Minimize2 size={13} /> : <Maximize2 size={13} />}</button>{canClose ? <button type="button" aria-label={`Close ${runtime.displayName} pane`} title="Close pane" onClick={(event) => { event.stopPropagation(); close(runtime.id); }}><X size={13} /></button> : null}</header>
    <div className="agent-pane-scroll" ref={scrollRef} onScroll={(event) => { const node = event.currentTarget; followingRef.current = node.scrollHeight - node.scrollTop - node.clientHeight <= 60; }}>
      {runs.length ? runs.map((run) => <RunTimeline key={run.id} run={run} runtime={runtime} resolveApproval={resolveApproval} openDecision={openDecision} decisionLoading={decisionLoading} />) : <div className="agent-pane-empty"><Sparkles size={18} /><strong>Ready for a direct turn</strong><span>Select this pane and use the shared composer below.</span></div>}
      {selected ? commandCards.map((card) => <CommandResultCard key={card.id} card={card} />) : null}
      {selected && error ? <div className="error-banner"><AlertTriangle size={16} /><span>{error}</span><button onClick={(event) => { event.stopPropagation(); clearError(); }}><X size={14} /></button></div> : null}
    </div>
  </section>;
}

function TimelineCard({ item, runtime, resolveApproval, openDecision, decisionLoading }: { readonly item: TimelineItem; readonly runtime: RuntimeDescriptor | undefined; readonly resolveApproval: (id: string, decision: string) => void; readonly openDecision: (url: string) => void; readonly decisionLoading: string | null }): React.JSX.Element | null {
  const name = runtime?.displayName ?? item.runtimeId;
  if (item.kind === "message") return <article className={`message-card ${item.role}`}><header><span className="runtime-orb" style={{ "--runtime-accent": runtime?.accent ?? "#df7e45" } as React.CSSProperties}>{item.role === "user" ? "Y" : name[0]}</span><div><strong>{item.role === "user" ? "You" : item.role === "reviewer" ? `${name} review` : name}</strong><small>{formatTime(item.timestamp)}{item.complete ? "" : " · streaming"}</small></div><CopyButton text={item.text} /></header><div className="markdown"><RichMarkdown>{item.text}</RichMarkdown></div></article>;
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

function CommandResultCard({ card }: { readonly card: CommandCard }): React.JSX.Element {
  return <article className={`command-card ${card.result.tone ?? "neutral"}`}><header><Code2 size={15} /><code>{card.input}</code><span>{card.result.title}</span></header>{card.result.kind === "table" && card.result.rows ? <div className="table-scroll"><table><tbody>{card.result.rows.map((row, index) => <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody></table></div> : <div className="markdown"><RichMarkdown>{card.result.body}</RichMarkdown></div>}</article>;
}

function CopyButton({ text }: { readonly text: string }): React.JSX.Element {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const copy = async (): Promise<void> => {
    try {
      const result = await window.firekeepStudio.invoke({ type: "clipboard.write", text });
      if (result.type !== "clipboard-written") throw new Error("clipboard returned an unexpected response");
      setState("copied");
      window.setTimeout(() => setState("idle"), 1_200);
    } catch {
      setState("failed");
      window.setTimeout(() => setState("idle"), 1_800);
    }
  };
  return <button type="button" className={`copy-button ${state}`} aria-label="Copy response" title={state === "failed" ? "Copy failed" : state === "copied" ? "Copied" : "Copy response"} onClick={() => ignore(copy())}>{state === "copied" ? <CheckCircle2 size={14} /> : <Copy size={14} />}</button>;
}

function sessionAccentStyle(color: SessionColor = DEFAULT_SESSION_COLOR): React.CSSProperties {
  return { "--session-accent": SESSION_COLOR_OPTIONS.find((option) => option.id === color)?.value ?? SESSION_COLOR_OPTIONS[0]!.value } as React.CSSProperties;
}
function nextTheme(theme: StudioSnapshot["theme"]): StudioSnapshot["theme"] { return theme === "system" ? "dark" : theme === "dark" ? "light" : "system"; }
function themeLabel(theme: StudioSnapshot["theme"]): string { return theme === "system" ? "System" : theme === "dark" ? "Dark" : "Light"; }
function runtimeName(runtimes: readonly RuntimeDescriptor[], id: string): string { return runtimes.find((runtime) => runtime.id === id)?.displayName ?? id; }
function pretty(value: unknown): string { return typeof value === "string" ? value : JSON.stringify(value, null, 2); }
function pathLeaf(value: string): string { return value.replace(/[\\/]+$/, "").split(/[\\/]/).at(-1) || value; }
function formatTime(value: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
function relativeTime(value: string): string { const delta = Date.now() - new Date(value).getTime(); if (!Number.isFinite(delta) || delta < 60_000) return "now"; if (delta < 3_600_000) return `${Math.floor(delta / 60_000)}m`; if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)}h`; return `${Math.floor(delta / 86_400_000)}d`; }
function formatUsage(usage: RuntimeUsage): string { const pieces = [usage.inputTokens !== undefined ? `${usage.inputTokens.toLocaleString()} in` : "", usage.cacheCreationInputTokens !== undefined ? `${usage.cacheCreationInputTokens.toLocaleString()} cache write` : "", usage.cachedInputTokens !== undefined ? `${usage.cachedInputTokens.toLocaleString()} cached` : "", usage.outputTokens !== undefined ? `${usage.outputTokens.toLocaleString()} out` : "", usage.reasoningTokens !== undefined ? `${usage.reasoningTokens.toLocaleString()} reasoning` : "", usage.costUsd !== undefined ? `$${usage.costUsd.toFixed(4)}` : "", usage.durationMs !== undefined ? `${(usage.durationMs / 1_000).toFixed(1)}s` : ""].filter(Boolean); return pieces.join(" · ") || `${usage.totalTokens?.toLocaleString() ?? 0} tokens`; }
function stripMarkdown(value: string): string { return value.replace(/```[\s\S]*?```/g, " code block ").replace(/[`*_>#\[\]()~-]/g, " ").replace(/\s+/g, " ").trim(); }
function loginMethods(runtime: RuntimeDescriptor | undefined): readonly LoginMethod[] { return runtime?.loginMethods?.length ? runtime.loginMethods : ["browser"]; }
function methodLabel(method: LoginMethod): string { return method === "api-key" ? "API key" : method === "device" ? "Device code" : method[0]?.toUpperCase() + method.slice(1); }
function ignore(promise: Promise<unknown>): void { void promise.catch(() => undefined); }
