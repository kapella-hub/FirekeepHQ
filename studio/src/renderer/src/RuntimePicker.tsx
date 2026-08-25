import { Check, ChevronDown } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { RuntimeDescriptor } from "../../core/runtime.js";
import type { RuntimeDiagnostic } from "../../core/studio-service.js";

interface RuntimePickerProps {
  readonly runtimes: readonly RuntimeDescriptor[];
  readonly selectedId: string | null;
  readonly diagnostics: Readonly<Record<string, RuntimeDiagnostic>>;
  readonly onSelect: (runtimeId: string) => void;
}

export function RuntimePicker({ runtimes, selectedId, diagnostics, onSelect }: RuntimePickerProps): React.JSX.Element {
  const choices = runtimes.filter((runtime) => runtime.capabilities.includes("chat"));
  const selected = choices.find((runtime) => runtime.id === selectedId);
  const selectedIndex = Math.max(0, choices.findIndex((runtime) => runtime.id === selectedId));
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(selectedIndex);
  const rootRef = useRef<HTMLDivElement>(null);
  const status = runtimeStatus(selected ? diagnostics[selected.id] : undefined);

  useEffect(() => {
    if (!open) setActiveIndex(selectedIndex);
  }, [open, selectedIndex]);

  useEffect(() => {
    if (!open) return undefined;
    const close = (event: PointerEvent): void => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);

  const choose = (index: number): void => {
    const runtime = choices[index];
    if (!runtime) return;
    setOpen(false);
    if (runtime.id !== selectedId) onSelect(runtime.id);
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>): void => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        setOpen(true);
        setActiveIndex(selectedIndex);
      } else {
        const direction = event.key === "ArrowDown" ? 1 : -1;
        setActiveIndex((index) => (index + direction + choices.length) % choices.length);
      }
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (open) choose(activeIndex);
      else setOpen(true);
    } else if (event.key === "Home" && open) {
      event.preventDefault();
      setActiveIndex(0);
    } else if (event.key === "End" && open) {
      event.preventDefault();
      setActiveIndex(Math.max(0, choices.length - 1));
    } else if (event.key === "Escape" && open) {
      event.preventDefault();
      setOpen(false);
    }
  };

  return <div className="runtime-picker" ref={rootRef}>
    <button
      type="button"
      className="runtime-picker-trigger"
      aria-label={`Primary runtime: ${selected?.displayName ?? "not selected"}, ${status.label.toLowerCase()}`}
      aria-haspopup="listbox"
      aria-expanded={open}
      aria-controls="primary-runtime-options"
      onClick={() => setOpen((value) => !value)}
      onKeyDown={onKeyDown}
    >
      <span className="runtime-orb" style={{ "--runtime-accent": selected?.accent ?? "#df7e45" } as React.CSSProperties}>{selected?.displayName.slice(0, 1) ?? "?"}</span>
      <span className="runtime-picker-copy"><small>Primary runtime</small><strong>{selected?.displayName ?? "Choose an agent"}</strong></span>
      <span className={`runtime-picker-status ${status.tone}`}><i />{status.label}</span>
      <ChevronDown className="runtime-picker-chevron" size={15} />
    </button>
    {open ? <div className="runtime-picker-menu" id="primary-runtime-options" role="listbox" aria-label="Primary runtime">
      <header><span>Choose the agent for new turns</span><small>Reviewers stay independent</small></header>
      {choices.map((runtime, index) => {
        const optionStatus = runtimeStatus(diagnostics[runtime.id]);
        const isSelected = runtime.id === selectedId;
        return <button
          type="button"
          key={runtime.id}
          role="option"
          aria-selected={isSelected}
          className={index === activeIndex ? "active" : ""}
          onMouseEnter={() => setActiveIndex(index)}
          onClick={() => choose(index)}
        >
          <span className="runtime-orb" style={{ "--runtime-accent": runtime.accent ?? "#df7e45" } as React.CSSProperties}>{runtime.displayName.slice(0, 1)}</span>
          <span><strong>{runtime.displayName}</strong><small>{runtime.transport}</small></span>
          <span className={`runtime-option-status ${optionStatus.tone}`}><i />{optionStatus.label}</span>
          {isSelected ? <Check className="runtime-option-check" size={15} /> : null}
        </button>;
      })}
    </div> : null}
  </div>;
}

function runtimeStatus(diagnostic: RuntimeDiagnostic | undefined): { readonly label: string; readonly tone: string } {
  if (!diagnostic) return { label: "Checking", tone: "checking" };
  if (diagnostic.connection.state === "missing") return { label: "Not installed", tone: "error" };
  if (diagnostic.connection.state === "error") return { label: "Error", tone: "error" };
  if (diagnostic.connection.state === "disconnected") return { label: "Offline", tone: "warning" };
  if (diagnostic.auth.state === "disconnected") return { label: "Sign in", tone: "warning" };
  if (diagnostic.auth.state === "error") return { label: "Auth error", tone: "error" };
  return { label: "Ready", tone: "ready" };
}
