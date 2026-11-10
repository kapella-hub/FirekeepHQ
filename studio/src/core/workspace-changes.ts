/**
 * The review surface's model of the workspace: which files differ from the last commit,
 * what each diff says, and the feedback a human writes against it.
 *
 * Pure parsing and formatting only. The git process lives in `main/workspace-changes.ts`.
 */
export type ChangeStatus = "modified" | "added" | "deleted" | "renamed" | "untracked" | "conflicted";

export interface ChangedFile {
  /** Repository-relative path with forward slashes, exactly as git reports it. */
  readonly path: string;
  /** The path before a rename. */
  readonly previousPath?: string;
  readonly status: ChangeStatus;
  /** Every change to this file is in the index: the human accepted it. */
  readonly accepted: boolean;
  /** Line counts against the last commit; null when git cannot count (binary, untracked). */
  readonly additions: number | null;
  readonly deletions: number | null;
}

export type WorkspaceChangesState = "ok" | "no-workspace" | "not-a-repo" | "no-git";

export interface WorkspaceChanges {
  readonly state: WorkspaceChangesState;
  readonly branch: string | null;
  readonly files: readonly ChangedFile[];
  /** True when the list was cut at `MAX_CHANGED_FILES`. */
  readonly truncated: boolean;
}

export interface FileDiff {
  readonly path: string;
  readonly diff: string;
  readonly binary: boolean;
  /** True when the diff was cut at `MAX_DIFF_CHARS`. */
  readonly truncated: boolean;
}

export const MAX_CHANGED_FILES = 500;
export const MAX_DIFF_CHARS = 400_000;

export const EMPTY_CHANGES: WorkspaceChanges = { state: "no-workspace", branch: null, files: [], truncated: false };

/** Parses `git status --porcelain=v1 -z`. Ignored entries are dropped. */
export function parsePorcelainStatus(output: string): ChangedFile[] {
  const fields = output.split("\0");
  const files: ChangedFile[] = [];
  for (let index = 0; index < fields.length; index += 1) {
    const entry = fields[index] as string;
    if (entry.length < 4) continue;
    const x = entry[0] as string;
    const y = entry[1] as string;
    const path = entry.slice(3);
    // A rename or copy carries its source path as the next NUL-separated field.
    const hasSource = x === "R" || x === "C" || y === "R" || y === "C";
    const previousPath = hasSource ? fields[index + 1] : undefined;
    if (hasSource) index += 1;
    if (x === "!" && y === "!") continue;
    const untracked = x === "?" && y === "?";
    const conflicted = x === "U" || y === "U" || (x === "A" && y === "A") || (x === "D" && y === "D");
    const status: ChangeStatus = untracked ? "untracked"
      : conflicted ? "conflicted"
        : x === "R" || y === "R" ? "renamed"
          : x === "D" || y === "D" ? "deleted"
            : x === "A" || x === "C" ? "added"
              : "modified";
    files.push({
      path,
      ...(previousPath ? { previousPath } : {}),
      status,
      accepted: !untracked && !conflicted && x !== " " && y === " ",
      additions: null,
      deletions: null,
    });
  }
  return files;
}

/** Parses `git diff --numstat -z` into counts by (new) path. Binary files report null counts. */
export function parseNumstat(output: string): Map<string, { additions: number | null; deletions: number | null }> {
  const counts = new Map<string, { additions: number | null; deletions: number | null }>();
  const fields = output.split("\0");
  for (let index = 0; index < fields.length; index += 1) {
    const match = /^(\d+|-)\t(\d+|-)\t(.*)$/s.exec(fields[index] as string);
    if (!match) continue;
    let path = match[3] as string;
    if (path === "") {
      // Rename: "adds\tdels\t" NUL old NUL new
      path = fields[index + 2] ?? "";
      index += 2;
    }
    if (!path) continue;
    counts.set(path, {
      additions: match[1] === "-" ? null : Number(match[1]),
      deletions: match[2] === "-" ? null : Number(match[2]),
    });
  }
  return counts;
}

