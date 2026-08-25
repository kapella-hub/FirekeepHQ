import DOMPurify from "dompurify";
import { CheckCircle2, Copy, Maximize2, Minus, Plus, RotateCcw, X } from "lucide-react";
import { useEffect, useId, useState } from "react";

interface MermaidDiagramProps {
  readonly source: string;
}

export function MermaidDiagram({ source }: MermaidDiagramProps): React.JSX.Element {
  const reactId = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const [svg, setSvg] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const theme = document.documentElement.dataset.theme === "light" ? "default" : "dark";

  useEffect(() => {
    let active = true;
    setSvg("");
    setError(null);
    void import("mermaid").then(async ({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme,
        suppressErrorRendering: true,
        flowchart: { htmlLabels: false, useMaxWidth: true },
      });
      const rendered = await mermaid.render(`firekeep-diagram-${reactId}`, source);
      if (!active) return;
      const clean = DOMPurify.sanitize(rendered.svg, {
        USE_PROFILES: { svg: true, svgFilters: true },
        FORBID_TAGS: ["foreignObject", "script"],
        FORBID_ATTR: ["href", "xlink:href", "onclick", "onload"],
      });
      setSvg(clean);
    }).catch((caught: unknown) => {
      if (!active) return;
      setError(cleanError(caught));
    });
    return () => { active = false; };
  }, [reactId, source, theme]);

  useEffect(() => {
    if (!expanded) return undefined;
    const onKeyDown = (event: KeyboardEvent): void => { if (event.key === "Escape") setExpanded(false); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [expanded]);

  const copy = (): void => {
    void window.firekeepStudio.invoke({ type: "clipboard.write", text: source }).then((result) => {
      if (result.type !== "clipboard-written") throw new Error("clipboard returned an unexpected response");
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1_200);
    });
  };
  const controls = (
    <div className="diagram-actions">
      <button type="button" title="Zoom out" aria-label="Zoom diagram out" onClick={() => setZoom((value) => Math.max(0.55, value - 0.15))}><Minus size={14} /></button>
      <button type="button" title="Reset zoom" aria-label="Reset diagram zoom" onClick={() => setZoom(1)}><RotateCcw size={13} /><span>{Math.round(zoom * 100)}%</span></button>
      <button type="button" title="Zoom in" aria-label="Zoom diagram in" onClick={() => setZoom((value) => Math.min(2.2, value + 0.15))}><Plus size={14} /></button>
      <button type="button" title="Copy Mermaid source" aria-label="Copy Mermaid source" onClick={copy}>{copied ? <CheckCircle2 size={14} /> : <Copy size={14} />}</button>
      {!expanded ? <button type="button" title="Expand diagram" aria-label="Expand diagram" onClick={() => setExpanded(true)}><Maximize2 size={14} /></button> : null}
    </div>
  );
  const canvas = svg
    ? <div className="diagram-viewport"><div className="diagram-svg" style={{ zoom }} dangerouslySetInnerHTML={{ __html: svg }} /></div>
    : error
      ? <div className="diagram-error"><strong>Diagram could not be rendered</strong><span>{error}</span><pre>{source}</pre></div>
      : <div className="diagram-loading"><span className="pulse-dot" /> Rendering diagram…</div>;

  return <>
    <figure className="mermaid-diagram" aria-label="Rendered Mermaid diagram">
      <figcaption><span>Diagram</span>{controls}</figcaption>
      {canvas}
    </figure>
    {expanded ? <div className="diagram-overlay" role="dialog" aria-modal="true" aria-label="Expanded diagram" onMouseDown={(event) => { if (event.target === event.currentTarget) setExpanded(false); }}>
      <section className="diagram-expanded"><header><strong>Diagram</strong>{controls}<button type="button" className="diagram-close" aria-label="Close expanded diagram" onClick={() => setExpanded(false)}><X size={17} /></button></header>{canvas}</section>
    </div> : null}
  </>;
}

function cleanError(value: unknown): string {
  const message = value instanceof Error ? value.message : String(value);
  return message.replace(/^Error:\s*/i, "").split("\n")[0]?.slice(0, 240) || "Invalid Mermaid syntax";
}
