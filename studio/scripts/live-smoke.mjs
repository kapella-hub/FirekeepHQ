import { spawnSync } from "node:child_process";

const result = spawnSync(process.execPath, ["node_modules/vitest/vitest.mjs", "run", "tests/live-runtimes.test.ts"], {
  cwd: process.cwd(),
  env: { ...process.env, STUDIO_LIVE_RUNTIMES: "1" },
  stdio: "inherit",
  windowsHide: true,
});
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
