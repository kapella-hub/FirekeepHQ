import { Check, ChevronDown, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

export interface DoneWhenChipProps {
  /** Confirmed completion checks (shell commands); non-empty turns Send into a quick mission. */
  readonly checks: readonly string[];
  readonly onChange: (checks: readonly string[]) => void;
  /** Resolves the workspace's detected test command (workspace.detectTest), or null. */
  readonly detect: () => Promise<string | null>;
}

const MAX_CHECKS = 8;

export function DoneWhenChip({ checks, onChange, detect }: DoneWhenChipProps): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [moreOpen, setMoreOpen] = useState(false);
  const [label, setLabel] = useState("");
  const [timeout, setTimeoutValue] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const summary = checks.length === 0 ? "Done when…" : `Done when: ${checks[0]} ✓${checks.length > 1 ? ` +${checks.length - 1}` : ""}`;

  useEffect(() => {
    if (!open) return undefined;
    const close = (event: PointerEvent): void => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent): void => {
      // Capture phase + stopImmediatePropagation: closing the popover must not also reach App's Escape (run.cancel).
      if (event.key === "Escape") { event.stopImmediatePropagation(); setOpen(false); triggerRef.current?.focus(); }
    };
    document.addEventListener("pointerdown", close);
    window.addEventListener("keydown", escape, true);
    return () => { document.removeEventListener("pointerdown", close); window.removeEventListener("keydown", escape, true); };
  }, [open]);

  const toggle = (): void => {
    const next = !open;
    setOpen(next);
    if (!next) return;
    setDraft("");
    detect().then((command) => { if (command) setDraft((value) => value || command); }, () => undefined);
  };

  const add = (): void => {
    const command = draft.trim();
    if (!command || checks.length >= MAX_CHECKS || checks.includes(command)) { if (command && checks.includes(command)) setDraft(""); return; }
    onChange([...checks, command]);
    setDraft("");
    setLabel("");
    setTimeoutValue("");
  };

  return <div className="agent-chip-root done-when-root" ref={rootRef}>
    <button ref={triggerRef} type="button" className="agent-chip done-when-chip" aria-label={summary} aria-haspopup="dialog" aria-expanded={open} onClick={toggle}>
      {checks.length ? <Check size={13} /> : null}
      <span className="agent-chip-text">{summary}</span>
      <ChevronDown size={13} />
    </button>
    {open ? <div className="agent-popover done-when-popover" role="dialog" aria-label="Done when">
      <div className="section-heading"><span>Done when</span></div>
      <p className="done-when-hint">Send runs a mission that is finished only when every check passes.</p>
      <div className="done-when-add">
        <input aria-label="Check command" autoFocus value={draft} placeholder="e.g. npm test" onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); add(); } }} />
        <button type="button" className="primary-action" disabled={!draft.trim() || checks.length >= MAX_CHECKS} onClick={add}>Add check</button>
      </div>
      {checks.length ? <ul className="done-when-list">{checks.map((check) => <li key={check}><code>{check}</code><button type="button" className="icon-button compact" aria-label={`Remove check ${check}`} onClick={() => onChange(checks.filter((item) => item !== check))}><X size={13} /></button></li>)}</ul> : null}
      <details open={moreOpen} onToggle={(event) => setMoreOpen((event.currentTarget as HTMLDetailsElement).open)}>
        <summary>More</summary>
        {moreOpen ? <div className="done-when-more">
          <label>Name<input value={label} onChange={(event) => setLabel(event.target.value)} /></label>
          <label>Timeout (seconds)<input type="number" min={1} value={timeout} onChange={(event) => setTimeoutValue(event.target.value)} /></label>
          <small>Labels only for now; missions take the command.</small>
        </div> : null}
      </details>
    </div> : null}
  </div>;
}
