import { z } from "zod";
import type { CommandRegistry } from "../core/slash-commands.js";
import type { StudioService } from "../core/studio-service.js";
import type { StudioAction, StudioActionResult } from "../shared/ipc.js";
import type { DecisionBoardTransport } from "./decision-board-client.js";
import { LoopbackDecisionBoardClient } from "./decision-board-client.js";
import type { VoiceInput } from "./voice-input.js";
import { SESSION_COLORS } from "../core/session-store.js";

const id = z.string().min(1).max(128).regex(/^[a-zA-Z0-9._:-]+$/);
const optionalId = id.optional();
const decisionAnswer = z.object({
  answer: z.string().max(100_000),
  actions_confirmed: z.array(z.string().max(20_000)).max(32),
  skipped: z.boolean(),
}).strict();
const decisionAnswers = z.record(z.string().min(1).max(128), decisionAnswer)
  .refine((value) => Object.keys(value).length <= 64, "too many Decision Board answers");
const MAX_CLIPBOARD_TEXT = 1_000_000;
const language = z.string().min(2).max(35).regex(/^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/);
const actionSchema = z.discriminatedUnion("type", [
  z.object({ type: z.literal("bootstrap") }).strict(),
  z.object({ type: z.literal("dashboard.open") }).strict(),
  z.object({ type: z.literal("clipboard.read") }).strict(),
  z.object({ type: z.literal("clipboard.write"), text: z.string().max(MAX_CLIPBOARD_TEXT) }).strict(),
  z.object({ type: z.literal("decision.load"), url: z.string().min(1).max(512) }).strict(),
  z.object({ type: z.literal("decision.submit"), url: z.string().min(1).max(512), answers: decisionAnswers }).strict(),
  z.object({ type: z.literal("message.send"), text: z.string().min(1).max(1_000_000) }).strict(),
  z.object({ type: z.literal("message.sendTo"), runtimeId: id, text: z.string().min(1).max(1_000_000) }).strict(),
  z.object({ type: z.literal("command.execute"), input: z.string().min(1).max(20_000) }).strict(),
  z.object({ type: z.literal("command.complete"), input: z.string().max(20_000) }).strict(),
  z.object({ type: z.literal("runtime.probe"), runtimeId: optionalId }).strict(),
  z.object({ type: z.literal("runtime.models"), runtimeId: id }).strict(),
  z.object({ type: z.literal("runtime.login"), runtimeId: id, method: z.enum(["browser", "device", "api-key", "console", "sso"]).optional(), secret: z.string().min(1).max(32_768).optional() }).strict(),
  z.object({ type: z.literal("runtime.logout"), runtimeId: id }).strict(),
  z.object({ type: z.literal("primary.set"), runtimeId: id }).strict(),
  z.object({ type: z.literal("reviewer.add"), runtimeId: id }).strict(),
  z.object({ type: z.literal("reviewer.remove"), runtimeId: id }).strict(),
  z.object({ type: z.literal("reviewer.mode"), mode: z.enum(["off", "manual", "after-turn"]) }).strict(),
  z.object({ type: z.literal("review.run"), runtimeId: optionalId, focus: z.string().max(2_000).optional() }).strict(),
  z.object({ type: z.literal("model.set"), runtimeId: id, modelId: z.string().max(256) }).strict(),
  z.object({ type: z.literal("effort.set"), runtimeId: id, effort: z.enum(["low", "medium", "high", "xhigh", "max"]).nullable() }).strict(),
  z.object({ type: z.literal("permission.set"), runtimeId: id, mode: z.enum(["safe", "standard", "unrestricted"]) }).strict(),
  z.object({ type: z.literal("approval.resolve"), approvalId: id, decision: z.string().min(1).max(128) }).strict(),
  z.object({ type: z.literal("workspace.choose") }).strict(),
  z.object({ type: z.literal("session.new") }).strict(),
  z.object({ type: z.literal("session.resume"), sessionId: id }).strict(),
  z.object({ type: z.literal("session.rename"), name: z.string().min(1).max(120) }).strict(),
  z.object({ type: z.literal("session.update"), sessionId: id, name: z.string().min(1).max(120), color: z.enum(SESSION_COLORS) }).strict(),
  z.object({ type: z.literal("mission.run") }).strict(),
  z.object({ type: z.literal("mission.continue") }).strict(),
  z.object({ type: z.literal("mission.repair"), note: z.string().min(1).max(4_000) }).strict(),
  z.object({ type: z.literal("mission.complete"), taskResult: z.enum(["success", "partial", "failure"]), note: z.string().max(4_000).optional() }).strict(),
  z.object({ type: z.literal("mission.cancel") }).strict(),
  z.object({ type: z.literal("theme.set"), theme: z.enum(["system", "dark", "light"]) }).strict(),
  z.object({ type: z.literal("voice.set"), enabled: z.boolean() }).strict(),
  z.object({ type: z.literal("voice.input.start"), language: language.optional() }).strict(),
  z.object({ type: z.literal("voice.input.stop") }).strict(),
  z.object({ type: z.literal("run.cancel"), runId: optionalId }).strict(),
]);

