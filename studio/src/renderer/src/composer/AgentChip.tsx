import { ChevronDown, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { RuntimeDescriptor, RuntimeEffort, RuntimeModel } from "../../../core/runtime.js";
import type { RuntimeDiagnostic } from "../../../core/studio-service.js";

export type PermissionMode = "safe" | "standard" | "unrestricted";
export type ReviewerMode = "off" | "manual" | "after-turn";

export interface ModelRefreshState {
  readonly state: string;
  readonly message: string;
}

export interface AgentChipProps {
  readonly runtimes: readonly RuntimeDescriptor[];
  readonly diagnostics: Readonly<Record<string, RuntimeDiagnostic>>;
  readonly primaryRuntimeId: string | null;
  readonly models: readonly RuntimeModel[];
  /** The model id stored for the primary runtime ("" = provider default). */
  readonly selectedModel: string;
  readonly selectedEffort: RuntimeEffort | "";
  readonly liveEfforts: readonly RuntimeEffort[];
  readonly permissionMode: PermissionMode;
  readonly onPrimary: (runtimeId: string) => void;
  readonly onModel: (modelId: string) => void;
  readonly onEffort: (effort: RuntimeEffort | null) => void;
  readonly onPermission: (mode: PermissionMode) => void;
  readonly onLoadModels: () => void;
  readonly onRefreshModels: () => void;
  readonly refresh?: ModelRefreshState | null;
  /** Interim home of the reviewer-mode select; Task 18 moves it to Settings > Reviews. */
  readonly reviewerMode: ReviewerMode;
  readonly onReviewerMode: (mode: ReviewerMode) => void;
}

export function AgentChip(props: AgentChipProps): React.JSX.Element {
  const { runtimes, primaryRuntimeId, models, selectedModel, selectedEffort, liveEfforts, permissionMode, refresh } = props;
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const choices = runtimes.filter((runtime) => runtime.capabilities.includes("chat"));
  const primary = runtimes.find((runtime) => runtime.id === primaryRuntimeId);
  const model = models.find((item) => item.id === selectedModel) ?? models.find((item) => item.isDefault);
  const summary = primary ? [primary.displayName, model?.displayName, selectedEffort].filter(Boolean).join(" · ") : "Choose an agent";
  const loading = Boolean(refresh && refresh.state === "loading");

  useEffect(() => {
    if (!open) return undefined;
    const close = (event: PointerEvent): void => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent): void => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", escape); };
  }, [open]);

  return <div className="agent-chip-root" ref={rootRef}>
    <button type="button" className="agent-chip" aria-label={primary ? `Agent: ${summary}` : summary} aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
      <span className="runtime-orb" style={{ "--runtime-accent": primary?.accent ?? "#df7e45" } as React.CSSProperties}>{primary?.displayName.slice(0, 1) ?? "?"}</span>
      <span className="agent-chip-text">{summary}</span>
      <ChevronDown size={13} />
    </button>
    {open ? <div className="agent-popover" role="dialog" aria-label="Agent settings">
      <div className="section-heading"><span>Agent settings</span><button type="button" className="icon-button compact" disabled={!primaryRuntimeId || loading} aria-label="Refresh live model options" title="Refresh live model options" onClick={props.onRefreshModels}><RefreshCw className={loading ? "spin" : undefined} size={14} /></button></div>
      {primaryRuntimeId && refresh ? <p className={`model-refresh-status ${refresh.state}`} role="status" aria-live="polite">{refresh.message}</p> : null}
      <label>Agent<select value={primaryRuntimeId ?? ""} onChange={(event) => { if (event.target.value && event.target.value !== primaryRuntimeId) props.onPrimary(event.target.value); }}>
        {primaryRuntimeId ? null : <option value="">Choose an agent</option>}
        {choices.map((runtime) => <option key={runtime.id} value={runtime.id}>{runtime.displayName}{props.diagnostics[runtime.id]?.connection.state === "missing" ? " (not installed)" : ""}</option>)}
      </select></label>
      <label>Model<select disabled={!primaryRuntimeId} value={selectedModel} onFocus={props.onLoadModels} onChange={(event) => props.onModel(event.target.value)}>
        <option value="">Provider default</option>
        {models.filter((item) => !(item.id === "default" && item.isDefault)).map((item) => <option key={item.id} value={item.id}>{item.displayName}</option>)}
      </select></label>
      <label>Reasoning<select disabled={!primaryRuntimeId || !primary?.capabilities.includes("reasoning") || liveEfforts.length === 0} value={selectedEffort} onChange={(event) => props.onEffort(event.target.value ? event.target.value as RuntimeEffort : null)}>
        <option value="">Provider default</option>
        {liveEfforts.map((value) => <option key={value} value={value}>{value}</option>)}
      </select></label>
      <label>Permissions<select disabled={!primaryRuntimeId} value={permissionMode} onChange={(event) => props.onPermission(event.target.value as PermissionMode)}>
        <option value="safe">Safe · read only</option><option value="standard">Standard · ask</option><option value="unrestricted">Unrestricted · explicit</option>
      </select></label>
      <label>After each turn<select value={props.reviewerMode} onChange={(event) => props.onReviewerMode(event.target.value as ReviewerMode)}>
        <option value="off">Off</option><option value="manual">Manual</option><option value="after-turn">After every turn</option>
      </select></label>
    </div> : null}
  </div>;
}
