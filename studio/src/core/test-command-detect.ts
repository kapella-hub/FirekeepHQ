export interface WorkspaceEntry {
  readonly name: string;
  readonly isDirectory: boolean;
}

// Spec §5.3, in this order: the first rule that matches proposes the command.
const RULES: ReadonlyArray<{ readonly matches: (entry: WorkspaceEntry) => boolean; readonly command: string }> = [
  { matches: (entry) => !entry.isDirectory && entry.name === "package.json", command: "npm test" },
  { matches: (entry) => (!entry.isDirectory && (entry.name === "pyproject.toml" || entry.name === "pytest.ini")) || (entry.isDirectory && entry.name === "tests"), command: "pytest" },
  { matches: (entry) => !entry.isDirectory && entry.name === "Cargo.toml", command: "cargo test" },
  { matches: (entry) => !entry.isDirectory && entry.name === "go.mod", command: "go test ./..." },
];

/** Propose one verification command from a workspace's top-level entries, or null. */
export function detectTestCommand(entries: readonly WorkspaceEntry[]): string | null {
  for (const rule of RULES) if (entries.some(rule.matches)) return rule.command;
  return null;
}