export function parseStudioAction(value: unknown): StudioAction {
  return actionSchema.parse(value) as StudioAction;
}

export interface ClipboardTransport {
  readText(): string;
  writeText(text: string): void;
}

const emptyClipboard: ClipboardTransport = { readText: () => "", writeText: () => undefined };
const unavailableVoiceInput: VoiceInput = {
  transcribe: async () => ({ state: "unavailable", text: "", detail: "Voice input is unavailable in this Studio build." }),
  cancel: () => false,
};

export class StudioController {
  constructor(
    readonly service: StudioService,
    readonly commands: CommandRegistry,
    readonly appVersion: string,
    readonly openExternal: (url: string) => Promise<void> = async () => undefined,
    readonly selectWorkspace: () => Promise<string | null> = async () => null,
    readonly dashboardUrl: string | null = null,
    readonly decisionBoards: DecisionBoardTransport = new LoopbackDecisionBoardClient(),
    readonly clipboard: ClipboardTransport = emptyClipboard,
    readonly voiceInput: VoiceInput = unavailableVoiceInput,
  ) {}

  async dispatch(rawAction: unknown): Promise<StudioActionResult> {
    const action = parseStudioAction(rawAction);
    if (action.type === "bootstrap") {
      return {
        type: "bootstrap",
        appName: "Firekeep Studio",
        version: this.appVersion,
        dashboardAvailable: this.dashboardUrl !== null,
        snapshot: this.service.snapshot(),
        runtimes: this.service.runtimes.list().map((runtime) => runtime.descriptor),
        events: this.service.events(),
        sessions: await this.service.listSessions(),
      };
    }
    if (action.type === "dashboard.open") {
      if (!this.dashboardUrl) throw new Error("Firekeep dashboard is not configured");
      await this.#openHttpUrl(this.dashboardUrl, "configured dashboard");
      return { type: "state", snapshot: this.service.snapshot() };
    }
    if (action.type === "clipboard.read") return { type: "clipboard-read", text: this.clipboard.readText().slice(0, MAX_CLIPBOARD_TEXT) };
    if (action.type === "clipboard.write") {
      this.clipboard.writeText(action.text);
      return { type: "clipboard-written" };
    }
    if (action.type === "voice.input.start") return { type: "voice-input", ...await this.voiceInput.transcribe(action.language) };
    if (action.type === "voice.input.stop") {
      const stopped = this.voiceInput.cancel();
      return { type: "voice-input", state: "cancelled", text: "", detail: stopped ? "Voice input stopped." : "No voice input was active." };
    }
    if (action.type === "decision.load") return { type: "decision", board: await this.decisionBoards.load(action.url) };
    if (action.type === "decision.submit") {
      await this.decisionBoards.submit(action.url, action.answers);
      return { type: "decision-submitted", url: action.url };
    }
    if (action.type === "command.complete") return { type: "completions", items: this.commands.complete(action.input) };
    if (action.type === "command.execute") return { type: "command", result: await this.commands.execute(action.input), snapshot: this.service.snapshot(), sessions: await this.service.listSessions(), events: this.service.events() };
    if (action.type === "message.send") await this.service.sendMessage(action.text);
    else if (action.type === "message.sendTo") await this.service.sendMessageTo(action.runtimeId, action.text);
    else if (action.type === "runtime.probe") return { type: "diagnostics", items: action.runtimeId ? [await this.service.probeRuntime(action.runtimeId)] : await this.service.probeAll() };
    else if (action.type === "runtime.models") return { type: "models", runtimeId: action.runtimeId, items: await this.service.listModels(action.runtimeId) };
    else if (action.type === "runtime.login") {
      const result = await this.service.login(action.runtimeId, {
        ...(action.method ? { method: action.method } : {}),
        ...(action.secret ? { secret: action.secret } : {}),
      });
      if (result.state === "browser" || result.state === "device") await this.#openProviderUrl(result.url);
      return { type: "login", runtimeId: action.runtimeId, result };
    } else if (action.type === "runtime.logout") await this.service.logout(action.runtimeId);
    else if (action.type === "primary.set") await this.service.setPrimary(action.runtimeId);
    else if (action.type === "reviewer.add") await this.service.addReviewer(action.runtimeId);
    else if (action.type === "reviewer.remove") await this.service.removeReviewer(action.runtimeId);
    else if (action.type === "reviewer.mode") await this.service.setReviewerMode(action.mode);
    else if (action.type === "review.run") await this.service.runReview(action.runtimeId, action.focus);
    else if (action.type === "model.set") await this.service.setModel(action.runtimeId, action.modelId);
    else if (action.type === "effort.set") await this.service.setEffort(action.runtimeId, action.effort);
    else if (action.type === "permission.set") await this.service.setPermissionMode(action.runtimeId, action.mode);
    else if (action.type === "approval.resolve") return { type: "approval", accepted: this.service.resolveApproval(action.approvalId, action.decision) };
    else if (action.type === "workspace.choose") {
      const selected = await this.selectWorkspace();
      if (selected) await this.service.setWorkspace(selected);
    }
    else if (action.type === "session.new") await this.service.startNewSession();
    else if (action.type === "session.resume") await this.service.resumeSession(action.sessionId);
    else if (action.type === "session.rename") await this.service.renameSession(action.name);
    else if (action.type === "session.update") await this.service.updateSession(action.sessionId, { name: action.name, color: action.color });
    else if (action.type === "mission.run") await this.service.runMission();
    else if (action.type === "mission.continue") await this.service.continueMission();
    else if (action.type === "mission.repair") await this.service.repairMission(action.note);
    else if (action.type === "mission.complete") await this.service.completeMission(action.taskResult, action.note);
    else if (action.type === "mission.cancel") this.service.cancelMission();
    else if (action.type === "theme.set") await this.service.setTheme(action.theme);
    else if (action.type === "voice.set") await this.service.setVoice(action.enabled);
    else if (action.type === "run.cancel") this.service.cancel(action.runId);
    const sessions = action.type.startsWith("session.") || action.type.startsWith("mission.") ? await this.service.listSessions() : undefined;
    return { type: "state", snapshot: this.service.snapshot(), ...(sessions ? { sessions, events: this.service.events() } : {}) };
  }

  async #openProviderUrl(value: string): Promise<void> {
    await this.#openHttpUrl(value, "provider login");
  }

  async #openHttpUrl(value: string, label: string): Promise<void> {
    const url = new URL(value);
    if (url.protocol !== "https:" && url.protocol !== "http:") throw new Error(`${label} has an unsupported URL`);
    await this.openExternal(url.toString());
  }
}
