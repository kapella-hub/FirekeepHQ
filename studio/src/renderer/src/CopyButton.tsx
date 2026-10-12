import { useState } from "react";
import { CheckCircle2, Copy } from "lucide-react";
import { ignore } from "./util.js";

export function CopyButton({ text, label = "Copy response" }: { readonly text: string; readonly label?: string }): React.JSX.Element {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const copy = async (): Promise<void> => {
    try {
      const result = await window.firekeepStudio.invoke({ type: "clipboard.write", text });
      if (result.type !== "clipboard-written") throw new Error("clipboard returned an unexpected response");
      setState("copied");
      window.setTimeout(() => setState("idle"), 1_200);
    } catch {
      setState("failed");
      window.setTimeout(() => setState("idle"), 1_800);
    }
  };
  return <button type="button" className={`copy-button ${state}`} aria-label={label} title={state === "failed" ? "Copy failed" : state === "copied" ? "Copied" : label} onClick={() => ignore(copy())}>{state === "copied" ? <CheckCircle2 size={14} /> : <Copy size={14} />}</button>;
}
