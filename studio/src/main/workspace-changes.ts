import { execFile } from "node:child_process";
import { readFile, rm } from "node:fs/promises";
import { isAbsolute, relative, resolve } from "node:path";
import {
  EMPTY_CHANGES,
  MAX_CHANGED_FILES,
  MAX_DIFF_CHARS,
  parseNumstat,
  parsePorcelainStatus,
  untrackedDiff,
  withCounts,
  type ChangedFile,
  type FileDiff,
  type WorkspaceChanges,
} from "../core/workspace-changes.js";

export interface GitResult {
  readonly code: number;
  readonly stdout: string;
  readonly stderr: string;
  /** True when the git executable itself could not be started. */
  readonly missing: boolean;
}

export type GitRunner = (cwd: string, args: readonly string[]) => Promise<GitResult>;

// Repository configuration is workspace content, and an agent can edit it. These overrides keep a
// read of the workspace from running a configured helper or pager (diffs also pass --no-ext-diff).
const SAFE_CONFIG = ["-c", "core.fsmonitor=false", "-c", "core.quotepath=off", "-c", "core.pager=cat", "-c", "color.ui=never"];

export const gitRunner = (timeout: number): GitRunner => (cwd, args) => new Promise((resolvePromise) => {
  execFile("git", [...SAFE_CONFIG, ...args], {
    cwd,
    windowsHide: true,
    timeout,
    maxBuffer: 16 * 1024 * 1024,
    encoding: "utf8",
    env: { ...process.env, GIT_OPTIONAL_LOCKS: "0", GIT_TERMINAL_PROMPT: "0", GIT_PAGER: "cat" },
  }, (error, stdout, stderr) => {
    const failure = error as (NodeJS.ErrnoException & { code?: string | number }) | null;
    if (failure && failure.code === "ENOENT") resolvePromise({ code: 127, stdout: "", stderr: "git is not installed", missing: true });
    else resolvePromise({ code: failure ? (typeof failure.code === "number" ? failure.code : 1) : 0, stdout: String(stdout), stderr: String(stderr), missing: false });
  });
});

export const runGit: GitRunner = gitRunner(20_000);

export interface WorkspaceChangesOptions {
  /** The folder the agents work in, or null when none is chosen. */
  readonly workspace: () => string | null;
  /** Asks the human before a file's changes are thrown away. */
  readonly confirmRevert: (file: ChangedFile) => Promise<boolean>;
  readonly git?: GitRunner;
}

interface Located {
  readonly root: string;
  /** Pathspec limiting every listing to the workspace folder inside the repository. */
  readonly scope: string;
}

/**
 * Reads and edits the workspace's uncommitted changes through git.
 *
 * Every path the renderer names is checked against a fresh `git status` first, so an action
 * can only ever touch a file git itself reports as changed inside the workspace.
 */
export class WorkspaceChangesService {
  readonly #options: WorkspaceChangesOptions;
  readonly #git: GitRunner;

  constructor(options: WorkspaceChangesOptions) {
    this.#options = options;
    this.#git = options.git ?? runGit;
  }

  async list(): Promise<WorkspaceChanges> {
    const workspace = this.#options.workspace();
    if (!workspace) return EMPTY_CHANGES;
    const located = await this.#locate(workspace);
    if (typeof located === "string") return { ...EMPTY_CHANGES, state: located };
    const status = await this.#git(located.root, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", located.scope]);
    if (status.code !== 0) throw new Error(gitFailure("read the workspace status", status));
    const all = parsePorcelainStatus(status.stdout);
    const numstat = await this.#git(located.root, ["diff", "HEAD", "--numstat", "-z", "--no-ext-diff", "--", located.scope]);
    const files = withCounts(all.slice(0, MAX_CHANGED_FILES), numstat.code === 0 ? parseNumstat(numstat.stdout) : new Map());
    const branch = await this.#git(located.root, ["branch", "--show-current"]);
    return { state: "ok", branch: branch.code === 0 ? branch.stdout.trim() || null : null, files, truncated: all.length > MAX_CHANGED_FILES };
  }

  async diff(path: string): Promise<FileDiff> {
    const { located, file } = await this.#require(path);
    if (file.status === "untracked") return this.#untracked(located, file);
    const result = await this.#git(located.root, ["diff", "HEAD", "--no-ext-diff", "--no-color", "-M", "--", ...(file.previousPath ? [file.previousPath] : []), file.path]);
    // A repository with no commit yet has no HEAD to compare against: show what is staged.
    const shown = result.code === 0 ? result : await this.#git(located.root, ["diff", "--cached", "--no-ext-diff", "--no-color", "--", file.path]);
    if (shown.code !== 0) throw new Error(gitFailure(`read the diff for ${file.path}`, shown));
    const binary = /^Binary files .* differ$/m.test(shown.stdout) || /^GIT binary patch$/m.test(shown.stdout);
    return { path: file.path, diff: shown.stdout.slice(0, MAX_DIFF_CHARS), binary, truncated: shown.stdout.length > MAX_DIFF_CHARS };
  }

