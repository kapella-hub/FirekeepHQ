import { describe, expect, it } from "vitest";
import { dashboardUrlFromConfig } from "../src/main/firekeep-config.js";

describe("configured Firekeep dashboard", () => {
  it("derives the dashboard from both supported server layouts", () => {
    expect(dashboardUrlFromConfig("[server]\nkind = ports\nscheme = http\nhost = 100.91.3.51\napi_key = secret\n")).toBe("http://100.91.3.51:8040/");
    expect(dashboardUrlFromConfig("[server]\nkind = paths\nbase_url = https://keep.example/team\n")).toBe("https://keep.example/team");
  });

  it("returns no link for an absent, malformed, or unsafe configuration", () => {
    expect(dashboardUrlFromConfig("[identity]\nagent_id = test\n")).toBeNull();
    expect(dashboardUrlFromConfig("[server]\nkind = ports\nscheme = file\nhost = keep.example\n")).toBeNull();
    expect(dashboardUrlFromConfig("[server]\nkind = paths\nbase_url = javascript:alert(1)\n")).toBeNull();
    expect(dashboardUrlFromConfig("[server]\nkind = paths\nbase_url = https://user:secret@keep.example\n")).toBeNull();
  });
});
