// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MissionSnapshot } from "../src/core/mission.js";
import { buildInbox, type InboxItem } from "../src/renderer/src/inbox.js";
import { AttentionInbox } from "../src/renderer/src/layout/AttentionInbox.js";
import type { TimelineItem } from "../src/renderer/src/timeline.js";

afterEach(cleanup);

const base = { id: "e1", runtimeId: "alpha", timestamp: "t" };
const approval = (approvalId: string, runId: string, decision?: string): TimelineItem =>
  ({ ...base, runId, kind: "approval", approvalId, title: `Run ${approvalId}`, detail: "npm test\nsecond line", options: ["Allow"], ...(decision ? { decision } : {}) });
const mission = (phase: MissionSnapshot["phase"], blockReason: string | null = null): MissionSnapshot =>
  ({ id: "m1", goal: "Ship the inbox", phase, blockReason }) as MissionSnapshot;

describe("buildInbox", () => {
  it("lists live approvals, unanswered boards and a waiting mission, in that order", () => {
    const items = buildInbox(
      [approval("a1", "run-live"), approval("a2", "run-live", "Allow"), approval("a3", "run-old")],
      "run-live",
      mission("awaiting-approval"),
      [{ url: "http://127.0.0.1:1/b", questions: 2, context: "Release scope" }],
    );
    expect(items.map((item) => item.id)).toEqual(["approval:a1", "decision:http://127.0.0.1:1/b", "mission:m1:awaiting-approval"]);
    expect(items[0]).toMatchObject({ title: "Run a1", detail: "npm test" });
    expect(items[1]).toMatchObject({ detail: "2 questions · Release scope" });
    expect(items[2]).toMatchObject({ title: "Mission waiting for sign-off", detail: "Ship the inbox" });
  });

  it("is empty when nothing is stopped, and explains a paused mission", () => {
    expect(buildInbox([approval("a1", "run-old")], null, mission("running"), [])).toEqual([]);
    expect(buildInbox([], null, mission("paused", "Token budget reached"), [])).toMatchObject([{ title: "Mission paused", detail: "Token budget reached" }]);
  });
});

describe("AttentionInbox", () => {
  const items: InboxItem[] = [
    { id: "approval:a1", kind: "approval", title: "Run npm test", detail: "npm test", approvalId: "a1" },
    { id: "mission:m1:paused", kind: "mission", title: "Mission paused", detail: "Token budget reached" },
  ];

  it("shows the count and opens the chosen item", () => {
    const onOpen = vi.fn();
    render(<AttentionInbox items={items} onOpen={onOpen} />);
    const button = screen.getByRole("button", { name: "Inbox: 2 waiting on you" });
    expect(button).toHaveTextContent("2");
    fireEvent.click(button);
    fireEvent.click(screen.getByRole("menuitem", { name: /Run npm test/ }));
    expect(onOpen).toHaveBeenCalledWith(items[0]);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("says so when nothing is waiting, and Escape closes it without reaching the app", () => {
    const appEscape = vi.fn();
    window.addEventListener("keydown", appEscape);
    render(<AttentionInbox items={[]} onOpen={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Inbox: nothing is waiting on you" }));
    expect(screen.getByText(/Nothing needs you right now/)).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(appEscape).not.toHaveBeenCalled();
    window.removeEventListener("keydown", appEscape);
  });
});
