import { Code2 } from "lucide-react";
import type { CommandResult } from "../../../core/slash-commands.js";
import { RichMarkdown } from "../RichMarkdown.js";

export interface CommandCard { readonly id: string; readonly input: string; readonly result: CommandResult }

export function CommandResultCard({ card }: { readonly card: CommandCard }): React.JSX.Element {
  return <article className={`command-card ${card.result.tone ?? "neutral"}`}><header><Code2 size={15} /><code>{card.input}</code><span>{card.result.title}</span></header>{card.result.kind === "table" && card.result.rows ? <div className="table-scroll"><table><tbody>{card.result.rows.map((row, index) => <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody></table></div> : <div className="markdown"><RichMarkdown>{card.result.body}</RichMarkdown></div>}</article>;
}