export function withCounts(files: readonly ChangedFile[], counts: ReadonlyMap<string, { additions: number | null; deletions: number | null }>): ChangedFile[] {
  return files.map((file) => {
    const found = counts.get(file.path);
    return found ? { ...file, additions: found.additions, deletions: found.deletions } : file;
  });
}

export type DiffLineKind = "context" | "add" | "remove" | "hunk" | "meta";

export interface DiffLine {
  readonly kind: DiffLineKind;
  readonly text: string;
  /** Line number in the old file (context and removed lines). */
  readonly oldLine: number | null;
  /** Line number in the new file (context and added lines). */
  readonly newLine: number | null;
}

/** Splits one file's unified diff into typed, numbered lines for display and line comments. */
export function parseUnifiedDiff(diff: string): DiffLine[] {
  const lines: DiffLine[] = [];
  let oldLine = 0;
  let newLine = 0;
  let inHunk = false;
  const source = diff.endsWith("\n") ? diff.slice(0, -1) : diff;
  if (!source) return lines;
  for (const text of source.split("\n")) {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text);
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      inHunk = true;
      lines.push({ kind: "hunk", text, oldLine: null, newLine: null });
    } else if (!inHunk || text.startsWith("\\")) {
      lines.push({ kind: "meta", text, oldLine: null, newLine: null });
    } else if (text.startsWith("+")) {
      lines.push({ kind: "add", text: text.slice(1), oldLine: null, newLine });
      newLine += 1;
    } else if (text.startsWith("-")) {
      lines.push({ kind: "remove", text: text.slice(1), oldLine, newLine: null });
      oldLine += 1;
    } else {
      lines.push({ kind: "context", text: text.slice(1), oldLine, newLine });
      oldLine += 1;
      newLine += 1;
    }
  }
  return lines;
}

/** A unified diff for a file git does not track yet: every line is an addition. */
export function untrackedDiff(path: string, content: string): string {
  if (content === "") return `--- /dev/null\n+++ b/${path}\n`;
  const body = content.endsWith("\n") ? content.slice(0, -1) : content;
  const lines = body.split("\n");
  return `--- /dev/null\n+++ b/${path}\n@@ -0,0 +1,${lines.length} @@\n${lines.map((line) => `+${line}`).join("\n")}\n`;
}

export interface ReviewComment {
  readonly path: string;
  /** Line in the new file, or null for a comment on the whole file. */
  readonly line: number | null;
  /** The line's text, quoted back so the agent can find it even after the file moves. */
  readonly quote?: string;
  readonly text: string;
}

/** The message Studio sends to the agent when the human submits review comments. */
export function reviewPrompt(comments: readonly ReviewComment[]): string {
  const kept = comments.filter((comment) => comment.text.trim().length > 0);
  if (kept.length === 0) return "";
  const byPath = new Map<string, ReviewComment[]>();
  for (const comment of kept) byPath.set(comment.path, [...(byPath.get(comment.path) ?? []), comment]);
  const sections: string[] = [];
  for (const [path, entries] of byPath) {
    const lines = [`### \`${path}\``];
    for (const entry of [...entries].sort((a, b) => (a.line ?? 0) - (b.line ?? 0))) {
      const where = entry.line === null ? "Whole file" : `Line ${entry.line}`;
      const quote = entry.quote?.trim() ? ` (\`${entry.quote.trim().slice(0, 160).replace(/`/g, "'")}\`)` : "";
      lines.push(`- **${where}**${quote}: ${entry.text.trim()}`);
    }
    sections.push(lines.join("\n"));
  }
  const count = `${kept.length} comment${kept.length === 1 ? "" : "s"}`;
  return `I reviewed your changes in the workspace and left ${count}. Address each one, then summarise what you changed.\n\n${sections.join("\n\n")}`;
}
