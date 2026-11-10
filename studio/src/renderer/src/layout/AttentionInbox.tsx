import { useEffect, useRef, useState } from "react";
import { Flag, Inbox, MessagesSquare, ShieldCheck } from "lucide-react";
import type { InboxItem } from "../inbox.js";

const ICONS = { approval: ShieldCheck, decision: MessagesSquare, mission: Flag } as const;

/** The top-bar inbox: one list of everything in this session that is waiting on the human. */
export function AttentionInbox({ items, onOpen }: {
  readonly items: readonly InboxItem[];
  readonly onOpen: (item: InboxItem) => void;
}): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent | KeyboardEvent): void => {
      if (event instanceof KeyboardEvent) {
        // Capture phase + stopImmediatePropagation: closing the list must not also reach App's Escape (run.cancel).
        if (event.key === "Escape") { event.stopImmediatePropagation(); setOpen(false); }
        return;
      }
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", close, true);
    return () => { window.removeEventListener("mousedown", close); window.removeEventListener("keydown", close, true); };
  }, [open]);
  const count = items.length;
  const label = count === 0 ? "Inbox: nothing is waiting on you" : `Inbox: ${count} waiting on you`;
  return (
    <div className="inbox-wrap" ref={wrapRef}>
      <button type="button" className={`icon-button inbox-button${count > 0 ? " waiting" : ""}`} aria-label={label} aria-haspopup="menu" aria-expanded={open} title={label} onClick={() => setOpen((value) => !value)}>
        <Inbox size={17} />{count > 0 ? <span className="inbox-count" aria-hidden="true">{count > 9 ? "9+" : count}</span> : null}
      </button>
      {open ? (
        <div className="inbox-menu" role="menu" aria-label="Waiting on you">
          <header><strong>Waiting on you</strong><small>{count === 0 ? "All clear" : `${count} item${count === 1 ? "" : "s"}`}</small></header>
          {count === 0 ? <p className="inbox-empty">Nothing needs you right now. Approvals, Decision Boards and missions awaiting sign-off appear here.</p> : items.map((item) => {
            const Icon = ICONS[item.kind];
            return (
              <button key={item.id} type="button" role="menuitem" className={`inbox-item ${item.kind}`} onClick={() => { setOpen(false); onOpen(item); }}>
                <Icon size={15} /><span><strong>{item.title}</strong><small>{item.detail}</small></span>
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
