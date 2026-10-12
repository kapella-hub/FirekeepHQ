import { useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, ExternalLink, KeyRound, LogOut, RefreshCw, ShieldCheck, Terminal, X } from "lucide-react";
import type { ReviewerMode, RuntimeDiagnostic, ThemeMode } from "../../../core/studio-service.js";
import type { RuntimeDescriptor } from "../../../core/runtime.js";
import type { StudioUpdateState } from "../../../shared/ipc";
import type { KeepStatus } from "../../../shared/keep";
import { CommandResultCard, type CommandCard } from "../conversation/CommandResultCard.js";

export type SettingsSection = "agents" | "keep" | "reviews" | "appearance" | "updates";

export const SETTINGS_SECTIONS: readonly { readonly id: SettingsSection; readonly label: string }[] = [
  { id: "agents", label: "Agents & accounts" },
  { id: "keep", label: "Keep connection" },
  { id: "reviews", label: "Reviews" },
  { id: "appearance", label: "Appearance" },
  { id: "updates", label: "Studio updates" },
];

export interface SettingsSheetProps {
  readonly section: SettingsSection;
  readonly onSection: (section: SettingsSection) => void;
  readonly onClose: () => void;
  /** True while a dialog stacked above the sheet (the Connect form) owns Escape. */
  readonly suspendEscape: boolean;
  // Agents & accounts
  readonly runtimes: readonly RuntimeDescriptor[];
  readonly diagnostics: Readonly<Record<string, RuntimeDiagnostic>>;
  readonly primaryRuntimeId: string | null;
  readonly reviewerRuntimeIds: readonly string[];
  readonly refresh: () => void;
  readonly choosePrimary: (runtimeId: string) => void;
  readonly toggleReviewer: (runtimeId: string, reviewer: boolean) => void;
  readonly connect: (runtimeId: string) => void;
  readonly disconnect: (runtimeId: string) => void;
  // Keep connection
  readonly keepStatus: KeepStatus | null;
  readonly dashboardAvailable: boolean;
  /** Runs a kit slash command through command.execute and records the outcome (or the failure) in `kitCard`. */
  readonly runCommand: (input: string) => Promise<void>;
  /** The last kit command card, owned by App so it survives tab switches and closing the sheet. */
  readonly kitCard: CommandCard | null;
  readonly onOpenDashboard: () => void;
  // Reviews
  readonly reviewerMode: ReviewerMode;
  readonly onReviewerMode: (mode: ReviewerMode) => void;
  // Appearance
  readonly theme: ThemeMode;
  readonly onTheme: (theme: ThemeMode) => void;
  // Studio updates
  readonly update: StudioUpdateState | null;
  readonly onUpdate: () => void;
}

