import { describe, expect, it } from "vitest";
import { rankCommands } from "../src/renderer/src/palette/CommandPalette.js";

const items = ["/doctor", "/keep-search", "/mission", "/review", "/reviewer", "/session"].map((value) => ({ value, label: value, description: "" }));
const values = (query: string): string[] => rankCommands(query, items).map((item) => item.value);

describe("rankCommands", () => {
  it("ignores a leading slash and ranks prefix over word-start over subsequence", () => {
    expect(values("rev")).toEqual(["/review", "/reviewer"]);
    expect(values("/rev")).toEqual(["/review", "/reviewer"]);
    expect(values("se")).toEqual(["/session", "/keep-search"]);
    expect(values("rvw")).toEqual(["/review", "/reviewer"]);
  });
  it("excludes non-matching items", () => {
    expect(values("ms")).toEqual(["/mission"]);
  });
  it("returns everything alphabetically for an empty query", () => {
    expect(values("")).toEqual(["/doctor", "/keep-search", "/mission", "/review", "/reviewer", "/session"]);
  });
});
