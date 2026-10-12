import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle } from "lucide-react";
import type { KeepReceipt } from "../../../core/keep/receipt.js";
import type { StudioAction, StudioActionResult } from "../../../shared/ipc";
import { KEEP_INSTALL_COMMANDS, type KeepItem, type KeepStatus } from "../../../shared/keep";
import { CopyButton } from "../CopyButton.js";
import { RichMarkdown } from "../RichMarkdown.js";
import { ignore } from "../util.js";

export interface KeepPanelProps {
  readonly status: KeepStatus | null;
  /** This session's receipts, newest first. */
  readonly receipts: readonly KeepReceipt[];
  readonly invoke: (action: StudioAction) => Promise<StudioActionResult>;
  /** Appends a quoted block to the composer. */
  readonly onInsert: (text: string) => void;
  readonly onOpenKeepSettings: () => void;
  /** Re-checks the kit; App applies the reply unless a newer push arrived meanwhile. */
  readonly onRetry: () => void;
  /** An externally supplied query (the command palette): switches to Memory, fills and focuses the search box. */
  readonly searchRequest?: KeepSearchRequest | null;
  /** Called once the panel has taken the request, so a remount does not replay it. */
  readonly onSearchRequestHandled?: (() => void) | undefined;
}

export interface KeepSearchRequest { readonly query: string; readonly nonce: number }

type Tab = "memory" | "skills" | "team";
const TABS: readonly { readonly id: Tab; readonly label: string }[] = [{ id: "memory", label: "Memory" }, { id: "skills", label: "Skills" }, { id: "team", label: "Team" }];
const TAB_KEY = "firekeep.keepTab";

function readTab(): Tab {
  try {
    const value = window.localStorage.getItem(TAB_KEY);
    return value === "skills" || value === "team" ? value : "memory";
  } catch { return "memory"; }
}
function writeTab(tab: Tab): void {
  try { window.localStorage.setItem(TAB_KEY, tab); } catch { /* storage unavailable: the tab just will not persist */ }
}

export function quoteBlock(body: string): string {
  return `${body.replace(/\n+$/, "").split("\n").map((line) => `> ${line}`).join("\n")}\n`;
}

export function KeepPanel({ status, receipts, invoke, onInsert, onOpenKeepSettings, onRetry, searchRequest = null, onSearchRequestHandled }: KeepPanelProps): React.JSX.Element {
  return <aside className="keep-panel" aria-label="The Keep">
    <h2>The Keep</h2>
    {!status || status.state === "starting" ? <div className="keep-skeleton" aria-busy="true" aria-label="Connecting to the Keep"><span /><span /><span /></div>
      : status.state === "missing" ? <MissingCard onRetry={onRetry} />
      : status.state === "connected" ? <>
        {status.personalMode ? <p className="keep-personal-banner" role="status">Personal mode — nothing from this session is shared</p> : null}
        <ConnectedTabs receipts={receipts} invoke={invoke} onInsert={onInsert} searchRequest={searchRequest} onSearchRequestHandled={onSearchRequestHandled} />
      </>
      : <div className="keep-state-card">
        <p className="keep-state-detail"><AlertTriangle size={13} /> {status.detail}</p>
        {status.state === "unreachable" && status.retryInMs !== null ? <p className="keep-retrying" role="status">Retrying…</p> : null}
        <button type="button" onClick={onOpenKeepSettings}>Run doctor</button>
      </div>}
  </aside>;
}

function MissingCard({ onRetry }: Pick<KeepPanelProps, "onRetry">): React.JSX.Element {
  return <div className="keep-state-card">
    <h3>Connect Studio to your team's Keep</h3>
    <p>The Keep is your team's shared memory: what agents learned, the skills they built, and who is working on what.</p>
    {([["macOS / Linux", KEEP_INSTALL_COMMANDS.unix], ["Windows", KEEP_INSTALL_COMMANDS.windows]] as const).map(([label, command]) => <div key={label} className="keep-install">
      <small>{label}</small>
      <div><code>{command}</code><CopyButton text={command} label={`Copy ${label} install command`} /></div>
    </div>)}
    <button type="button" className="primary-action" onClick={onRetry}>I've installed it — retry</button>
  </div>;
}

function ConnectedTabs({ receipts, invoke, onInsert, searchRequest, onSearchRequestHandled }: Pick<KeepPanelProps, "receipts" | "invoke" | "onInsert" | "searchRequest" | "onSearchRequestHandled">): React.JSX.Element {
  const [tab, setTab] = useState<Tab>(readTab);
  const [detail, setDetail] = useState<KeepItem | null>(null);
  const [pending, setPending] = useState<KeepSearchRequest | null>(null);
  useEffect(() => {
    if (!searchRequest) return;
    setPending(searchRequest);
    setTab("memory");
    setDetail(null);
    onSearchRequestHandled?.();
  }, [searchRequest, onSearchRequestHandled]);
  const choose = (next: Tab): void => { setTab(next); setDetail(null); writeTab(next); };
  const closeDetail = useCallback(() => setDetail(null), []);
  return <>
    <div role="tablist" aria-label="Keep sections" className="keep-tabs">
      {TABS.map((entry) => <button key={entry.id} type="button" role="tab" id={`keep-tab-${entry.id}`} aria-selected={tab === entry.id} aria-controls="keep-tabpanel" onClick={() => choose(entry.id)}>{entry.label}</button>)}
    </div>
    <div role="tabpanel" id="keep-tabpanel" aria-labelledby={`keep-tab-${tab}`} className="keep-tabpanel">
      {tab === "team" ? <TeamTab invoke={invoke} />
        : <SearchTab key={tab} kind={tab === "skills" ? "skill" : "memory"} receipts={receipts} invoke={invoke} onOpen={setDetail} request={pending} onRequestApplied={() => setPending(null)} />}
    </div>
    {detail ? <ItemDetail item={detail} onClose={closeDetail} onInsert={(text) => { onInsert(text); setDetail(null); }} /> : null}
  </>;
}

