import { readdir } from "node:fs/promises";
import { detectTestCommand } from "../core/test-command-detect.js";

/** The test command a "Done when..." check should default to, or null when unknown or unreadable. */
export async function detectWorkspaceTestCommand(workspace: string | null): Promise<string | null> {
  if (!workspace) return null;
  try {
    const entries = await readdir(workspace, { withFileTypes: true });
    return detectTestCommand(entries.map((entry) => ({ name: entry.name, isDirectory: entry.isDirectory() })));
  } catch {
    return null;
  }
}
