const VISUAL_INTENT = /\b(?:diagram|flowchart|graph|mind\s*map|mindmap|sequence|topology|visuali[sz](?:e|ation)|chart)\b/i;

const STUDIO_VISUAL_HINT = [
  "[Firekeep Studio visual]",
  "When a diagram clarifies this answer, include it as a fenced `mermaid` block.",
  "Studio renders Mermaid natively; do not substitute ASCII art for the diagram.",
].join(" ");

export function withStudioVisualHint(prompt: string): string {
  if (!VISUAL_INTENT.test(prompt) || /```\s*mermaid\b/i.test(prompt)) return prompt;
  return `${prompt}\n\n${STUDIO_VISUAL_HINT}`;
}