  /** Accept: put the file's changes in the index. Nothing on disk changes. */
  async accept(path: string): Promise<WorkspaceChanges> {
    const { located, file } = await this.#require(path);
    if (file.status === "conflicted") throw new Error("Resolve the merge conflict in this file before accepting it");
    const result = await this.#git(located.root, ["add", "-A", "--", ...(file.previousPath ? [file.previousPath] : []), file.path]);
    if (result.code !== 0) throw new Error(gitFailure(`accept ${file.path}`, result));
    return this.list();
  }

  /** Undo an accept: take the file back out of the index. Nothing on disk changes. */
  async unaccept(path: string): Promise<WorkspaceChanges> {
    const { located, file } = await this.#require(path);
    const paths = [...(file.previousPath ? [file.previousPath] : []), file.path];
    const result = await this.#git(located.root, ["restore", "--staged", "--", ...paths]);
    // No commit yet: there is no HEAD to restore the index from.
    const done = result.code === 0 ? result : await this.#git(located.root, ["rm", "-q", "--cached", "--", ...paths]);
    if (done.code !== 0) throw new Error(gitFailure(`unaccept ${file.path}`, done));
    return this.list();
  }

  /** Revert: throw the file's changes away, after the human confirms in a native dialog. */
  async revert(path: string): Promise<WorkspaceChanges> {
    const { located, file } = await this.#require(path);
    if (file.status === "conflicted") throw new Error("Resolve the merge conflict in this file before reverting it");
    if (!await this.#options.confirmRevert(file)) return this.list();
    if (file.status === "untracked") {
      await rm(this.#inside(located, file.path), { force: true });
    } else if (file.status === "added") {
      const result = await this.#git(located.root, ["rm", "-q", "-f", "--", file.path]);
      if (result.code !== 0) throw new Error(gitFailure(`revert ${file.path}`, result));
    } else {
      if (file.status === "renamed" && file.previousPath) {
        const removed = await this.#git(located.root, ["rm", "-q", "-f", "--", file.path]);
        if (removed.code !== 0) await rm(this.#inside(located, file.path), { force: true });
      }
      const restored = await this.#git(located.root, ["restore", "--source=HEAD", "--staged", "--worktree", "--", file.previousPath ?? file.path]);
      if (restored.code !== 0) throw new Error(gitFailure(`revert ${file.path}`, restored));
    }
    return this.list();
  }

  async #untracked(located: Located, file: ChangedFile): Promise<FileDiff> {
    const buffer = await readFile(this.#inside(located, file.path));
    if (buffer.subarray(0, 8_000).includes(0)) return { path: file.path, diff: "", binary: true, truncated: false };
    const text = buffer.toString("utf8");
    const diff = untrackedDiff(file.path, text.slice(0, MAX_DIFF_CHARS));
    return { path: file.path, diff, binary: false, truncated: text.length > MAX_DIFF_CHARS };
  }

  async #require(path: string): Promise<{ located: Located; file: ChangedFile }> {
    const workspace = this.#options.workspace();
    if (!workspace) throw new Error("Choose a workspace first");
    const located = await this.#locate(workspace);
    if (typeof located === "string") throw new Error(located === "no-git" ? "git is not installed" : "The workspace is not a git repository");
    const status = await this.#git(located.root, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", located.scope]);
    if (status.code !== 0) throw new Error(gitFailure("read the workspace status", status));
    const file = parsePorcelainStatus(status.stdout).find((entry) => entry.path === path);
    if (!file) throw new Error("That file no longer has changes");
    return { located, file };
  }

  async #locate(workspace: string): Promise<Located | "no-git" | "not-a-repo"> {
    const top = await this.#git(workspace, ["rev-parse", "--show-toplevel"]);
    if (top.missing) return "no-git";
    const root = top.stdout.trim();
    if (top.code !== 0 || !root) return "not-a-repo";
    const inside = relative(resolve(root), resolve(workspace)).replace(/\\/g, "/");
    // A symlinked or differently-cased workspace can fail to relate to the root; then show the whole repository.
    const scope = inside === "" || inside.startsWith("..") || isAbsolute(inside) ? "." : inside;
    return { root: resolve(root), scope };
  }

  /** Absolute path of a repository-relative file, refused when it would leave the repository. */
  #inside(located: Located, path: string): string {
    const absolute = resolve(located.root, path);
    const back = relative(located.root, absolute);
    if (back === "" || back.startsWith("..") || isAbsolute(back)) throw new Error("That path is outside the workspace");
    return absolute;
  }
}

function gitFailure(what: string, result: GitResult): string {
  const detail = result.stderr.trim().split(/\r?\n/)[0] ?? "";
  return `Could not ${what}${detail ? `: ${detail}` : ""}`;
}
