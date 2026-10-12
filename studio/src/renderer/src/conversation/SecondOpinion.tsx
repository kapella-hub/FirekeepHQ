import { useEffect, useState } from "react";
import type { RuntimeDescriptor } from "../../../core/runtime.js";

export interface SecondOpinionChooserProps {
  readonly reviewers: readonly RuntimeDescriptor[];
  readonly primaryRuntimeId: string | null;
  readonly onRun: (runtimeId: string, focus: string) => void;
  readonly onClose: () => void;
}

/** One-line chooser mounted only while open; default reviewer is the first review-capable runtime that is not the primary. */
export function SecondOpinionChooser({ reviewers, primaryRuntimeId, onRun, onClose }: SecondOpinionChooserProps): React.JSX.Element {
  const defaultReviewer = (reviewers.find((candidate) => candidate.id !== primaryRuntimeId) ?? reviewers[0])?.id ?? "";
  const [chosen, setChosen] = useState("");
  const [focus, setFocus] = useState("");
  const runtimeId = reviewers.some((candidate) => candidate.id === chosen) ? chosen : defaultReviewer;
  useEffect(() => {
    const escape = (event: KeyboardEvent): void => {
      // Capture phase + stopImmediatePropagation: closing the chooser must not also reach App's Escape (run.cancel).
      if (event.key === "Escape") { event.stopImmediatePropagation(); onClose(); }
    };
    window.addEventListener("keydown", escape, true);
    return () => window.removeEventListener("keydown", escape, true);
  }, [onClose]);
  const run = (): void => { onRun(runtimeId, focus.trim()); onClose(); };
  return <div className="second-opinion-chooser">
    <select aria-label="Reviewer" value={runtimeId} onChange={(event) => setChosen(event.target.value)}>{reviewers.map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.displayName}</option>)}</select>
    <input type="text" placeholder="Focus (optional), e.g. security" value={focus} onChange={(event) => setFocus(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); run(); } }} />
    <button type="button" className="primary-action" onClick={run}>Run</button>
  </div>;
}
