import { useEffect, useRef, useState, type ReactNode } from "react";
import { AlertTriangle, CheckCircle2, ChevronDown, Download, FolderOpen, RefreshCw, Settings, Sparkles } from "lucide-react";
import type { StudioUpdateState } from "../../../shared/ipc";
import { FirekeepMark } from "../FirekeepMark.js";

export function pathLeaf(value: string): string { return value.replace(/[\\/]+$/, "").split(/[\\/]/).at(-1) || value; }

export function TopBar({ workspacePath, recentWorkspaces, onChooseFolder, onSwitchWorkspace, onOpenSettings, update, onUpdate, agentWorking = false, center, actions }: {
  readonly workspacePath: string | null;
  readonly recentWorkspaces: readonly string[];
  readonly onChooseFolder: () => void;
  readonly onSwitchWorkspace: (path: string) => void;
  readonly onOpenSettings: () => void;
  readonly update: StudioUpdateState | null;
  readonly onUpdate: () => void;
  readonly agentWorking?: boolean;
  readonly center?: ReactNode;
  readonly actions?: ReactNode;
}): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const pickerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent | KeyboardEvent): void => {
      if (event instanceof KeyboardEvent) { if (event.key === "Escape") setOpen(false); return; }
      if (!pickerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", close);
    return () => { window.removeEventListener("mousedown", close); window.removeEventListener("keydown", close); };
  }, [open]);
  const label = workspacePath ? pathLeaf(workspacePath) : "Home";
  return (
    <header className="titlebar">
      <div className="brand"><span className="brand-icon" role="status" aria-label={agentWorking ? "Agent working" : "Firekeep Studio idle"}><FirekeepMark size={18} />{agentWorking ? <span className="brand-activity" /> : null}</span><span>Firekeep</span><strong>Studio</strong></div>
      <div className="titlebar-center">
        <div className="repo-picker-wrap" ref={pickerRef}>
          <button type="button" className="repo-picker" aria-label={`Workspace: ${label}`} aria-haspopup="menu" aria-expanded={open} title={workspacePath ?? "No workspace chosen"} onClick={() => setOpen((value) => !value)}>
            <FolderOpen size={14} /><span>{label}</span><ChevronDown size={12} />
          </button>
          {open ? (
            <div className="repo-menu" role="menu" aria-label="Workspaces">
              {recentWorkspaces.map((path) => (
                <button key={path} type="button" role="menuitem" className={path === workspacePath ? "current" : undefined} title={path} onClick={() => { setOpen(false); if (path !== workspacePath) onSwitchWorkspace(path); }}>
                  <span>{pathLeaf(path)}</span><small>{path}</small>
                </button>
              ))}
              <button type="button" role="menuitem" className="repo-menu-open" onClick={() => { setOpen(false); onChooseFolder(); }}>Open folder…</button>
            </div>
          ) : null}
        </div>
        {center}
      </div>
      <div className="titlebar-actions">
        {update && update.phase !== "disabled" ? <StudioUpdateButton state={update} onAction={onUpdate} /> : null}
        {actions}
        <button type="button" className="icon-button" aria-label="Settings" title="Settings" onClick={onOpenSettings}><Settings size={17} /></button>
      </div>
    </header>
  );
}

export function StudioUpdateButton({ state, onAction }: { readonly state: StudioUpdateState; readonly onAction: () => void }): React.JSX.Element {
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
