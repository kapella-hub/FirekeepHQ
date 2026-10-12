import type { ReactNode } from "react";

export function StudioShell({ topBar, rail, conversation, keepPanel, keepPanelOpen, busy, children }: {
  readonly topBar: ReactNode;
  readonly rail: ReactNode;
  readonly conversation: ReactNode;
  readonly keepPanel: ReactNode;
  readonly keepPanelOpen: boolean;
  readonly busy?: boolean;
  /** Modals and other overlays rendered inside the shell root. */
  readonly children?: ReactNode;
}): React.JSX.Element {
  return (
    <div className="studio" data-keep-open={String(keepPanelOpen)} data-busy={busy || undefined}>
      {topBar}
      {rail}
      {conversation}
      {keepPanelOpen ? keepPanel : null}
      {children}
    </div>
  );
}
