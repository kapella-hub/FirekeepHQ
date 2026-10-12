import { useState } from "react";
import { receiptLabel, type KeepReceipt } from "../../../core/keep/receipt.js";

type FeedbackPhase = { readonly useful: boolean; readonly phase: "sending" | "done" | "failed" };

export interface KeepReceiptCardProps {
  readonly receipt: KeepReceipt;
  readonly runId: string;
  /** Only the newest receipt, with no run active, may offer retry. */
  readonly canRetry: boolean;
  /** Rejects when the feedback could not be recorded. */
  readonly onFeedback: (memoryIds: readonly string[], useful: boolean) => Promise<void>;
  readonly onRetry: (runId: string) => void;
}

export function KeepReceiptCard({ receipt, runId, canRetry, onFeedback, onRetry }: KeepReceiptCardProps): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const [feedback, setFeedback] = useState<Readonly<Record<string, FeedbackPhase>>>({});

  if (receipt.status === "unavailable") {
    return canRetry
      ? <div className="keep-receipt-row"><button type="button" className="keep-receipt unavailable" onClick={() => onRetry(runId)}>{receiptLabel(receipt)}</button></div>
      : <div className="keep-receipt-row"><span className="keep-receipt unavailable">Keep receipt unavailable</span></div>;
  }

  const empty = receipt.recalledCount === 0 && receipt.learnedCount === 0;
  const send = (ids: readonly string[], useful: boolean): void => {
    const key = ids.join("|");
    setFeedback((current) => ({ ...current, [key]: { useful, phase: "sending" } }));
    onFeedback(ids, useful).then(
      () => setFeedback((current) => ({ ...current, [key]: { useful, phase: "done" } })),
      () => setFeedback((current) => ({ ...current, [key]: { useful, phase: "failed" } })),
    );
  };

  return <div className="keep-receipt-row">
    <button type="button" className={`keep-receipt${receipt.status === "measured" && empty ? " muted" : ""}`} aria-expanded={open} onClick={() => setOpen((value) => !value)}>{receiptLabel(receipt)}</button>
    {open ? <ul className="keep-receipt-details">
      {receipt.recalls.map((recall, index) => {
        const state = feedback[recall.memoryIds.join("|")];
        return <li key={`recall:${index}`}>
          <span className="receipt-text"><strong>{recall.query}</strong> · {recall.trigger === "prompt-hook" ? "via prompt hook" : "asked by the agent"} · {recall.resultCount} result{recall.resultCount === 1 ? "" : "s"}</span>
          {recall.memoryIds.length > 0 ? <span className="receipt-thumbs">
            <button type="button" aria-label="Knowledge was right" title="Was this knowledge right?" disabled={Boolean(state)} onClick={() => send(recall.memoryIds, true)}>👍</button>
            <button type="button" aria-label="Knowledge was wrong" title="Was this knowledge right?" disabled={Boolean(state)} onClick={() => send(recall.memoryIds, false)}>👎</button>
            {state?.phase === "failed" ? <button type="button" className="receipt-retry" onClick={() => send(recall.memoryIds, state.useful)}>retry</button> : null}
          </span> : null}
        </li>;
      })}
      {receipt.writes.map((write, index) => <li key={`write:${index}`}><span className="receipt-text">Learned · {write.summary}</span></li>)}
    </ul> : null}
  </div>;
}
