import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, it, vi } from "vitest";
import { FakeRuntime } from "./helpers/fake-runtime.js";
import { RuntimeRegistry } from "../src/core/runtime-registry.js";
import { MemorySessionStore, parseSessionWorktree, type SessionWorktree, type WorktreeManager } from "../src/core/session-store.js";
import { MemorySettingsStore } from "../src/core/settings-store.js";
import { StudioService, type StudioPersistedState } from "../src/core/studio-service.js";
import { SessionWorktrees } from "../src/main/session-worktrees.js";

const hasGit = (() => { try { execFileSync("git", ["--version"], { stdio: "ignore" }); return true; } catch { return false; } })();
const temp = realpathSync(mkdtempSync(join(tmpdir(), "studio-worktrees-")));
afterAll(() => rmSync(temp, { recursive: true, force: true, maxRetries: 3 }));

function git(cwd: string, ...args: string[]): string {
  return execFileSync("git", ["-c", "user.name=Studio Test", "-c", "user.email=studio@example.invalid", "-c", "commit.gpgsign=false", ...args], { cwd, encoding: "utf8" }).trim();
}

function repository(name: string): string {
  const repo = join(temp, name);
  mkdirSync(join(repo, "packages", "app"), { recursive: true });
  git(repo, "init", "-q");
  writeFileSync(join(repo, "packages", "app", "index.txt"), "one\n");
  git(repo, "add", "-A");
  git(repo, "commit", "-q", "-m", "first");
  return repo;
}

describe.skipIf(!hasGit)("SessionWorktrees", () => {
  it("creates a worktree on its own branch, opening the same subfolder, and removes it when clean", async () => {
    const repo = repository("clean");
    const worktrees = new SessionWorktrees(join(temp, "owned-clean"));
    const worktree = await worktrees.create("session-abcdef012345-rest", join(repo, "packages", "app"));
    expect(worktree.branch).toBe("studio/abcdef012345");
    expect(worktree.path).toBe(join(worktree.root, "packages", "app"));
    expect(existsSync(join(worktree.path, "index.txt"))).toBe(true);
    expect(git(worktree.root, "rev-parse", "--abbrev-ref", "HEAD")).toBe("studio/abcdef012345");
    expect(await worktrees.exists(worktree)).toBe(true);

    expect(await worktrees.remove(worktree)).toBeNull();
    expect(existsSync(worktree.root)).toBe(false);
    expect(git(repo, "branch", "--list", "studio/*")).toBe("");
  });

  it("leaves a worktree with uncommitted work in place, and keeps a branch that has commits", async () => {
    const repo = repository("dirty");
    const worktrees = new SessionWorktrees(join(temp, "owned-dirty"));
    const dirty = await worktrees.create("session-dirty", repo);
    writeFileSync(join(dirty.root, "new.txt"), "unsaved\n");
    expect(await worktrees.remove(dirty)).toContain("left at");
    expect(existsSync(join(dirty.root, "new.txt"))).toBe(true);

    const committed = await worktrees.create("session-committed", repo);
    writeFileSync(join(committed.root, "work.txt"), "kept\n");
    git(committed.root, "add", "-A");
    git(committed.root, "commit", "-q", "-m", "agent work");
    expect(await worktrees.remove(committed)).toContain("stay on branch studio/committed");
    expect(existsSync(committed.root)).toBe(false);
    expect(git(repo, "branch", "--list", "studio/committed")).toContain("studio/committed");
  });

  it("refuses a folder that is not a repository, a repository with no commits, and a path it does not own", async () => {
    const worktrees = new SessionWorktrees(join(temp, "owned-refuse"));
    const plain = join(temp, "plain");
    mkdirSync(plain);
    // The temp folder may itself sit inside a repository on a developer machine; a ceiling keeps git from looking up.
    const prior = process.env.GIT_CEILING_DIRECTORIES;
    process.env.GIT_CEILING_DIRECTORIES = temp;
    try {
      await expect(worktrees.create("session-plain", plain)).rejects.toThrow("not a git repository");
    } finally {
      if (prior === undefined) delete process.env.GIT_CEILING_DIRECTORIES; else process.env.GIT_CEILING_DIRECTORIES = prior;
    }
    const empty = join(temp, "empty");
    mkdirSync(empty);
    git(empty, "init", "-q");
    await expect(worktrees.create("session-empty", empty)).rejects.toThrow("no commits yet");
    await expect(worktrees.create("../escape", empty)).rejects.toThrow("invalid session id");
    expect(await worktrees.remove({ path: plain, root: plain, branch: "studio/x", source: empty })).toContain("not created by Studio");
    expect(existsSync(plain)).toBe(true);
  });
});

