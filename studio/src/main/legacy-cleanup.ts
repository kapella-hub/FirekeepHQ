import { rm } from "node:fs/promises";
import { join } from "node:path";

/**
 * Studio 0.4 kept encrypted API keys in `<userData>/secrets.json`. Studio 0.5 stores no secrets, so the
 * file is removed at startup. Best effort: a missing file or any other error is ignored and nothing is
 * logged (the path and contents are nobody's business).
 */
export async function removeLegacySecrets(userDataDir: string): Promise<void> {
  try {
    await rm(join(userDataDir, "secrets.json"), { force: true });
  } catch {
    /* Best effort only; a locked or unreadable file is left for the next start. */
  }
}
