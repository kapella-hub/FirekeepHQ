import { describe, expect, it, vi } from "vitest";
import { FirekeepClient, isMissingExecutable } from "../src/main/firekeep-client.js";
import type { ProcessResult } from "../src/main/runtime/process.js";

const result = (stdout: string, exitCode = 0, stderr = ""): ProcessResult => ({
  exitCode,
  signal: null,
  stdout,
  stderr,
  timedOut: false,
  truncated: false,
  durationMs: 12,
});

describe("FirekeepClient", () => {
  it("maps allowlisted Client Kit operations to bounded argv", async () => {
    const runner = vi.fn(async (args: readonly string[]) => result(`ran ${args.join(" ")}`));
    const client = new FirekeepClient({ runner });

    await expect(client.execute("doctor", [])).resolves.toMatchObject({ ok: true, output: "ran doctor" });
    await expect(client.execute("personal", ["on"])).resolves.toMatchObject({ ok: true, output: "ran personal on" });
    await expect(client.execute("connect", ["user@example", "--agent-id", "studio"])).resolves.toMatchObject({ ok: true });
    expect(runner).toHaveBeenLastCalledWith(["connect", "user@example", "--agent-id", "studio"], expect.anything());
  });

  it("rejects unknown operations and invalid personal/connect arguments", async () => {
    const client = new FirekeepClient({ runner: async () => result("") });
    await expect(client.execute("destroy", [])).rejects.toThrow(/unsupported/i);
    await expect(client.execute("personal", ["maybe"])).rejects.toThrow(/personal action/i);
    await expect(client.execute("connect", ["host; shutdown"])).rejects.toThrow(/invalid SSH target/i);
  });

  it("reports non-zero exits without discarding provider diagnostics", async () => {
    const client = new FirekeepClient({ runner: async () => result("partial", 2, "failed check") });
    await expect(client.execute("doctor", [])).resolves.toMatchObject({ ok: false, exitCode: 2, output: "partial\nfailed check" });
  });
});

describe("FirekeepClient whoami", () => {
  it("runs whoami with fixed --json and rejects extra args", async () => {
    const runner = vi.fn(async () => result('{"agent_id":"a"}'));
    const client = new FirekeepClient({ runner });
    await client.execute("whoami", []);
    expect(runner).toHaveBeenLastCalledWith(["whoami", "--json"], expect.anything());
    await expect(client.execute("whoami", ["x"])).rejects.toThrow(/no arguments/);
  });

  it("gives whoami a 15 s budget (Keep status waits on it), other quick actions keep 60 s", async () => {
    const runner = vi.fn(async () => result("{}"));
    const client = new FirekeepClient({ runner });
    await client.execute("whoami", []);
    expect(runner).toHaveBeenLastCalledWith(["whoami", "--json"], { timeoutMs: 15_000 });
    await client.execute("status", []);
    expect(runner).toHaveBeenLastCalledWith(["status"], { timeoutMs: 60_000 });
  });
});

describe("FirekeepClient command resolution", () => {
  it("locates the kit again on every call, so a kit installed while Studio runs is found on Retry", async () => {
    const resolveCommand = vi.fn(() => "firekeep-studio-test-missing-7f3a91");
    const client = new FirekeepClient({ resolveCommand });
    const first = await client.execute("whoami", []).catch((error: unknown) => error);
    expect(isMissingExecutable(first)).toBe(true);
    await client.execute("whoami", []).catch(() => undefined);
    expect(resolveCommand).toHaveBeenCalledTimes(2);
  });
});

describe("isMissingExecutable", () => {
  it("recognises a spawn ENOENT error code and missing-command messages", () => {
    expect(isMissingExecutable(Object.assign(new Error("spawn firekeep.exe"), { code: "ENOENT" }))).toBe(true);
    expect(isMissingExecutable(new Error("spawn firekeep ENOENT"))).toBe(true);
    expect(isMissingExecutable(new Error("'firekeep' is not recognized as an internal or external command"))).toBe(true);
    expect(isMissingExecutable("spawn firekeep ENOENT")).toBe(true);
  });

  it("does not treat other failures as a missing kit", () => {
    expect(isMissingExecutable(Object.assign(new Error("spawn EACCES"), { code: "EACCES" }))).toBe(false);
    expect(isMissingExecutable(new Error("timed out"))).toBe(false);
    expect(isMissingExecutable(null)).toBe(false);
  });
});
