// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MissionSchedule } from "../src/core/schedule.js";
import { ScheduleDialog, localInputValue } from "../src/renderer/src/schedule/ScheduleDialog.js";
import type { StudioAction, StudioActionResult } from "../src/shared/ipc.js";

afterEach(cleanup);

const stored: MissionSchedule = {
  id: "schedule-1", goal: "Keep the build green", checks: ["npm test"], repeat: "daily", nextRunAt: "2030-01-02T09:00:00.000Z",
  workspacePath: "C:\\repo", primaryRuntimeId: "alpha", reviewerRuntimeIds: [], runtimeSettings: {}, worktree: true,
  createdAt: "2026-11-10T00:00:00.000Z", approvedAt: "2026-11-10T00:00:00.000Z", lastRun: { at: "2026-11-10T09:00:00.000Z", sessionId: "session-9", error: null },
};

function mount(overrides: Partial<React.ComponentProps<typeof ScheduleDialog>> = {}) {
  const invoke = vi.fn(async (_action: StudioAction) => ({ type: "snapshot" }) as unknown as StudioActionResult);
  const onOpenSession = vi.fn();
  const onClose = vi.fn();
  render(<ScheduleDialog schedules={[stored]} workspacePath={"C:\\repo"} agentName="Alpha" canWorktree invoke={invoke} onOpenSession={onOpenSession} onClose={onClose} {...overrides} />);
  return { invoke, onOpenSession, onClose };
}

describe("ScheduleDialog", () => {
  it("lists what is scheduled, opens the session of the last run and deletes on request", async () => {
    const { invoke, onOpenSession } = mount();
    const list = screen.getByRole("list", { name: "Scheduled missions" });
    expect(list).toHaveTextContent("Keep the build green");
    expect(list).toHaveTextContent("every day");
    expect(list).toHaveTextContent("own worktree");
    fireEvent.click(screen.getByRole("button", { name: "Open that session" }));
    expect(onOpenSession).toHaveBeenCalledWith("session-9");
    fireEvent.click(screen.getByRole("button", { name: "Delete schedule: Keep the build green" }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "schedule.delete", scheduleId: "schedule-1" }));
  });

  it("sends the goal, one check per line, the local time as ISO and the repeat", async () => {
    const { invoke } = mount({ schedules: [] });
    expect(screen.getByText("Nothing is scheduled.")).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "Review and schedule…" });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Goal" }), { target: { value: " Update dependencies " } });
    fireEvent.change(screen.getByRole("textbox", { name: /Done when these commands pass/ }), { target: { value: "npm test\n\n npm run typecheck " } });
    const local = new Date(2031, 4, 6, 7, 30);
    fireEvent.change(screen.getByLabelText("First run"), { target: { value: localInputValue(local) } });
    fireEvent.change(screen.getByRole("combobox", { name: "Repeat" }), { target: { value: "weekdays" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /Run in its own git worktree/ }));
    fireEvent.click(submit);
    await waitFor(() => expect(invoke).toHaveBeenCalledWith({ type: "schedule.create", goal: "Update dependencies", checks: ["npm test", "npm run typecheck"], firstRunAt: local.toISOString(), repeat: "weekdays", worktree: true }));
  });

  it("shows why a schedule was refused and says what is missing before one can be made", async () => {
    const invoke = vi.fn(async () => { throw new Error("the scheduled time must be in the future"); });
    mount({ schedules: [], invoke });
    fireEvent.change(screen.getByRole("textbox", { name: "Goal" }), { target: { value: "Goal" } });
    fireEvent.change(screen.getByRole("textbox", { name: /Done when these commands pass/ }), { target: { value: "npm test" } });
    fireEvent.click(screen.getByRole("button", { name: "Review and schedule…" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("must be in the future");
    cleanup();
    mount({ schedules: [], workspacePath: null });
    expect(screen.getByText("Choose a workspace folder first.")).toBeInTheDocument();
  });
});
