import { useEffect, useMemo, useRef, useState } from "react";
import type { CommandCompletion } from "../../../core/slash-commands.js";

function bare(text: string): string {
  return text.toLowerCase().replace(/^\//, "");
}

function isSubsequence(query: string, text: string): boolean {
  let at = 0;
  for (const char of text) if (char === query[at]) at += 1;
  return at === query.length;
}

/** Prefix beats word-start beats subsequence; ties sort alphabetically; non-matches are dropped. A leading "/" is ignored on both sides. */
export function rankCommands(query: string, items: readonly CommandCompletion[]): CommandCompletion[] {
  const q = bare(query.trim());
  const scored: { item: CommandCompletion; score: number }[] = [];
  for (const item of items) {
    const label = bare(item.label);
    let score: number;
    if (!q) score = 0;
    else if (label.startsWith(q)) score = 0;
    else if (label.split(/[^a-z0-9]+/).some((word) => word !== "" && word.startsWith(q))) score = 1;
    else if (isSubsequence(q, label)) score = 2;
    else continue;
    scored.push({ item, score });
  }
  return scored
    .sort((a, b) => a.score - b.score || a.item.label.localeCompare(b.item.label))
    .map((entry) => entry.item);
}

export interface CommandPaletteProps {
  readonly open: boolean;
  /** Escape or a backdrop click: the caller restores focus to whatever opened the palette. */
  readonly onClose: () => void;
  readonly completions: readonly CommandCompletion[];
  readonly onRun: (command: CommandCompletion) => void;
  readonly onSearchKeep: (query: string) => void;
}

export function CommandPalette({ open, onClose, completions, onRun, onSearchKeep }: CommandPaletteProps): React.JSX.Element | null {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActive(0);
    inputRef.current?.focus();
    const escape = (event: KeyboardEvent): void => {
      // Capture phase + stopImmediatePropagation: closing the palette must not also reach App's Escape (run.cancel).
      if (event.key === "Escape") { event.stopImmediatePropagation(); closeRef.current(); }
    };
    window.addEventListener("keydown", escape, true);
    return () => window.removeEventListener("keydown", escape, true);
  }, [open]);

  const ranked = useMemo(() => rankCommands(query, completions), [query, completions]);
  if (!open) return null;
  const count = ranked.length + 1;
  const activeIndex = Math.min(active, count - 1);
  const run = (index: number): void => {
    if (index === 0) onSearchKeep(query.trim());
    else onRun(ranked[index - 1]!);
  };
  const keyDown = (event: React.KeyboardEvent): void => {
    if (event.key === "ArrowDown") { event.preventDefault(); setActive((activeIndex + 1) % count); }
    else if (event.key === "ArrowUp") { event.preventDefault(); setActive((activeIndex + count - 1) % count); }
    else if (event.key === "Enter") { event.preventDefault(); run(activeIndex); }
  };
  return <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
    <div className="command-palette" role="dialog" aria-modal="true" aria-label="Command palette">
      <input ref={inputRef} type="text" role="combobox" aria-expanded="true" aria-controls="palette-options" aria-activedescendant={`palette-option-${activeIndex}`} aria-label="Search commands" placeholder="Type a command, or search the Keep" value={query} onChange={(event) => { setQuery(event.target.value); setActive(0); }} onKeyDown={keyDown} />
      <ul id="palette-options" role="listbox" aria-label="Commands">
        <li id="palette-option-0" role="option" aria-selected={activeIndex === 0} onMouseEnter={() => setActive(0)} onMouseDown={(event) => event.preventDefault()} onClick={() => run(0)}>Search the Keep…</li>
        {ranked.map((item, index) => <li key={item.value} id={`palette-option-${index + 1}`} role="option" aria-selected={activeIndex === index + 1} onMouseEnter={() => setActive(index + 1)} onMouseDown={(event) => event.preventDefault()} onClick={() => run(index + 1)}>
          <span>{item.label}</span>{item.description ? <small>{item.description}</small> : null}
        </li>)}
      </ul>
    </div>
  </div>;
}
