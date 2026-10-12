import { describe, expect, it } from "vitest";
import { detectTestCommand, type WorkspaceEntry } from "../src/core/test-command-detect.js";

const f = (name: string): WorkspaceEntry => ({ name, isDirectory: false });
const d = (name: string): WorkspaceEntry => ({ name, isDirectory: true });

describe("detectTestCommand", () => {
  it.each([
    [[f("package.json")], "npm test"],
    [[f("pyproject.toml")], "pytest"],
    [[f("pytest.ini")], "pytest"],
    [[d("tests")], "pytest"],
    [[f("Cargo.toml")], "cargo test"],
    [[f("go.mod")], "go test ./..."],
  ])("%j → %s", (entries, expected) => {
    expect(detectTestCommand(entries)).toBe(expected);
  });

  it("prefers package.json over python markers (spec order)", () => {
    expect(detectTestCommand([f("pyproject.toml"), f("package.json")])).toBe("npm test");
  });

  it("does not treat a FILE named tests as a test directory", () => {
    expect(detectTestCommand([f("tests")])).toBeNull();
  });

  it("returns null for an empty or unknown workspace", () => {
    expect(detectTestCommand([])).toBeNull();
    expect(detectTestCommand([f("README.md"), d("src")])).toBeNull();
  });
});
