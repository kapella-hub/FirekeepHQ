import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { readReportFile, removeReportFile } from "../src/main/keep/report-file.js";

describe("Keep report file", () => {
  const dirs: string[] = [];
  afterEach(async () => {
    await Promise.all(dirs.splice(0).map((dir) => rm(dir, { recursive: true, force: true })));
  });

  async function tempDir(): Promise<string> {
    const dir = await mkdtemp(join(tmpdir(), "studio-report-"));
    dirs.push(dir);
    return dir;
  }

  it("reads one Keep session id per line, ignoring CRLF and blank lines", async () => {
    const path = join(await tempDir(), "s.keep");
    await writeFile(path, "s1\r\ns2\n\n", "utf8");
    await expect(readReportFile(path)).resolves.toEqual(["s1", "s2"]);
  });

  it("returns no ids for a missing file", async () => {
    await expect(readReportFile(join(await tempDir(), "missing.keep"))).resolves.toEqual([]);
  });

  it("removing a missing file resolves", async () => {
    await expect(removeReportFile(join(await tempDir(), "missing.keep"))).resolves.toBeUndefined();
  });
});