describe("worktree sessions in StudioService", () => {
  function setup() {
    const alpha = new FakeRuntime({ id: "alpha", displayName: "Alpha", description: "Alpha agent", transport: "test", capabilities: ["chat", "streaming"] });
    const sessions = new MemorySessionStore();
    const present = new Set<string>();
    const removed: SessionWorktree[] = [];
    const worktrees: WorktreeManager = {
      create: vi.fn(async (sessionId: string, workspacePath: string) => {
        if (workspacePath.includes("plain")) throw new Error("this workspace is not a git repository, so a session cannot get its own worktree");
        const worktree = { path: `W:\\${sessionId}\\repo`, root: `W:\\${sessionId}\\repo`, branch: `studio/${sessionId}`, source: workspacePath };
        present.add(worktree.root);
        return worktree;
      }),
      remove: vi.fn(async (worktree: SessionWorktree) => { removed.push(worktree); present.delete(worktree.root); return null; }),
      exists: vi.fn(async (worktree: SessionWorktree) => present.has(worktree.root)),
    };
    const service = new StudioService({
      runtimes: new RuntimeRegistry([alpha]),
      settings: new MemorySettingsStore<StudioPersistedState>(),
      sessions,
      worktrees,
      idFactory: (() => { let value = 0; return (prefix) => `${prefix}-${++value}`; })(),
      now: () => "2026-11-10T00:00:00.000Z",
    });
    return { alpha, sessions, service, worktrees, present, removed };
  }

  it("runs the session's agent in its worktree while other sessions keep the shared folder", async () => {
    const { alpha, sessions, service } = setup();
    await service.initialize();
    await service.setPrimary("alpha");
    await service.setWorkspace("C:\\repo");
    const shared = service.snapshot().activeSessionId;
    await service.startNewSession({ worktree: true });
    const isolated = service.snapshot();
    expect(isolated.worktree).toMatchObject({ branch: `studio/${isolated.activeSessionId}`, source: "C:\\repo" });
    expect(isolated.workspacePath).toBe(isolated.worktree?.path);
    expect((await sessions.list()).find((item) => item.id === isolated.activeSessionId)?.worktree).toEqual(isolated.worktree);

    await service.sendMessage("work apart");
    expect(alpha.runs.at(-1)?.cwd).toBe(isolated.worktree?.path);
    await expect(service.setWorkspace("C:\\elsewhere")).rejects.toThrow("its own worktree");

    await service.resumeSession(shared);
    expect(service.snapshot()).toMatchObject({ worktree: null, workspacePath: "C:\\repo" });
    await service.sendMessage("work together");
    expect(alpha.runs.at(-1)?.cwd).toBe("C:\\repo");

    await service.resumeSession(isolated.activeSessionId);
    expect(service.snapshot().workspacePath).toBe(isolated.worktree?.path);
  });

  it("leaves the open session alone when the workspace cannot have a worktree", async () => {
    const { service } = setup();
    await service.initialize();
    await expect(service.startNewSession({ worktree: true })).rejects.toThrow("choose a workspace folder");
    await service.setWorkspace("C:\\plain");
    const before = service.snapshot().activeSessionId;
    await expect(service.startNewSession({ worktree: true })).rejects.toThrow("not a git repository");
    expect(service.snapshot()).toMatchObject({ activeSessionId: before, worktree: null });
  });

  it("removes the worktree with its session and refuses to run once the worktree is gone", async () => {
    const { service, present, removed } = setup();
    await service.initialize();
    await service.setPrimary("alpha");
    await service.setWorkspace("C:\\repo");
    await service.startNewSession({ worktree: true });
    const first = service.snapshot();
    present.clear();
    await expect(service.sendMessage("anyone there")).rejects.toThrow("worktree is gone");

    expect(await service.deleteSession(first.activeSessionId, first.activeSessionId)).toBeNull();
    expect(removed).toEqual([first.worktree]);
    expect(service.snapshot().worktree).toBeNull();
  });

  it("only trusts a complete stored worktree record", () => {
    expect(parseSessionWorktree({ path: "a", root: "b", branch: "c", source: "d" })).toEqual({ path: "a", root: "b", branch: "c", source: "d" });
    expect(parseSessionWorktree({ path: "a", root: "", branch: "c", source: "d" })).toBeNull();
    expect(parseSessionWorktree("nope")).toBeNull();
  });
});
