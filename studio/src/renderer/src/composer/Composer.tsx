import { ClipboardPaste, Send, Square } from "lucide-react";
import { useState } from "react";

export interface CompletionItem { readonly value: string; readonly label: string; readonly description: string }

export interface ComposerUsage {
  readonly tokens: number;
  readonly freshTokens: number;
  readonly cachedTokens: number;
  readonly costUsd: number;
  readonly totalRuns: number;
  readonly measuredRuns: number;
  readonly tokenBudget: number | null;
}

export interface ComposerProps {
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly onSubmit: () => void;
  readonly busy: boolean;
  readonly canStop: boolean;
  readonly onStop: () => void;
  readonly onPaste: () => void;
  readonly placeholder: string;
  readonly textareaRef: React.RefObject<HTMLTextAreaElement | null>;
  readonly completions: readonly CompletionItem[];
  readonly completionIndex: number;
  readonly onCompletionIndex: (index: number) => void;
  readonly onPickCompletion: (value: string) => void;
  /** Chips row, e.g. the Agent chip and (Task 15) the Done-when chip. */
  readonly chips?: React.ReactNode;
  readonly usage: ComposerUsage;
  readonly onExpandUsage?: (expanded: boolean) => void;
}

/** 12_000 -> "12k", 1_500 -> "1.5k", 2_400_000 -> "2.4M". */
export function compactTokens(value: number): string {
  const trim = (n: number): string => (n >= 10 ? Math.round(n) : Math.round(n * 10) / 10).toString();
  if (value >= 1_000_000) return `${trim(value / 1_000_000)}M`;
  if (value >= 1_000) return `${trim(value / 1_000)}k`;
  return String(Math.round(value));
}

export function Composer(props: ComposerProps): React.JSX.Element {
  const { value, completions, completionIndex, usage } = props;
  const [usageOpen, setUsageOpen] = useState(false);
  const budgetPercent = usage.tokenBudget ? Math.min(100, (usage.tokens / usage.tokenBudget) * 100) : 0;
  const toggleUsage = (): void => { const next = !usageOpen; setUsageOpen(next); props.onExpandUsage?.(next); };

  return <div className="composer-zone">
    {completions.length ? <div className="command-menu" role="listbox">{completions.slice(0, 9).map((item, index) => <button role="option" aria-selected={index === completionIndex} key={`${item.value}:${index}`} className={index === completionIndex ? "selected" : ""} onMouseDown={(event) => { event.preventDefault(); props.onPickCompletion(item.value); }}><span>{item.label}</span><small>{item.description}</small></button>)}</div> : null}
    <div className="composer-shell">
      <textarea ref={props.textareaRef} aria-label="Message composer" value={value} rows={1} placeholder={props.placeholder} onChange={(event) => props.onChange(event.target.value)} onKeyDown={(event) => {
        if (event.key === "ArrowDown" && completions.length) { event.preventDefault(); props.onCompletionIndex(Math.min(completionIndex + 1, completions.length - 1)); }
        else if (event.key === "ArrowUp" && completions.length) { event.preventDefault(); props.onCompletionIndex(Math.max(completionIndex - 1, 0)); }
        else if (event.key === "Tab" && completions[completionIndex]) { event.preventDefault(); props.onPickCompletion(completions[completionIndex]!.value); }
        else if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); props.onSubmit(); }
      }} />
      <div className="composer-actions">
        <button type="button" className="paste-button" aria-label="Paste from clipboard" title="Paste from clipboard" onClick={props.onPaste}><ClipboardPaste size={17} /></button>
        {props.canStop ? <button className="send-button stop" title="Cancel run" onClick={props.onStop}><Square size={15} fill="currentColor" /></button> : <button className="send-button" title="Send" disabled={!value.trim() || props.busy} onClick={props.onSubmit}><Send size={17} /></button>}
      </div>
    </div>
    {props.chips ? <div className="composer-chips">{props.chips}</div> : null}
    {usageOpen ? <div className="usage-detail" aria-label="Session usage">
      <div className="usage-total"><strong>{usage.freshTokens.toLocaleString()} fresh tokens</strong><button type="button" className="text-button inline" onClick={() => { props.onChange("/budget "); props.textareaRef.current?.focus(); }}>Set guard</button></div>
      <div className="usage-breakdown"><span>{usage.cachedTokens.toLocaleString()} cached</span><span>{usage.tokens.toLocaleString()} total traffic</span></div>
      {usage.tokenBudget ? <><div className="budget-track" aria-label={`${budgetPercent.toFixed(0)} percent of token guard used`}><span style={{ width: `${budgetPercent}%` }} /></div><small>{Math.max(0, usage.tokenBudget - usage.tokens).toLocaleString()} before the next-turn guard</small></> : <small>No token guard · use <code>/budget set 50k</code></small>}
      <small>{usage.measuredRuns}/{usage.totalRuns} runs reported usage{usage.costUsd ? ` · $${usage.costUsd.toFixed(4)} reported` : " · provider cost unavailable"}</small>
    </div> : null}
    <div className="composer-hint"><span><kbd>Enter</kbd> send · <kbd>Shift Enter</kbd> newline · <kbd>⌘K</kbd> commands</span><span>{props.busy ? <><span className="pulse-dot" /> Agent working · </> : null}<button type="button" className="usage-line" aria-expanded={usageOpen} onClick={toggleUsage}>{compactTokens(usage.freshTokens)} fresh · {compactTokens(usage.cachedTokens)} cached</button></span></div>
  </div>;
}
