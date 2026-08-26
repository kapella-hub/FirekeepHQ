import { Component, type ErrorInfo, type ReactNode } from "react";

interface RenderBoundaryProps {
  readonly children: ReactNode;
  readonly resetKey?: unknown;
}

interface RenderBoundaryState { readonly failed: boolean }

export class RenderBoundary extends Component<RenderBoundaryProps, RenderBoundaryState> {
  override state: RenderBoundaryState = { failed: false };

  static getDerivedStateFromError(): RenderBoundaryState { return { failed: true }; }

  override componentDidUpdate(previous: RenderBoundaryProps): void {
    if (this.state.failed && previous.resetKey !== this.props.resetKey) this.setState({ failed: false });
  }

  override componentDidCatch(_error: Error, _info: ErrorInfo): void {
    // React reports the component stack; the boundary keeps the rest of the session usable.
  }

  override render(): ReactNode {
    return this.state.failed
      ? <div className="render-fallback" role="alert"><strong>This response could not be rendered.</strong><span>The raw session is still saved; continue the conversation or export it.</span></div>
      : this.props.children;
  }
}
