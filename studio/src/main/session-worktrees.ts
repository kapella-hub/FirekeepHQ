import { mkdir, rmdir, stat } from "node:fs/promises";
import { basename, dirname, isAbsolute, join, relative, resolve } from "node:path";
import type { SessionWorktree, WorktreeManager } from "../core/session-store.js";
import { gitRunner, type GitRunner } from "./workspace-changes.js";

const SESSION_ID = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/;

/**
 * Gives a session its own git worktree under a folder Studio owns, so its agent edits files
 * no other session touches. The worktree starts from the repository's current commit on a new
 * `studio/<id>` branch; uncommitted changes in the original folder are not carried over.
 *
 * Nothing here ever forces: a worktree with uncommitted or untracked files is left in place
 * when its session is deleted, and a branch is deleted only when git reports it fully merged.
 */
export class SessionWorktrees implements WorktreeManager {
  readonly #base: string;
  readonly #git: GitRunner;

  constructor(base: string, git: GitRunner = gitRunner(120_000)) {
    this.#base = resolve(base);
    this.#git = git;
  }

  async create(sessionId: string, workspacePath: string): Promise<SessionWorktree> {
    if (!SESSION_ID.test(sessionId)) throw new Error(`invalid session id: ${sessionId}`);
    const top = await this.#git(workspacePath, ["rev-parse", "--show-toplevel"]);
    if (top.missing) throw new Error("git is not installed, so Studio cannot create a worktree");
    if (top.code !== 0 || !top.stdout.trim()) throw new Error("this workspace is not a git repository, so a session cannot get its own worktree");
    const source = resolve(top.stdout.trim());
    const head = await this.#git(source, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"]);
    if (head.code !== 0) throw new Error("this repository has no commits yet; commit once before starting a worktree session");
    // Where the workspace sits inside its repository, so the session opens the same subfolder.
    const prefix = (await this.#git(workspacePath, ["rev-parse", "--show-prefix"])).stdout.trim().replace(/\/+$/, "");
    const root = join(this.#base, sessionId, basename(source) || "workspace");
    const branch = `studio/${sessionId.replace(/^session-/, "").replace(/-/g, "").slice(0, 12)}`;
    await mkdir(dirname(root), { recursive: true });
    // Hooks are repository content an agent can edit; creating a worktree must not run one.
    const added = await this.#git(source, ["-c", `core.hooksPath=${join(this.#base, ".no-hooks")}`, "worktree", "add", "-b", branch, root, "HEAD"]);
    if (added.code !== 0) throw new Error(`git could not create the worktree: ${lastLine(added.stderr) || "unknown error"}`);
    const path = prefix ? join(root, ...prefix.split("/")) : root;
    if (!this.#inside(path)) throw new Error("the workspace folder resolves outside its worktree");
    await mkdir(path, { recursive: true });
    return { path, root, branch, source };
  }

  async remove(worktree: SessionWorktree): Promise<string | null> {
    if (!this.#inside(worktree.root)) return `Its worktree at ${worktree.root} was not created by Studio and was left in place.`;
    if (await this.exists(worktree)) {
      const removed = await this.#git(worktree.source, ["worktree", "remove", worktree.root]);
      if (removed.code !== 0) return `Its worktree has uncommitted changes and was left at ${worktree.root} (branch ${worktree.branch}).`;
    } else {
      await this.#git(worktree.source, ["worktree", "prune"]);
    }
    await rmdir(dirname(worktree.root)).catch(() => undefined);
    // -d refuses a branch whose commits are not merged, which is exactly the work worth keeping.
    const branch = await this.#git(worktree.source, ["branch", "-d", worktree.branch]);
    return branch.code === 0 ? null : `Its commits stay on branch ${worktree.branch} in ${worktree.source}.`;
  }

  async exists(worktree: SessionWorktree): Promise<boolean> {
    return stat(worktree.root).then((entry) => entry.isDirectory(), () => false);
  }

  #inside(path: string): boolean {
    const offset = relative(this.#base, resolve(path));
    return offset.length > 0 && !offset.startsWith("..") && !isAbsolute(offset);
  }
}

function lastLine(text: string): string {
  return text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).at(-1) ?? "";
}