export function SettingsSheet(props: SettingsSheetProps): React.JSX.Element {
  const { section, onSection, onClose, suspendEscape } = props;
  const sheetRef = useRef<HTMLElement>(null);
  const suspendRef = useRef(suspendEscape);
  suspendRef.current = suspendEscape;

  useEffect(() => {
    const escape = (event: KeyboardEvent): void => {
      // Capture phase + stopImmediatePropagation: closing the sheet must not also reach App's Escape (run.cancel).
      if (event.key === "Escape" && !suspendRef.current) { event.stopImmediatePropagation(); onClose(); }
    };
    window.addEventListener("keydown", escape, true);
    return () => window.removeEventListener("keydown", escape, true);
  }, [onClose]);

  useEffect(() => {
    sheetRef.current?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus();
    // Focus the active tab once on open; later section changes keep focus where the user put it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const trapTab = (event: React.KeyboardEvent): void => {
    if (event.key !== "Tab") return;
    const focusable = [...(sheetRef.current?.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])') ?? [])].filter((element) => !(element as HTMLButtonElement).disabled && element.tabIndex >= 0);
    const first = focusable[0];
    const last = focusable.at(-1);
    if (!first || !last) return;
    const active = document.activeElement;
    if (!sheetRef.current?.contains(active)) { event.preventDefault(); first.focus(); }
    else if (event.shiftKey && active === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && active === last) { event.preventDefault(); first.focus(); }
  };

  const moveTab = (event: React.KeyboardEvent, index: number): void => {
    const step = event.key === "ArrowDown" || event.key === "ArrowRight" ? 1 : event.key === "ArrowUp" || event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const next = SETTINGS_SECTIONS[(index + step + SETTINGS_SECTIONS.length) % SETTINGS_SECTIONS.length]!;
    onSection(next.id);
    window.requestAnimationFrame(() => sheetRef.current?.querySelector<HTMLElement>(`#settings-tab-${next.id}`)?.focus());
  };

  const label = SETTINGS_SECTIONS.find((entry) => entry.id === section)!.label;
  return <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
    <section ref={sheetRef} className="modal settings-sheet" role="dialog" aria-modal="true" aria-label="Settings" onKeyDown={trapTab}>
      <button type="button" className="modal-close" aria-label="Close Settings" onClick={onClose}><X size={17} /></button>
      <nav className="settings-nav">
        <h2>Settings</h2>
        <div role="tablist" aria-label="Settings sections" aria-orientation="vertical">
          {SETTINGS_SECTIONS.map((entry, index) => <button key={entry.id} type="button" role="tab" id={`settings-tab-${entry.id}`} aria-selected={section === entry.id} aria-controls="settings-panel" tabIndex={section === entry.id ? 0 : -1} onClick={() => onSection(entry.id)} onKeyDown={(event) => moveTab(event, index)}>{entry.label}</button>)}
        </div>
      </nav>
      <section id="settings-panel" className="settings-panel" role="region" aria-label={label}>
        {section === "agents" ? <AgentsSection {...props} />
          : section === "keep" ? <KeepSection {...props} />
          : section === "reviews" ? <ReviewsSection {...props} />
          : section === "appearance" ? <AppearanceSection {...props} />
          : <UpdatesSection {...props} />}
      </section>
    </section>
  </div>;
}

function AgentsSection({ runtimes, diagnostics, primaryRuntimeId, reviewerRuntimeIds, refresh, choosePrimary, toggleReviewer, connect, disconnect }: SettingsSheetProps): React.JSX.Element {
  return <>
    <header className="runtime-manager-header">
      <div><p className="eyebrow">AGENT CONTROL</p><h3>Agents &amp; accounts</h3><p>Switch the primary, assign reviewers, and manage provider accounts without keeping runtime infrastructure in the inspector.</p></div>
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
    <footer className="runtime-manager-footer"><Terminal size={13} /><span>Model, reasoning, and permission settings follow each selected runtime. Use the Agent chip or <code>/model</code>, <code>/effort</code>, and <code>/permissions</code>.</span></footer>
  </>;
}

function KeepSection({ keepStatus, dashboardAvailable, runCommand, kitCard, onOpenDashboard }: SettingsSheetProps): React.JSX.Element {
  const [running, setRunning] = useState<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const connected = keepStatus?.state === "connected";
  const run = async (input: string): Promise<void> => {
    if (running) return;
    setRunning(input);
    try { await runCommand(input); }
    finally { if (mounted.current) setRunning(null); }
  };
  const personal = connected && keepStatus.personalMode;
  return <>
    <header className="settings-header"><h3>Keep connection</h3><p>How this machine reaches your team's Keep. These buttons run the Firekeep kit commands for you.</p></header>
    <div className="settings-body">
      <dl className="keep-connection-facts">
        <dt>Status</dt>
        <dd>{!keepStatus || keepStatus.state === "starting" ? "Connecting…"
          : keepStatus.state === "connected" ? <span className="keep-ok"><CheckCircle2 size={13} /> Connected</span>
          : keepStatus.state === "missing" ? <span className="keep-bad"><AlertTriangle size={13} /> Kit not installed</span>
          : <span className="keep-bad"><AlertTriangle size={13} /> {keepStatus.state === "unconfigured" ? "Not configured" : "Unreachable"}</span>}</dd>
        {keepStatus && (keepStatus.state === "unconfigured" || keepStatus.state === "unreachable") ? <><dt>Detail</dt><dd>{keepStatus.detail}</dd></> : null}
        {keepStatus?.state === "connected" ? <>
          <dt>Agent</dt><dd>{keepStatus.agentId}</dd>
          <dt>Kit version</dt><dd>{keepStatus.kitVersion}</dd>
          <dt>Personal mode</dt><dd>{keepStatus.personalMode ? "On — nothing from this session is shared" : "Off"}</dd>
        </> : null}
      </dl>
      <div className="settings-actions">
        <button type="button" disabled={running !== null} onClick={() => void run("/firekeep doctor")}>Run doctor</button>
        <button type="button" disabled={running !== null || !connected} aria-pressed={personal} onClick={() => void run("/firekeep personal toggle")}>{personal ? "Turn personal mode off" : "Turn personal mode on"}</button>
        <button type="button" disabled={running !== null} onClick={() => void run("/firekeep update")}>Update kit</button>
        {dashboardAvailable ? <button type="button" onClick={onOpenDashboard}><ExternalLink size={13} /> Open dashboard</button> : null}
      </div>
      {running ? <p className="settings-running" role="status">Running <code>{running}</code>…</p> : null}
      {kitCard ? <CommandResultCard card={kitCard} /> : null}
    </div>
  </>;
}

function ReviewsSection({ runtimes, reviewerRuntimeIds, reviewerMode, onReviewerMode, toggleReviewer }: SettingsSheetProps): React.JSX.Element {
  const candidates = runtimes.filter((runtime) => runtime.capabilities.includes("review"));
  return <>
    <header className="settings-header"><h3>Reviews</h3><p>Choose when a second agent checks the primary's work, and which agents do it.</p></header>
    <div className="settings-body">
      <label className="settings-field">After each turn<select value={reviewerMode} onChange={(event) => onReviewerMode(event.target.value as ReviewerMode)}>
        <option value="off">Off</option><option value="manual">Manual</option><option value="after-turn">After every turn</option>
      </select></label>
      <h4>Default reviewers</h4>
      {candidates.length === 0 ? <p className="settings-empty">No installed agent can review.</p> : <ul className="settings-reviewers">
        {candidates.map((runtime) => {
          const reviewer = reviewerRuntimeIds.includes(runtime.id);
          return <li key={runtime.id}>
            <span className="runtime-orb" style={{ "--runtime-accent": runtime.accent ?? "#df7e45" } as React.CSSProperties}>{runtime.displayName[0]}</span>
            <strong>{runtime.displayName}</strong>
            <button type="button" className={reviewer ? "active" : ""} aria-label={reviewer ? `Remove ${runtime.displayName} reviewer` : `Add ${runtime.displayName} as reviewer`} aria-pressed={reviewer} onClick={() => toggleReviewer(runtime.id, reviewer)}>{reviewer ? "Remove" : "Add"}</button>
          </li>;
        })}
      </ul>}
    </div>
  </>;
}

const THEMES: readonly { readonly id: ThemeMode; readonly label: string }[] = [{ id: "system", label: "System" }, { id: "dark", label: "Dark" }, { id: "light", label: "Light" }];

function AppearanceSection({ theme, onTheme }: SettingsSheetProps): React.JSX.Element {
  return <>
    <header className="settings-header"><h3>Appearance</h3><p>System follows your operating system's light or dark setting.</p></header>
    <div className="settings-body">
      <div role="radiogroup" aria-label="Theme" className="settings-radios">
        {THEMES.map((entry) => <label key={entry.id}><input type="radio" name="studio-theme" value={entry.id} checked={theme === entry.id} onChange={() => onTheme(entry.id)} />{entry.label}</label>)}
      </div>
    </div>
  </>;
}

function UpdatesSection({ update, onUpdate }: SettingsSheetProps): React.JSX.Element {
  const enabled = update !== null && update.phase !== "disabled";
  const busy = update?.phase === "checking" || update?.phase === "downloading";
  const action = update?.phase === "ready" ? `Restart to install ${update.availableVersion ?? ""}`.trim()
    : update?.phase === "available" ? `Download ${update.availableVersion ?? "update"}`
    : update?.phase === "downloading" ? `Downloading… ${update.progressPercent ?? 0}%`
    : "Check now";
  return <>
    <header className="settings-header"><h3>Studio updates</h3><p>Firekeep Studio updates itself separately from the Keep kit.</p></header>
    <div className="settings-body">
      {!enabled ? <p className="settings-empty">Updates are not available in this build.</p> : <>
        <dl className="keep-connection-facts">
          <dt>Version</dt><dd>{update.currentVersion}</dd>
          {update.availableVersion ? <><dt>Available</dt><dd>{update.availableVersion}</dd></> : null}
          <dt>Status</dt><dd>{update.detail}</dd>
        </dl>
        <div className="settings-actions"><button type="button" disabled={busy} onClick={onUpdate}>{action}</button></div>
      </>}
    </div>
  </>;
}
