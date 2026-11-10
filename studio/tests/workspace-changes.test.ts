import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { parseNumstat, parsePorcelainStatus, parseUnifiedDiff, reviewPrompt, untrackedDiff } from "../src/core/workspace-changes.js";
import { WorkspaceChangesService, type GitRunner } from "../src/main/workspace-changes.js";

describe("workspace change parsing", () => {
  it("reads porcelain status, including renames, untracked and conflicted files", () => {
    const files = parsePorcelainStatus(["M  staged.ts", " M dirty.ts", "MM both.ts", "A  new.ts", " D gone.ts", "R  moved/new name.ts", "old name.ts", "?? notes.md", "UU clash.ts", "!! ignored.log", ""].join("\0"));
    expect(files.map((file) => [file.path, file.status, file.accepted])).toEqual([
      ["staged.ts", "modified", true],
      ["dirty.ts", "modified", false],
      ["both.ts", "modified", false],
      ["new.ts", "added", true],
      ["gone.ts", "deleted", false],
      ["moved/new name.ts", "renamed", true],
      ["notes.md", "untracked", false],
      ["clash.ts", "conflicted", false],
    ]);
    expect(files[5]?.previousPath).toBe("old name.ts");
  });

  it("reads numstat counts, binary files and renames", () => {
    const counts = parseNumstat(["3\t1\ta.ts", "-\t-\tlogo.png", "2\t0\t", "old.ts", "new.ts", ""].join("\0"));
    expect(counts.get("a.ts")).toEqual({ additions: 3, deletions: 1 });
    expect(counts.get("logo.png")).toEqual({ additions: null, deletions: null });
    expect(counts.get("new.ts")).toEqual({ additions: 2, deletions: 0 });
  });

  it("numbers the lines of a unified diff", () => {
    const lines = parseUnifiedDiff("diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -4,3 +4,4 @@ function x() {\n keep\n-old\n+new\n+more\n tail\n\\ No newline at end of file\n");
    expect(lines.map((line) => [line.kind, line.oldLine, line.newLine])).toEqual([
      ["meta", null, null], ["meta", null, null], ["meta", null, null], ["hunk", null, null],
      ["context", 4, 4], ["remove", 5, null], ["add", null, 5], ["add", null, 6], ["context", 6, 7], ["meta", null, null],
    ]);
    expect(lines[6]?.text).toBe("new");
    expect(parseUnifiedDiff("")).toEqual([]);
  });

  it("renders an untracked file as additions and review comments as one message", () => {
    expect(untrackedDiff("n.txt", "a\nb\n")).toBe("--- /dev/null\n+++ b/n.txt\n@@ -0,0 +1,2 @@\n+a\n+b\n");
    const prompt = reviewPrompt([
      { path: "src/a.ts", line: 12, quote: "const x = `1`;", text: "Name this." },
      { path: "src/a.ts", line: null, text: "Split the file." },
      { path: "src/b.ts", line: 3, text: "  " },
    ]);
    expect(prompt).toContain("left 2 comments");
    expect(prompt).toContain("### `src/a.ts`\n- **Whole file**: Split the file.\n- **Line 12** (`const x = '1';`): Name this.");
    expect(prompt).not.toContain("src/b.ts");
    expect(reviewPrompt([])).toBe("");
  });
});

const gitAvailable = (() => { try { execFileSync("git", ["--version"], { stdio: "ignore" }); return true; } catch { return false; } })();
const roots: string[] = [];
afterEach(async () => { await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true }))); });

async function repository(): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), "studio-changes-"));
  roots.push(root);
  const git = (...args: string[]): void => { execFileSync("git", args, { cwd: root, stdio: "ignore" }); };
  git("init", "-q");
  git("config", "user.email", "studio@example.invalid");
  git("config", "user.name", "Studio Test");
  git("config", "core.autocrlf", "false");
  await mkdir(join(root, "src"));
  await writeFile(join(root, "src", "a.ts"), "one\ntwo\nthree\n");
  await writeFile(join(root, "src", "b.ts"), "keep\n");
  git("add", "-A");
  git("commit", "-q", "-m", "base");
  return root;
}

