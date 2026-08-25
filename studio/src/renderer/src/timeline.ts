import type { MessageRole, RuntimeEvent, RuntimeUsage } from "../../core/runtime.js";

interface TimelineBase {
  readonly id: string;
  readonly runId: string;
  readonly runtimeId: string;
  readonly timestamp: string;
}

export type TimelineItem =
  | TimelineBase & { readonly kind: "message"; readonly role: MessageRole; readonly text: string; readonly complete: boolean }
  | TimelineBase & { readonly kind: "reasoning"; readonly text: string }
  | TimelineBase & { readonly kind: "tool"; readonly name: string; readonly summary: string; readonly status: "running" | "completed" | "failed"; readonly input?: unknown; readonly output?: unknown; readonly update?: string }
  | TimelineBase & { readonly kind: "diff"; readonly diff: string }
  | TimelineBase & { readonly kind: "notice"; readonly level: "info" | "warning" | "error"; readonly message: string; readonly detail?: string }
  | TimelineBase & { readonly kind: "approval"; readonly approvalId: string; readonly title: string; readonly detail: string; readonly options: readonly string[]; readonly decision?: string }
  | TimelineBase & { readonly kind: "usage"; readonly usage: RuntimeUsage };

export type MessageTimelineItem = Extract<TimelineItem, { readonly kind: "message" }>;

export interface TimelineRun {
  readonly id: string;
  readonly runId: string;
  readonly runtimeId: string;
  readonly timestamp: string;
  readonly messages: readonly MessageTimelineItem[];
  readonly attention: readonly TimelineItem[];
  readonly activity: readonly TimelineItem[];
}

