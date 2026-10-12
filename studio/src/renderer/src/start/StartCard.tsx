import { FolderOpen, KeyRound } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { RuntimeDescriptor } from "../../../core/runtime.js";
import type { RuntimeDiagnostic } from "../../../core/studio-service.js";
import type { KeepHighlights, KeepStatus } from "../../../shared/keep.js";
import { FirekeepMark } from "../FirekeepMark.js";
import { RichMarkdown } from "../RichMarkdown.js";
import { pathLeaf } from "../util.js";

export interface StartCardProps {
  readonly runtimes: readonly RuntimeDescriptor[];
  readonly diagnostics: Readonly<Record<string, RuntimeDiagnostic>>;
  readonly primaryRuntimeId: string | null;
  readonly recentWorkspaces: readonly string[];
  readonly workspacePath: string | null;
  readonly highlights: KeepHighlights | null;
  readonly keepStatus: KeepStatus | null;
  readonly onPrimary: (runtimeId: string) => void;
  readonly onConnect: (runtimeId: string) => void;
  readonly onChooseFolder: () => void;
  readonly onSwitchWorkspace: (path: string) => void;
  readonly onOpenSession: (goal: string) => void;
}

function accent(runtime: RuntimeDescriptor): React.CSSProperties {
  return { "--runtime-accent": runtime.accent ?? "#df7e45" } as React.CSSProperties;
}

export function StartCard(props: StartCardProps): React.JSX.Element {
  const { runtimes, diagnostics, primaryRuntimeId, recentWorkspaces, workspacePath, highlights, keepStatus } = props;
  const [changing, setChanging] = useState(false);
  const autoRef = useRef(false);
  const chatRuntimes = runtimes.filter((runtime) => runtime.capabilities.includes("chat"));
  const probed = Object.keys(diagnostics).length > 0;
  const ready = chatRuntimes.filter((runtime) => {
    const diagnostic = diagnostics[runtime.id];
    return diagnostic?.connection.state === "ready" && diagnostic.auth.state === "connected";
  });
  const primary = runtimes.find((runtime) => runtime.id === primaryRuntimeId) ?? null;
  const soleReady = !primaryRuntimeId && ready.length === 1 ? ready[0] : undefined;

  const soleReadyId = soleReady?.id;
  const { onPrimary } = props;
  useEffect(() => {
    if (soleReadyId && !autoRef.current) {
      autoRef.current = true;
      onPrimary(soleReadyId);
    }
  }, [soleReadyId, onPrimary]);

  useEffect(() => {
    if (!changing) return;
    // Capture phase + stopImmediatePropagation: leaving the picker must not also reach App's Escape (run.cancel).
    const escape = (event: KeyboardEvent): void => {
      if (event.key === "Escape") { event.stopImmediatePropagation(); setChanging(false); }
    };
    document.addEventListener("keydown", escape, true);
    return () => document.removeEventListener("keydown", escape, true);
  }, [changing]);

  const workspaces = workspacePath && !recentWorkspaces.includes(workspacePath) ? [workspacePath, ...recentWorkspaces] : recentWorkspaces;
  const showHighlights = keepStatus?.state === "connected" && highlights;

  const cards = (list: readonly RuntimeDescriptor[], connect: boolean): React.JSX.Element => (
    <div className="start-agent-cards">{list.map((runtime) => connect
      ? <div key={runtime.id} className="start-agent-card" style={accent(runtime)}><span className="runtime-orb">{runtime.displayName[0]}</span><span><strong>{runtime.displayName}</strong><small>{diagnostics[runtime.id]?.auth.state === "connected" ? "Not reachable" : "Not connected"}</small></span><button type="button" aria-label={`Connect ${runtime.displayName}`} onClick={() => props.onConnect(runtime.id)}><KeyRound size={12} /> Connect</button></div>
      : <div key={runtime.id} role="button" tabIndex={0} aria-label={`Use ${runtime.displayName}`} className="start-agent-card" style={accent(runtime)}
        onClick={() => { setChanging(false); props.onPrimary(runtime.id); }}
        onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setChanging(false); props.onPrimary(runtime.id); } }}>
        <span className="runtime-orb">{runtime.displayName[0]}</span><span><strong>{runtime.displayName}</strong><small>{runtime.transport}</small></span>
      </div>)}</div>
  );

  let agentRow: React.JSX.Element | null = null;
  const current = primary ?? soleReady ?? null;
  const canChange = ready.some((runtime) => runtime.id !== current?.id);
  if (current && !changing) {
    agentRow = <p className="start-using">Using {current.displayName}{canChange ? <> · <button type="button" className="link-button" onClick={() => setChanging(true)}>change</button></> : null}</p>;
  } else if (probed) {
    agentRow = ready.length > 0 ? cards(ready, false) : cards(chatRuntimes, true);
  }
  if (changing && ready.length > 0) agentRow = <>{cards(ready, false)}<p className="start-using"><button type="button" className="link-button" onClick={() => setChanging(false)}>cancel</button></p></>;

  return <section className="start-card" aria-label="Get started">
    <span className="welcome-mark"><FirekeepMark size={30} /></span>
    <h2>Start a session</h2>
    <div className="start-section"><h3>Agent</h3>{agentRow}</div>
    <div className="start-section"><h3>Workspace</h3>
      <div className="start-chips">
        {workspaces.map((path) => <button key={path} type="button" className="start-chip" aria-pressed={path === workspacePath} title={path} onClick={() => { if (path !== workspacePath) props.onSwitchWorkspace(path); }}>{pathLeaf(path)}</button>)}
        <button type="button" className="start-chip" onClick={props.onChooseFolder}><FolderOpen size={12} /> Open folder…</button>
      </div>
    </div>
    {showHighlights ? <div className="start-keep">
      {highlights.online ? <div className="start-section"><h3>Online now</h3><div className="markdown"><RichMarkdown>{highlights.online}</RichMarkdown></div></div> : null}
      {highlights.bulletin ? <div className="start-section"><h3>From the bulletin</h3><div className="markdown"><RichMarkdown>{highlights.bulletin}</RichMarkdown></div></div> : null}
      {highlights.paused.length > 0 ? <div className="start-section"><h3>Pick up where you left off</h3><ul className="start-paused">{highlights.paused.map((item) => <li key={item.sessionId}><button type="button" onClick={() => props.onOpenSession(item.goal)}>{item.goal}</button></li>)}</ul></div> : null}
    </div> : null}
  </section>;
}
