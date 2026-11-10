import { parseRuntimeSettings, type MissionRuntimeSettings } from "./mission.js";

export const SCHEDULE_REPEATS = ["once", "daily", "weekdays", "weekly"] as const;
export type ScheduleRepeat = typeof SCHEDULE_REPEATS[number];
export const MAX_SCHEDULES = 20;
export const MAX_SCHEDULE_CHECKS = 8;

/** What happened the last time a schedule came due. */
export interface ScheduleRun {
  readonly at: string;
  /** The session the mission ran in, or null when it never started. */
  readonly sessionId: string | null;
  /** Why it did not start; null when it did. */
  readonly error: string | null;
}

/**
 * A mission the human approved in advance. Everything the approval covered is frozen here:
 * the goal, the commands, the folder, the agents and their permission modes. A scheduled run
 * uses these values, never whatever the settings happen to be when it comes due.
 */
export interface MissionSchedule {
  readonly id: string;
  readonly goal: string;
  readonly checks: readonly string[];
  readonly repeat: ScheduleRepeat;
  /** ISO time of the next run, or null when a one-time schedule has had its turn. */
  readonly nextRunAt: string | null;
  readonly workspacePath: string;
  readonly primaryRuntimeId: string;
  readonly reviewerRuntimeIds: readonly string[];
  readonly runtimeSettings: Readonly<Record<string, MissionRuntimeSettings>>;
  /** Run each occurrence in its own git worktree instead of the workspace folder itself. */
  readonly worktree: boolean;
  readonly createdAt: string;
  readonly approvedAt: string;
  readonly lastRun: ScheduleRun | null;
}

const MAX_STEPS = 4_000;

/**
 * The occurrence after `from` that is later than `after`, keeping the local wall-clock time
 * across daylight-saving changes. A one-time schedule has none.
 */
export function nextOccurrence(from: string, repeat: ScheduleRepeat, after: number): string | null {
  if (repeat === "once") return null;
  const date = new Date(from);
  if (Number.isNaN(date.getTime())) return null;
  for (let step = 0; step < MAX_STEPS; step += 1) {
    date.setDate(date.getDate() + (repeat === "weekly" ? 7 : 1));
    if (repeat === "weekdays" && (date.getDay() === 0 || date.getDay() === 6)) continue;
    if (date.getTime() > after) return date.toISOString();
  }
  // Years overdue: restart from today at the same local time.
  const restart = new Date(after);
  const original = new Date(from);
  restart.setHours(original.getHours(), original.getMinutes(), 0, 0);
  return nextOccurrence(restart.toISOString(), repeat, after);
}

/**
 * Schedules whose time passed while Studio was closed do not fire late: a recurring one moves
 * to its next occurrence and a one-time one is retired, each noting what was missed.
 */
export function settleMissed(schedules: readonly MissionSchedule[], now: string): MissionSchedule[] {
  const moment = Date.parse(now);
  return schedules.map((schedule) => {
    if (!schedule.nextRunAt || Date.parse(schedule.nextRunAt) > moment) return schedule;
    return {
      ...schedule,
      nextRunAt: nextOccurrence(schedule.nextRunAt, schedule.repeat, moment),
      lastRun: { at: schedule.nextRunAt, sessionId: null, error: "Missed: Studio was not running at the scheduled time." },
    };
  });
}

export function cleanScheduleChecks(checks: readonly string[]): string[] {
  const clean = checks.map((check) => check.trim()).filter(Boolean);
  if (clean.length === 0) throw new Error("a scheduled mission needs at least one check command");
  if (clean.length > MAX_SCHEDULE_CHECKS) throw new Error(`a scheduled mission supports at most ${MAX_SCHEDULE_CHECKS} checks`);
  if (clean.some((check) => check.length > 1_000)) throw new Error("a check command is too long");
  return clean;
}

export function describeRepeat(repeat: ScheduleRepeat): string {
  return repeat === "once" ? "once" : repeat === "daily" ? "every day" : repeat === "weekdays" ? "every weekday" : "every week";
}

export function parseSchedules(value: unknown): MissionSchedule[] {
  if (!Array.isArray(value)) return [];
  const schedules: MissionSchedule[] = [];
  for (const raw of value) {
    if (schedules.length >= MAX_SCHEDULES) break;
    if (!raw || typeof raw !== "object") continue;
    const item = raw as Record<string, unknown>;
    const text = (key: string): string | null => (typeof item[key] === "string" && (item[key] as string).length > 0 ? item[key] as string : null);
    const id = text("id"), goal = text("goal"), workspacePath = text("workspacePath"), primaryRuntimeId = text("primaryRuntimeId"), createdAt = text("createdAt"), approvedAt = text("approvedAt");
    const repeat = SCHEDULE_REPEATS.find((entry) => entry === item.repeat);
    const checks = Array.isArray(item.checks) ? item.checks.filter((check): check is string => typeof check === "string" && check.length > 0) : [];
    // An entry that is incomplete, or was never approved, is dropped rather than repaired.
    if (!id || !goal || !workspacePath || !primaryRuntimeId || !createdAt || !approvedAt || !repeat || checks.length === 0) continue;
    const nextRunAt = typeof item.nextRunAt === "string" && !Number.isNaN(Date.parse(item.nextRunAt)) ? item.nextRunAt : null;
    schedules.push({
      id, goal, checks: checks.slice(0, MAX_SCHEDULE_CHECKS), repeat, nextRunAt, workspacePath, primaryRuntimeId,
      reviewerRuntimeIds: Array.isArray(item.reviewerRuntimeIds) ? item.reviewerRuntimeIds.filter((entry): entry is string => typeof entry === "string") : [],
      runtimeSettings: parseRuntimeSettings(item.runtimeSettings),
      worktree: item.worktree === true,
      createdAt, approvedAt,
      lastRun: parseRun(item.lastRun),
    });
  }
  return schedules;
}

function parseRun(value: unknown): ScheduleRun | null {
  if (!value || typeof value !== "object") return null;
  const run = value as Record<string, unknown>;
  if (typeof run.at !== "string") return null;
  return { at: run.at, sessionId: typeof run.sessionId === "string" ? run.sessionId : null, error: typeof run.error === "string" ? run.error : null };
}
