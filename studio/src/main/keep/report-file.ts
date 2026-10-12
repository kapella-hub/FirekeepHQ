import { readFile, rm } from "node:fs/promises";

/** Keep session ids the kit's bridge tap appended (kit 1.8, spec §7.2). Never throws. */
export async function readReportFile(path: string): Promise<string[]> {
  try {
    return (await readFile(path, "utf8")).split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  } catch {
    return [];
  }
}

/** Never throws: a report file that is already gone is the desired end state. */
export async function removeReportFile(path: string): Promise<void> {
  await rm(path, { force: true }).catch(() => undefined);
}
