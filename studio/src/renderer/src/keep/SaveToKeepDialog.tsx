import { useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, X } from "lucide-react";
import { memoryDraftProblem, parseTags, skillDraftProblem, type MemoryDraft, type SkillDraft } from "../../../core/keep/save.js";
import type { StudioAction, StudioActionResult } from "../../../shared/ipc";

type Mode = "memory" | "skill";

/**
 * Save what a session taught you without asking the agent to do it: a memory the team can
 * recall, or a skill draft that waits in the dashboard's review queue.
 */
export function SaveToKeepDialog({ invoke, onClose }: {
  /** Rejects with the Keep's own message on failure; shown inside the dialog. */
  readonly invoke: (action: StudioAction) => Promise<StudioActionResult>;
  readonly onClose: () => void;
}): React.JSX.Element {
  const [mode, setMode] = useState<Mode>("memory");
  const [action, setAction] = useState("");
  const [outcome, setOutcome] = useState("");
  const [resolution, setResolution] = useState("");
  const [tags, setTags] = useState("");
  const [trigger, setTrigger] = useState("");
  const [symptoms, setSymptoms] = useState("");
  const [steps, setSteps] = useState("");
  const [gotchas, setGotchas] = useState("");
  const [domain, setDomain] = useState("");
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const firstRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { firstRef.current?.focus(); }, [mode]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key !== "Escape") return;
      // Capture phase: closing the dialog must not also reach App's Escape (run.cancel).
      event.stopImmediatePropagation();
      onClose();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  const memory: MemoryDraft = { action, outcome, ...(resolution.trim() ? { resolution } : {}), tags: parseTags(tags) };
  const skill: SkillDraft = { trigger, symptoms, steps, ...(gotchas.trim() ? { gotchas } : {}), ...(domain.trim() ? { domain } : {}) };
  const required = mode === "memory" ? action.trim() && outcome.trim() : trigger.trim() && symptoms.trim() && steps.trim();

  const save = async (): Promise<void> => {
    const problem = mode === "memory" ? memoryDraftProblem(memory) : skillDraftProblem(skill);
    if (problem) { setFailure(problem); return; }
    setSaving(true);
    setFailure(null);
    try {
      await invoke(mode === "memory"
        ? { type: "keep.learn", action: memory.action.trim(), outcome: memory.outcome.trim(), ...(memory.resolution ? { resolution: memory.resolution.trim() } : {}), tags: [...memory.tags] }
        : { type: "keep.skillDraft", trigger: skill.trigger.trim(), symptoms: skill.symptoms.trim(), steps: skill.steps.trim(), ...(skill.gotchas ? { gotchas: skill.gotchas.trim() } : {}), ...(skill.domain ? { domain: skill.domain.trim() } : {}) });
      setSaved(mode === "memory" ? "Saved. Your team can recall this memory now." : "Draft saved. It waits in the dashboard's review queue and is not recalled until someone approves it.");
    } catch (caught) {
      setFailure(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <form className="modal keep-save" role="dialog" aria-modal="true" aria-labelledby="keep-save-title" onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <button type="button" className="icon-button keep-save-close" aria-label="Close" onClick={onClose}><X size={16} /></button>
        <h2 id="keep-save-title">Save to the Keep</h2>
        {saved ? <>
          <p className="keep-save-done" role="status"><CheckCircle2 size={15} /> {saved}</p>
          <div className="keep-save-actions"><button type="button" className="primary-action" onClick={onClose}>Done</button></div>
        </> : <>
          <div role="radiogroup" aria-label="What to save" className="keep-save-modes">
            <label><input type="radio" name="keep-save-mode" checked={mode === "memory"} onChange={() => { setMode("memory"); setFailure(null); }} />Memory</label>
            <label><input type="radio" name="keep-save-mode" checked={mode === "skill"} onChange={() => { setMode("skill"); setFailure(null); }} />Skill draft</label>
          </div>
          <p className="keep-save-hint">{mode === "memory"
            ? "One thing that happened and what came of it. Shared with your team as soon as it is saved."
            : "A repeatable procedure. Saved as a draft: it is reviewed in the dashboard before the team's agents can recall it."}</p>
          {mode === "memory" ? <>
            <label className="keep-save-field">What was done<textarea ref={firstRef} rows={2} value={action} onChange={(event) => setAction(event.target.value)} placeholder="Moved the nightly backup to 03:00 UTC" /></label>
            <label className="keep-save-field">What came of it<textarea rows={2} value={outcome} onChange={(event) => setOutcome(event.target.value)} placeholder="It no longer overlaps the index rebuild" /></label>
            <label className="keep-save-field">How it was resolved <small>optional: what you tried first and why it failed</small><textarea rows={2} value={resolution} onChange={(event) => setResolution(event.target.value)} /></label>
            <label className="keep-save-field">Tags <small>optional, comma separated</small><input type="text" value={tags} onChange={(event) => setTags(event.target.value)} placeholder="backup, ops" /></label>
          </> : <>
            <label className="keep-save-field">When it applies<textarea ref={firstRef} rows={2} value={trigger} onChange={(event) => setTrigger(event.target.value)} placeholder="The staging deploy fails at the migration step" /></label>
            <label className="keep-save-field">Symptoms<textarea rows={2} value={symptoms} onChange={(event) => setSymptoms(event.target.value)} placeholder="What you see when this is the problem" /></label>
            <label className="keep-save-field">Steps<textarea rows={4} value={steps} onChange={(event) => setSteps(event.target.value)} placeholder={"1. …\n2. …"} /></label>
            <label className="keep-save-field">Gotchas <small>optional</small><textarea rows={2} value={gotchas} onChange={(event) => setGotchas(event.target.value)} /></label>
            <label className="keep-save-field">Domain <small>optional</small><input type="text" value={domain} onChange={(event) => setDomain(event.target.value)} placeholder="deploy" /></label>
          </>}
          {failure ? <p className="keep-save-failure" role="alert"><AlertTriangle size={14} /> {failure}</p> : null}
          <div className="keep-save-actions">
            <span>Never save passwords, tokens or keys here.</span>
            <button type="button" onClick={onClose}>Cancel</button>
            <button type="submit" className="primary-action" disabled={saving || !required}>{saving ? "Saving…" : mode === "memory" ? "Save memory" : "Save draft"}</button>
          </div>
        </>}
      </form>
    </div>
  );
}
