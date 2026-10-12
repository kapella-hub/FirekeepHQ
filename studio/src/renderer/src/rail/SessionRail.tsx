import { useState } from "react";
import { AlertTriangle, Palette, Plus, Sparkles } from "lucide-react";
import { DEFAULT_SESSION_COLOR, type SessionColor, type StudioSessionSummary } from "../../../core/session-store";
import { SESSION_COLOR_OPTIONS, relativeTime, sessionAccentStyle } from "./session-style.js";

export function SessionRail({ sessions, activeSessionId, onNew, onResume, onEdit, keepWarning, onKeepWarning, editingId, saving, onToggleEdit, onCloseEdit }: {
  readonly sessions: readonly StudioSessionSummary[];
  readonly activeSessionId: string;
  readonly onNew: () => void;
  readonly onResume: (sessionId: string) => void;
  readonly onEdit: (sessionId: string, name: string, color: SessionColor) => Promise<void>;
  /** One line shown at the bottom when the Keep is not connected; null hides the row. */
  readonly keepWarning: string | null;
  readonly onKeepWarning: () => void;
  readonly editingId: string | null;
  readonly saving: boolean;
  readonly onToggleEdit: (sessionId: string) => void;
  readonly onCloseEdit: () => void;
}): React.JSX.Element {
  return (
    <aside className="session-rail">
      <div className="rail-heading"><span>Sessions</span><button className="icon-button compact" title="New session" aria-label="New session" onClick={onNew}><Plus size={15} /></button></div>
      <nav className="session-list" aria-label="Studio sessions">
        {sessions.map((session) => (
          <div key={session.id} className={`session-entry ${session.id === activeSessionId ? "active" : ""} ${editingId === session.id ? "editing" : ""}`} style={sessionAccentStyle(session.color)}>
            <button className="session-row" aria-current={session.id === activeSessionId ? "page" : undefined} onClick={() => { if (session.id !== activeSessionId) onResume(session.id); }}>
              <span className="session-row-heading"><span className="session-color-dot" /><span className="session-row-title">{session.name}</span></span>
              <span className="session-row-meta">{session.mission ? `${session.mission.phase} · ` : ""}{session.eventCount} events · {relativeTime(session.updatedAt)}</span>
            </button>
            <button type="button" className="session-customize" aria-label={`Customize ${session.name}`} aria-expanded={editingId === session.id} onClick={() => onToggleEdit(session.id)}><Palette size={13} /></button>
            {editingId === session.id ? <SessionEditor session={session} saving={saving} close={onCloseEdit} save={(name, color) => onEdit(session.id, name, color)} /> : null}
          </div>
        ))}
        {!sessions.length ? <div className="session-list-empty"><Sparkles size={17} /><span>Your next session starts here.</span></div> : null}
      </nav>
      {keepWarning ? <button type="button" className="keep-warning-row" onClick={onKeepWarning}><AlertTriangle size={13} /><span>{keepWarning}</span></button> : null}
    </aside>
  );
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

function ignore(promise: Promise<unknown>): void { void promise.catch(() => undefined); }
