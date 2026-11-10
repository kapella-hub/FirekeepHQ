import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Check, MessageSquarePlus, RefreshCw, Send, Trash2, Undo2, X } from "lucide-react";
import { parseUnifiedDiff, reviewPrompt, type ChangedFile, type FileDiff, type ReviewComment, type WorkspaceChanges } from "../../../core/workspace-changes.js";
import type { StudioAction, StudioActionResult } from "../../../shared/ipc";

const STATUS_LABEL: Record<ChangedFile["status"], string> = { modified: "M", added: "A", deleted: "D", renamed: "R", untracked: "N", conflicted: "!" };
const STATUS_NAME: Record<ChangedFile["status"], string> = { modified: "Modified", added: "Added", deleted: "Deleted", renamed: "Renamed", untracked: "New file", conflicted: "Merge conflict" };

const EMPTY_STATE: Record<Exclude<WorkspaceChanges["state"], "ok">, string> = {
  "no-workspace": "Choose a workspace folder to review what your agents change in it.",
  "not-a-repo": "This workspace is not a git repository, so Studio has no last commit to compare against.",
  "no-git": "git was not found on this machine. Install git to review changes here.",
};

interface DraftAnchor { readonly line: number | null; readonly quote?: string }

/**
 * The review surface: every uncommitted change in the workspace, one file at a time.
 * Accept stages a file, Revert throws its changes away (the main process confirms first),
 * and comments are sent back to the agent as one message.
 */