function SearchTab({ kind, receipts, invoke, onOpen, request, onRequestApplied }: { readonly kind: "memory" | "skill"; readonly receipts: readonly KeepReceipt[]; readonly invoke: KeepPanelProps["invoke"]; readonly onOpen: (item: KeepItem) => void; readonly request: KeepSearchRequest | null; readonly onRequestApplied: () => void }): React.JSX.Element {
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!request || kind !== "memory") return;
    // The existing 300 ms debounce below runs the search once the query lands.
    setQuery(request.query);
    inputRef.current?.focus();
    onRequestApplied();
  }, [request, kind, onRequestApplied]);
  const [items, setItems] = useState<readonly KeepItem[] | null>(null);
  const [skills, setSkills] = useState<readonly KeepItem[] | null>(null);
  const [failed, setFailed] = useState(false);
  const generation = useRef(0);
  const lastRun = useRef("");

  const run = useCallback((text: string): void => {
    const trimmed = text.trim();
    const id = ++generation.current;
    lastRun.current = trimmed;
    setFailed(false);
    if (!trimmed) { setItems(null); return; }
    ignore(invoke({ type: "keep.search", query: trimmed, kind }).then(
      (result) => { if (id === generation.current && result.type === "keep-items") setItems(result.items); },
      () => { if (id === generation.current) { setItems(null); setFailed(true); } },
    ));
  }, [invoke, kind]);

  useEffect(() => {
    if (kind !== "skill") return;
    let active = true;
    invoke({ type: "keep.skills" }).then((result) => { if (active && result.type === "keep-items") setSkills(result.items); }, () => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [invoke, kind]);

  useEffect(() => {
    if (query.trim() === lastRun.current) return;
    const timer = window.setTimeout(() => run(query), 300);
    return () => window.clearTimeout(timer);
  }, [query, run]);

  const label = kind === "skill" ? "Search team skills" : "Search team memory";
  const searching = query.trim() !== "";
  const shown = searching ? items : kind === "skill" ? skills : null;
  return <>
    <input ref={inputRef} type="search" className="keep-search" aria-label={label} placeholder={kind === "skill" ? "Search skills" : "Search the team's memory"} value={query} onChange={(event) => setQuery(event.target.value)} />
    {failed ? <p className="keep-empty">Could not reach the Keep. Try again.</p> : null}
    {shown ? shown.length === 0 ? <p className="keep-empty">{searching ? "Nothing found." : "No active skills yet."}</p>
      : <ul className="keep-results">{shown.map((item, index) => <li key={`${item.title}:${index}`}>
        <button type="button" onClick={() => onOpen(item)}>{item.title}</button>
        {item.body.includes("[TRIAL]") ? <span className="keep-trial-badge">TRIAL</span> : null}
      </li>)}</ul>
      : kind === "memory" ? <Recalls receipts={receipts} onPick={(text) => { setQuery(text); run(text); }} />
      : failed ? null : <p className="keep-empty">Loading skills…</p>}
  </>;
}

function Recalls({ receipts, onPick }: { readonly receipts: readonly KeepReceipt[]; readonly onPick: (query: string) => void }): React.JSX.Element {
  const recalls = receipts.flatMap((receipt) => receipt.recalls);
  if (recalls.length === 0) return <p className="keep-empty">Memories recalled in this session will show here.</p>;
  return <>
    <p className="keep-section-label">Recalled this session</p>
    <ul className="keep-results">{recalls.map((recall, index) => <li key={index}>
      <button type="button" onClick={() => onPick(recall.query)}>{recall.query}<small> · {recall.resultCount}</small></button>
    </li>)}</ul>
  </>;
}

function TeamTab({ invoke }: { readonly invoke: KeepPanelProps["invoke"] }): React.JSX.Element {
  const [markdown, setMarkdown] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    invoke({ type: "keep.team" }).then((result) => { if (active && result.type === "keep-team") setMarkdown(result.markdown); }, () => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [invoke]);
  if (failed) return <p className="keep-empty">Could not reach the Keep. Try again.</p>;
  if (markdown === null) return <p className="keep-empty">Loading…</p>;
  return markdown.trim() ? <div className="markdown"><RichMarkdown>{markdown}</RichMarkdown></div> : <p className="keep-empty">Nobody else is active in the Keep right now.</p>;
}

function ItemDetail({ item, onClose, onInsert }: { readonly item: KeepItem; readonly onClose: () => void; readonly onInsert: (text: string) => void }): React.JSX.Element {
  useEffect(() => {
    const escape = (event: KeyboardEvent): void => {
      // Capture phase + stopImmediatePropagation: closing the popover must not also reach App's Escape (run.cancel).
      if (event.key === "Escape") { event.stopImmediatePropagation(); onClose(); }
    };
    window.addEventListener("keydown", escape, true);
    return () => window.removeEventListener("keydown", escape, true);
  }, [onClose]);
  return <div className="keep-detail" role="dialog" aria-label={item.title}>
    <header><strong>{item.title}</strong><button type="button" aria-label="Close detail" onClick={onClose}>×</button></header>
    <div className="markdown keep-detail-body"><RichMarkdown>{item.body}</RichMarkdown></div>
    <button type="button" className="primary-action" onClick={() => onInsert(quoteBlock(item.body))}>Insert into composer</button>
  </div>;
}