describe.skipIf(!gitAvailable)("WorkspaceChangesService", () => {
  it("lists, diffs, accepts and reverts the workspace's changes", async () => {
    const root = await repository();
    await writeFile(join(root, "src", "a.ts"), "one\n2\nthree\nfour\n");
    await writeFile(join(root, "notes.md"), "draft\n");
    const confirmed: string[] = [];
    let allow = false;
    const service = new WorkspaceChangesService({ workspace: () => root, confirmRevert: async (file) => { confirmed.push(file.path); return allow; } });

    const listed = await service.list();
    expect(listed.state).toBe("ok");
    expect(listed.files).toEqual([
      { path: "src/a.ts", status: "modified", accepted: false, additions: 2, deletions: 1 },
      { path: "notes.md", status: "untracked", accepted: false, additions: null, deletions: null },
    ]);

    const diff = await service.diff("src/a.ts");
    expect(diff.diff).toContain("-two\n+2\n");
    expect(diff.binary).toBe(false);
    expect((await service.diff("notes.md")).diff).toBe("--- /dev/null\n+++ b/notes.md\n@@ -0,0 +1,1 @@\n+draft\n");

    const accepted = await service.accept("src/a.ts");
    expect(accepted.files.find((file) => file.path === "src/a.ts")?.accepted).toBe(true);
    expect((await service.unaccept("src/a.ts")).files.find((file) => file.path === "src/a.ts")?.accepted).toBe(false);

    // Declined in the dialog: nothing is lost.
    await service.revert("src/a.ts");
    expect(await readFile(join(root, "src", "a.ts"), "utf8")).toContain("four");
    allow = true;
    const afterRevert = await service.revert("src/a.ts");
    expect(await readFile(join(root, "src", "a.ts"), "utf8")).toBe("one\ntwo\nthree\n");
    expect(afterRevert.files.map((file) => file.path)).toEqual(["notes.md"]);
    await service.revert("notes.md");
    expect(existsSync(join(root, "notes.md"))).toBe(false);
    expect(confirmed).toEqual(["src/a.ts", "src/a.ts", "notes.md"]);
    expect((await service.list()).files).toEqual([]);
  });

  it("reverts added, deleted and renamed files back to the last commit", async () => {
    const root = await repository();
    const git = (...args: string[]): void => { execFileSync("git", args, { cwd: root, stdio: "ignore" }); };
    await writeFile(join(root, "src", "new.ts"), "fresh\n");
    git("add", "src/new.ts");
    await rm(join(root, "src", "b.ts"));
    git("mv", "src/a.ts", "src/renamed.ts");
    const service = new WorkspaceChangesService({ workspace: () => root, confirmRevert: async () => true });

    const listed = await service.list();
    expect(listed.files.map((file) => [file.path, file.status]).sort()).toEqual([["src/b.ts", "deleted"], ["src/new.ts", "added"], ["src/renamed.ts", "renamed"]]);
    await service.revert("src/new.ts");
    await service.revert("src/b.ts");
    await service.revert("src/renamed.ts");
    expect((await service.list()).files).toEqual([]);
    expect(existsSync(join(root, "src", "new.ts"))).toBe(false);
    expect(existsSync(join(root, "src", "renamed.ts"))).toBe(false);
    expect(await readFile(join(root, "src", "a.ts"), "utf8")).toBe("one\ntwo\nthree\n");
    expect(await readFile(join(root, "src", "b.ts"), "utf8")).toBe("keep\n");
  });

  it("only acts on files git reports as changed, and scopes to the workspace folder", async () => {
    const root = await repository();
    await writeFile(join(root, "outside.txt"), "top\n");
    await writeFile(join(root, "src", "b.ts"), "changed\n");
    const service = new WorkspaceChangesService({ workspace: () => join(root, "src"), confirmRevert: async () => true });

    expect((await service.list()).files.map((file) => file.path)).toEqual(["src/b.ts"]);
    await expect(service.diff("outside.txt")).rejects.toThrow(/no longer has changes/);
    await expect(service.revert("../../etc/passwd")).rejects.toThrow(/no longer has changes/);
    await expect(service.accept("src/a.ts")).rejects.toThrow(/no longer has changes/);
    expect(existsSync(join(root, "outside.txt"))).toBe(true);
  });

  it("reports a folder that is not a repository, a missing git, and no workspace", async () => {
    const plain = await mkdtemp(join(tmpdir(), "studio-plain-"));
    roots.push(plain);
    const confirmRevert = async (): Promise<boolean> => true;
    expect((await new WorkspaceChangesService({ workspace: () => plain, confirmRevert }).list()).state).toBe("not-a-repo");
    expect((await new WorkspaceChangesService({ workspace: () => null, confirmRevert }).list()).state).toBe("no-workspace");
    const missing: GitRunner = async () => ({ code: 127, stdout: "", stderr: "", missing: true });
    expect((await new WorkspaceChangesService({ workspace: () => plain, confirmRevert, git: missing }).list()).state).toBe("no-git");
  });
});