export function buildTimeline(events: readonly RuntimeEvent[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  const positions = new Map<string, number>();
  const upsert = (key: string, item: TimelineItem): void => {
    const position = positions.get(key);
    if (position === undefined) {
      positions.set(key, items.length);
      items.push(item);
    } else items[position] = item;
  };

  for (const event of events) {
    const payload = event.payload;
    const base = { id: event.id, runId: event.runId, runtimeId: event.runtimeId, timestamp: event.timestamp };
    if (payload.kind === "message.delta" || payload.kind === "message.completed") {
      const key = `message:${event.runId}:${payload.messageId}:${payload.role}`;
      const existing = positions.get(key) === undefined ? undefined : items[positions.get(key) as number];
      const previous = existing?.kind === "message" ? existing.text : "";
      upsert(key, { ...base, kind: "message", role: payload.role, text: payload.kind === "message.delta" ? previous + payload.text : payload.text, complete: payload.kind === "message.completed" });
    } else if (payload.kind === "reasoning.delta") {
      const key = `reasoning:${event.runId}:${payload.itemId}`;
      const existing = positions.get(key) === undefined ? undefined : items[positions.get(key) as number];
      upsert(key, { ...base, kind: "reasoning", text: (existing?.kind === "reasoning" ? existing.text : "") + payload.text });
    } else if (payload.kind === "tool.started") {
      upsert(`tool:${event.runId}:${payload.toolCallId}`, { ...base, kind: "tool", name: payload.name, summary: describeTool(payload.name, payload.input, "running"), status: "running", ...(payload.input === undefined ? {} : { input: payload.input }) });
    } else if (payload.kind === "tool.updated") {
      const key = `tool:${event.runId}:${payload.toolCallId}`;
      const existing = positions.get(key) === undefined ? undefined : items[positions.get(key) as number];
      const name = existing?.kind === "tool" ? existing.name : "Tool";
      const input = existing?.kind === "tool" ? existing.input : undefined;
      upsert(key, { ...base, kind: "tool", name, summary: describeTool(name, input, "running"), status: "running", ...(input !== undefined ? { input } : {}), update: payload.update });
    } else if (payload.kind === "tool.completed") {
      const key = `tool:${event.runId}:${payload.toolCallId}`;
      const existing = positions.get(key) === undefined ? undefined : items[positions.get(key) as number];
      const status = payload.failed ? "failed" : "completed";
      const input = existing?.kind === "tool" ? existing.input : undefined;
      upsert(key, { ...base, kind: "tool", name: payload.name, summary: describeTool(payload.name, input, status), status, ...(input !== undefined ? { input } : {}), ...(payload.output === undefined ? {} : { output: payload.output }) });
    } else if (payload.kind === "diff.updated") {
      upsert(`diff:${event.runId}:${payload.itemId}`, { ...base, kind: "diff", diff: payload.diff });
    } else if (payload.kind === "usage.updated") {
      upsert(`usage:${event.runId}`, { ...base, kind: "usage", usage: payload.usage });
    } else if (payload.kind === "notice") {
      items.push({ ...base, kind: "notice", level: payload.level, message: payload.message, ...(payload.detail ? { detail: payload.detail } : {}) });
    } else if (payload.kind === "run.failed") {
      items.push({ ...base, kind: "notice", level: payload.cancelled ? "warning" : "error", message: payload.cancelled ? "Run cancelled" : "Run failed", detail: payload.error });
    } else if (payload.kind === "approval.requested") {
      upsert(`approval:${payload.approvalId}`, { ...base, kind: "approval", approvalId: payload.approvalId, title: payload.title, detail: payload.detail, options: payload.options });
    } else if (payload.kind === "approval.resolved") {
      const key = `approval:${payload.approvalId}`;
      const existing = positions.get(key) === undefined ? undefined : items[positions.get(key) as number];
      if (existing?.kind === "approval") upsert(key, { ...existing, decision: payload.decision });
    }
  }
  return items;
}

export function groupTimeline(items: readonly TimelineItem[]): TimelineRun[] {
  const runs: Array<{
    id: string;
    runId: string;
    runtimeId: string;
    timestamp: string;
    messages: MessageTimelineItem[];
    attention: TimelineItem[];
    activity: TimelineItem[];
  }> = [];
  const positions = new Map<string, number>();
  for (const item of items) {
    const key = `${item.runId}:${item.runtimeId}`;
    let position = positions.get(key);
    if (position === undefined) {
      position = runs.length;
      positions.set(key, position);
      runs.push({ id: key, runId: item.runId, runtimeId: item.runtimeId, timestamp: item.timestamp, messages: [], attention: [], activity: [] });
    }
    const run = runs[position] as (typeof runs)[number];
    if (item.kind === "message") run.messages.push(item);
    else if (item.kind === "approval" || (item.kind === "notice" && item.level !== "info")) run.attention.push(item);
    else run.activity.push(item);
  }
  return runs;
}

type ToolStatus = "running" | "completed" | "failed";

function describeTool(name: string, input: unknown, status: ToolStatus): string {
  const lowerName = name.toLowerCase();
  const command = commandFrom(input).toLowerCase();
  if (command || /bash|shell|powershell|terminal|exec_command|run_command/.test(lowerName)) {
    if (command.trim() === "test" || /\b(npm|pnpm|yarn|bun)\s+(run\s+)?test\b|\b(vitest|pytest|go\s+test|cargo\s+test|dotnet\s+test)\b/.test(command)) return tense(status, "Running tests", "Ran tests", "Tests failed");
    if (/\bgit\s+status\b/.test(command)) return tense(status, "Checking repository status", "Checked repository status", "Repository status check failed");
    if (/\bgit\s+diff\b/.test(command)) return tense(status, "Reviewing workspace changes", "Reviewed workspace changes", "Workspace diff failed");
    if (/\b(npm|pnpm|yarn|bun)\s+(run\s+)?(build|dist|package)\b|\b(cargo|go|dotnet)\s+build\b/.test(command)) return tense(status, "Building the project", "Built the project", "Project build failed");
    if (/\b(tsc|typecheck|type-check|mypy|pyright)\b/.test(command)) return tense(status, "Checking types", "Checked types", "Type check failed");
    if (/\b(rg|grep|findstr|select-string)\b/.test(command)) return tense(status, "Searching the workspace", "Searched the workspace", "Workspace search failed");
    if (/\b(cat|type|get-content|sed|head|tail)\b/.test(command)) return tense(status, "Reading project files", "Read project files", "File read failed");
    if (/\b(ls|dir|get-childitem|find)\b/.test(command)) return tense(status, "Listing project files", "Listed project files", "File listing failed");
    if (/\b(npm|pnpm|yarn|bun)\s+(install|ci)\b/.test(command)) return tense(status, "Installing dependencies", "Installed dependencies", "Dependency install failed");
    return tense(status, "Running a shell command", "Ran a shell command", "Shell command failed");
  }
  if (lowerName.includes("memory_recall")) return tense(status, "Recalling team memory", "Recalled team memory", "Team-memory recall failed");
  if (lowerName.includes("skill_recall")) return tense(status, "Recalling team procedures", "Recalled team procedures", "Procedure recall failed");
  if (lowerName.includes("ctx_update")) return tense(status, "Updating working context", "Updated working context", "Context update failed");
  if (lowerName.includes("action_before")) return tense(status, "Declaring a consequential action", "Declared a consequential action", "Action declaration failed");
  if (lowerName.includes("action_after")) return tense(status, "Recording the action outcome", "Recorded the action outcome", "Action outcome recording failed");
  if (lowerName.includes("search_symbols") || lowerName.includes("similar_symbols")) return tense(status, "Searching project symbols", "Searched project symbols", "Symbol search failed");
  if (/web.*search|search_query/.test(lowerName)) return tense(status, "Searching the web", "Searched the web", "Web search failed");
  if (/apply_patch|\b(edit|write)\b/.test(lowerName)) return tense(status, "Editing project files", "Edited project files", "File edit failed");
  if (/\bread\b|view_file/.test(lowerName)) return tense(status, "Reading project files", "Read project files", "File read failed");
  const label = humanizeToolName(name);
  return tense(status, `Using ${label}`, `Used ${label}`, `${label} failed`);
}

function commandFrom(input: unknown): string {
  if (typeof input === "string") return input;
  if (!input || typeof input !== "object" || Array.isArray(input)) return "";
  const record = input as Record<string, unknown>;
  for (const key of ["command", "cmd", "script"]) if (typeof record[key] === "string") return record[key];
  return "";
}

function humanizeToolName(name: string): string {
  const leaf = name.split("__").at(-1) ?? name;
  return leaf.replace(/[_-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").trim().toLowerCase() || "a tool";
}

function tense(status: ToolStatus, running: string, completed: string, failed: string): string {
  return status === "running" ? running : status === "failed" ? failed : completed;
}
