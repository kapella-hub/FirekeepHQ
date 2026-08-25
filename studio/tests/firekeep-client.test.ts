import { describe, expect, it, vi } from "vitest";
import { FirekeepClient } from "../src/main/firekeep-client.js";
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