export function ReviewSheet({ changes, request, onChanges, onSend, onClose, agentWorking }: {
  readonly changes: WorkspaceChanges;
  /** The raw bridge: failures are shown inside the sheet, not in the app's error banner. */
  readonly request: (action: StudioAction) => Promise<StudioActionResult>;
  readonly onChanges: (changes: WorkspaceChanges) => void;
  readonly onSend: (prompt: string) => void;
  readonly onClose: () => void;
  readonly agentWorking: boolean;
}): React.JSX.Element {
  const [selected, setSelected] = useState<string | null>(changes.files[0]?.path ?? null);
  const [diff, setDiff] = useState<FileDiff | null>(null);
  const [loading, setLoading] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const [comments, setComments] = useState<readonly ReviewComment[]>([]);
  const [anchor, setAnchor] = useState<DraftAnchor | null>(null);
  const [draft, setDraft] = useState("");
  const draftRef = useRef<HTMLTextAreaElement>(null);
  const requestId = useRef(0);
  // Callers pass fresh closures on every render; refs keep the effects below from re-running for that.
  const requestRef = useRef(request);
  const onChangesRef = useRef(onChanges);
  requestRef.current = request;
  onChangesRef.current = onChanges;

  const file = changes.files.find((entry) => entry.path === selected) ?? null;
  // The selection follows the list: a reverted file disappears, so move to a neighbour.
  useEffect(() => {
    if (!file) setSelected(changes.files[0]?.path ?? null);
  }, [changes.files, file]);

  const run = useCallback(async (action: StudioAction): Promise<StudioActionResult | null> => {
    setFailure(null);
    const result = await requestRef.current(action);
    if (result.type === "error") { setFailure(result.message); return null; }
    if (result.type === "changes") onChangesRef.current(result.changes);
    return result;
  }, []);

  const refresh = useCallback(async (): Promise<void> => {
    setWorking(true);
    try { await run({ type: "changes.list" }); }
    finally { setWorking(false); }
  }, [run]);

  // Opening the sheet always starts from the workspace as it is now.
  useEffect(() => { void refresh(); }, [refresh]);

  const fingerprint = file ? `${file.path}:${file.status}:${file.accepted}:${file.additions}:${file.deletions}` : null;
  useEffect(() => {
    if (!file) { setDiff(null); return; }
    const id = requestId.current += 1;
    setLoading(true);
    void requestRef.current({ type: "changes.diff", path: file.path }).then((result) => {
      if (id !== requestId.current) return;
      if (result.type === "change-diff") { setDiff(result.diff); setFailure(null); }
      else { setDiff(null); if (result.type === "error") setFailure(result.message); }
    }).finally(() => { if (id === requestId.current) setLoading(false); });
    // `fingerprint` stands for the file: reload when its status changes, not on every list refresh.
  }, [fingerprint]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key !== "Escape") return;
      // Capture phase: closing the sheet must not also reach App's Escape (run.cancel).
      event.stopImmediatePropagation();
      if (anchor) { setAnchor(null); setDraft(""); } else onClose();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [anchor, onClose]);

  const lines = useMemo(() => {
    if (!diff || diff.binary) return [];
    const parsed = parseUnifiedDiff(diff.diff);
    // The git header before the first hunk repeats the file name; the toolbar already shows it.
    const firstHunk = parsed.findIndex((line) => line.kind === "hunk");
    return firstHunk > 0 ? parsed.slice(firstHunk) : parsed.filter((line) => line.kind !== "meta");
  }, [diff]);
  const act = async (type: "changes.accept" | "changes.unaccept" | "changes.revert"): Promise<void> => {
    if (!file) return;
    setWorking(true);
    try { await run({ type, path: file.path }); }
    finally { setWorking(false); }
  };
  const startComment = (next: DraftAnchor): void => {
    setAnchor(next);
    setDraft("");
    queueMicrotask(() => draftRef.current?.focus());
  };
  const addComment = (): void => {
    if (!file || !anchor || !draft.trim()) return;
    setComments((current) => [...current, { path: file.path, line: anchor.line, ...(anchor.quote ? { quote: anchor.quote } : {}), text: draft.trim() }]);
    setAnchor(null);
    setDraft("");
  };
  const fileComments = comments.filter((comment) => comment.path === file?.path);
  const accepted = changes.files.filter((entry) => entry.accepted).length;
  const total = changes.files.length;

  return (
    <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="modal review-sheet" role="dialog" aria-modal="true" aria-label="Review changes">
        <header className="review-header">
          <div><h2>Review changes</h2><p>{changes.state !== "ok" ? "Nothing to compare" : total === 0 ? "The workspace matches the last commit" : `${total}${changes.truncated ? "+" : ""} changed file${total === 1 ? "" : "s"} · ${accepted} accepted${changes.branch ? ` · ${changes.branch}` : ""}`}</p></div>
          <div className="review-header-actions">
            <button type="button" className="icon-button" aria-label="Refresh changes" title="Refresh" disabled={working} onClick={() => void refresh()}><RefreshCw size={15} className={working ? "spin" : undefined} /></button>
            <button type="button" className="icon-button" aria-label="Close review" title="Close" onClick={onClose}><X size={16} /></button>
          </div>
        </header>
        {failure ? <p className="review-failure" role="alert"><AlertTriangle size={14} /> {failure}</p> : null}
        {changes.state !== "ok" || total === 0 ? (
          <p className="review-empty">{changes.state !== "ok" ? EMPTY_STATE[changes.state] : "No uncommitted changes. When an agent edits files in this workspace, they appear here for review."}</p>
        ) : (
          <div className="review-body">
            <ul className="review-files" aria-label="Changed files">
              {changes.files.map((entry) => {
                const count = comments.filter((comment) => comment.path === entry.path).length;
                return (
                  <li key={entry.path}>
                    <button type="button" className={`review-file${entry.path === selected ? " current" : ""}${entry.accepted ? " accepted" : ""}`} aria-current={entry.path === selected} title={entry.previousPath ? `${entry.previousPath} → ${entry.path}` : entry.path} onClick={() => { setSelected(entry.path); setAnchor(null); setDraft(""); }}>
                      <span className={`review-status ${entry.status}`} title={STATUS_NAME[entry.status]} aria-label={STATUS_NAME[entry.status]}>{STATUS_LABEL[entry.status]}</span>
                      <span className="review-path"><bdi>{entry.path}</bdi></span>
                      {count > 0 ? <span className="review-comment-count" aria-label={`${count} comment${count === 1 ? "" : "s"}`}>{count}</span> : null}
                      {entry.accepted ? <Check size={13} className="review-accepted-mark" aria-label="Accepted" /> : entry.additions !== null || entry.deletions !== null ? <span className="review-counts"><ins>+{entry.additions ?? 0}</ins><del>−{entry.deletions ?? 0}</del></span> : null}
                    </button>
                  </li>
                );
              })}
            </ul>
            <div className="review-detail">
              {file ? <>
                <div className="review-toolbar">
                  <strong title={file.path}><bdi>{file.path}</bdi></strong>
                  <span className="review-toolbar-status">{STATUS_NAME[file.status]}</span>
                  <button type="button" onClick={() => startComment({ line: null })}><MessageSquarePlus size={13} /> Comment</button>
                  {file.accepted
                    ? <button type="button" disabled={working} title="Take this file back out of the index (git restore --staged)" onClick={() => void act("changes.unaccept")}><Undo2 size={13} /> Unaccept</button>
                    : <button type="button" className="primary-action" disabled={working || file.status === "conflicted"} title="Stage this file (git add). Nothing on disk changes." onClick={() => void act("changes.accept")}><Check size={13} /> Accept</button>}
                  <button type="button" className="danger-action" disabled={working || agentWorking || file.status === "conflicted"} title={agentWorking ? "Wait for the agent to finish before reverting" : "Discard this file's changes. Studio asks before anything is lost."} onClick={() => void act("changes.revert")}><Trash2 size={13} /> Revert…</button>
                </div>
                {fileComments.length > 0 ? (
                  <ul className="review-comments" aria-label="Comments on this file">
                    {fileComments.map((comment, index) => (
                      <li key={`${comment.line}:${index}`}><span>{comment.line === null ? "File" : `Line ${comment.line}`}</span><p>{comment.text}</p><button type="button" className="icon-button compact" aria-label="Remove comment" title="Remove comment" onClick={() => setComments((current) => current.filter((entry) => entry !== comment))}><X size={12} /></button></li>
                    ))}
                  </ul>
                ) : null}
                {anchor ? (
                  <form className="review-draft" onSubmit={(event) => { event.preventDefault(); addComment(); }}>
                    <label htmlFor="review-draft-text">{anchor.line === null ? "Comment on the whole file" : `Comment on line ${anchor.line}`}</label>
                    <textarea id="review-draft-text" ref={draftRef} rows={2} value={draft} placeholder="What should the agent change?" onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); addComment(); } }} />
                    <div><button type="button" onClick={() => { setAnchor(null); setDraft(""); }}>Cancel</button><button type="submit" className="primary-action" disabled={!draft.trim()}>Add comment</button></div>
                  </form>
                ) : null}
                <div className="review-diff" aria-label={`Diff of ${file.path}`} aria-busy={loading}>
                  {loading && !diff ? <p className="review-note">Loading diff…</p>
                    : !diff ? <p className="review-note">The diff could not be loaded.</p>
                      : diff.binary ? <p className="review-note">Binary file. Studio does not show its contents.</p>
                        : lines.length === 0 ? <p className="review-note">{file.status === "untracked" ? "Empty file." : "No textual changes (mode or whitespace only)."}</p>
                          : <table><tbody>
                            {lines.map((line, index) => {
                              const commentable = line.newLine !== null;
                              const marked = commentable && fileComments.some((comment) => comment.line === line.newLine);
                              return (
                                <tr key={index} className={`diff-${line.kind}${marked ? " commented" : ""}`}>
                                  <td className="diff-gutter">{line.oldLine ?? ""}</td>
                                  <td className="diff-gutter">{commentable ? <button type="button" aria-label={`Comment on line ${line.newLine}`} title="Comment on this line" onClick={() => startComment({ line: line.newLine, quote: line.text })}>{line.newLine}</button> : ""}</td>
                                  <td className="diff-sign">{line.kind === "add" ? "+" : line.kind === "remove" ? "−" : ""}</td>
                                  <td className="diff-text"><pre>{line.text || " "}</pre></td>
                                </tr>
                              );
                            })}
                          </tbody></table>}
                  {diff?.truncated ? <p className="review-note">This diff is long and was cut short. Open the file in your editor for the rest.</p> : null}
                </div>
              </> : null}
            </div>
          </div>
        )}
        <footer className="review-footer">
          <span>{comments.length === 0 ? "Click a line number to comment on it. Accept stages a file; nothing is committed." : `${comments.length} comment${comments.length === 1 ? "" : "s"} ready to send`}</span>
          <button type="button" className="primary-action" disabled={comments.length === 0 || agentWorking} title={agentWorking ? "Wait for the agent to finish" : "Send every comment to the agent as one message"} onClick={() => onSend(reviewPrompt(comments))}><Send size={13} /> Send to agent</button>
        </footer>
      </section>
    </div>
  );
}
