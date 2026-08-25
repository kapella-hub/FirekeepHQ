import { spawnSync } from "node:child_process";

const selected = process.argv.slice(2).join(",") || "codex,claude,kiro";
const result = spawnSync(process.execPath, ["node_modules/vitest/vitest.mjs", "run", "tests/live-tasks.test.ts"], {
  cwd: process.cwd(),
  env: { ...process.env, STUDIO_LIVE_TASKS: "1", STUDIO_LIVE_TASK_RUNTIME: selected },
  stdio: "inherit",
  windowsHide: true,
});
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
