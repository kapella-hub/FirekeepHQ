import type { LoginMethod, LoginRequest, LoginResult, RuntimeEffort, RuntimePermissionMode } from "./runtime.js";
import type { MissionSnapshot, MissionTaskResult } from "./mission.js";
import type { ReviewerMode, StudioService, ThemeMode } from "./studio-service.js";
import { isSessionColor, SESSION_COLORS } from "./session-store.js";

export interface ParsedSlashCommand {
  readonly name: string;
  readonly args: readonly string[];
  readonly flags: Readonly<Record<string, string | true>>;
  readonly raw: string;
}

export interface CommandResult {
  readonly kind: "markdown" | "notice" | "table";
  readonly title: string;
  readonly body: string;
  readonly tone?: "neutral" | "success" | "warning" | "danger";
  readonly rows?: readonly (readonly string[])[];
}

export interface CommandCompletion {
  readonly value: string;
  readonly label: string;
  readonly description: string;
}

interface CommandDefinition {
  readonly name: string;
  readonly aliases?: readonly string[];
  readonly summary: string;
  readonly usages: readonly string[];
  readonly execute: (command: ParsedSlashCommand) => Promise<CommandResult>;
}

export interface ClientKitControl {
  execute(action: string, args: readonly string[]): Promise<{
    readonly ok: boolean;
    readonly output: string;
    readonly exitCode: number | null;
  }>;
}

export interface CommandIntegrations {
  readonly firekeep?: ClientKitControl;
  readonly login?: (runtimeId: string, request: LoginRequest) => Promise<LoginResult>;
  readonly kiroIde?: {
    probe(): Promise<{ readonly available: boolean; readonly detail: string; readonly executable?: string }>;
    open(workspace?: string): Promise<{ readonly message: string }>;
  };
  readonly exportSession?: (format: "markdown" | "json", content: string, suggestedName: string) => Promise<{
    readonly saved: boolean;
    readonly detail: string;
  }>;
  readonly selectWorkspace?: () => Promise<string | null>;
}

export class CommandRegistry {
  readonly #definitions = new Map<string, CommandDefinition>();
  readonly #canonical: CommandDefinition[] = [];

  constructor(readonly service: StudioService) {}

  register(definition: CommandDefinition): void {
    if (this.#definitions.has(definition.name)) throw new Error(`duplicate command: ${definition.name}`);
    this.#canonical.push(definition);
    this.#definitions.set(definition.name, definition);
    for (const alias of definition.aliases ?? []) {
      if (this.#definitions.has(alias)) throw new Error(`duplicate command alias: ${alias}`);
      this.#definitions.set(alias, definition);
    }
  }

  async execute(input: string): Promise<CommandResult> {
    const parsed = parseSlashCommand(input);
    const definition = this.#definitions.get(parsed.name);
    if (!definition) throw new Error(`unknown command: ${parsed.name}. Try /help.`);
    return definition.execute(parsed);
  }

  complete(input: string): CommandCompletion[] {
    const text = input.trimStart();
    if (!text.startsWith("/")) return [];
    const trailingSpace = /\s$/.test(input);
    let tokens: string[];
    try {
      tokens = tokenize(text.slice(1));
    } catch {
      return [];
    }
    if (tokens.length <= 1 && !trailingSpace) {
      const query = tokens[0]?.toLowerCase() ?? "";
      return this.#canonical
        .filter((definition) => definition.name.startsWith(query))
        .map((definition) => ({
          value: `/${definition.name}`,
          label: `/${definition.name}`,
          description: definition.summary,
        }));
    }

    const commandName = tokens[0] ?? "";
    const definition = this.#definitions.get(commandName);
    if (!definition) return [];
    const args = tokens.slice(1);
    const prefix = `/${commandName}`;
    const runtimeValues = (values: readonly string[] = args, valuePrefix = prefix, omitSelected = false): CommandCompletion[] => {
      const leading = trailingSpace ? [...values] : values.slice(0, -1);
      const query = (trailingSpace ? "" : values.at(-1) ?? "").toLowerCase();
      const selected = new Set(omitSelected ? leading : []);
      return this.service.runtimes.list()
        .filter((runtime) => !selected.has(runtime.descriptor.id))
        .filter((runtime) => !query
          || runtime.descriptor.id.toLowerCase().startsWith(query)
          || runtime.descriptor.displayName.toLowerCase().startsWith(query))
        .map((runtime) => ({
          value: [valuePrefix, ...leading, runtime.descriptor.id].join(" "),
          label: runtime.descriptor.displayName,
          description: runtime.descriptor.description,
        }));
    };

    const firstRuntimeCommands = ["primary", "use", "connect", "disconnect", "model", "effort", "permissions", "handoff", "consensus"];
    if (firstRuntimeCommands.includes(commandName) && (args.length === 0 || (args.length === 1 && !trailingSpace))) {
      return runtimeValues();
    }
    if (commandName === "compare" && !args.includes("--prompt")) {
      return runtimeValues(args, prefix, true);
    }
    if (["runtime", "agents"].includes(commandName)
      && ["use", "status", "models"].includes(args[0] ?? "")
      && (args.length === 1 || (args.length === 2 && !trailingSpace))) {
      return runtimeValues(args.slice(1), `${prefix} ${args[0]}`);
    }
    if (["reviewer", "reviewers"].includes(commandName)
      && ["add", "remove"].includes(args[0] ?? "")
      && (args.length === 1 || (args.length === 2 && !trailingSpace))) {
      return runtimeValues(args.slice(1), `${prefix} ${args[0]}`).map((item) => ({
        ...item,
        description: args[0] === "add" ? "Add as an independent reviewer" : "Remove this reviewer",
      }));
    }
    return definition.usages.map((usage) => ({
      value: usage,
      label: usage,
      description: definition.summary,
    }));
  }

  help(name?: string): CommandResult {
    if (name) {
      const definition = this.#definitions.get(name);
      if (!definition) throw new Error(`unknown command: ${name}`);
      return {
        kind: "markdown",
        title: `/${definition.name}`,
        body: `${definition.summary}\n\n${definition.usages.map((usage) => `- \`${usage}\``).join("\n")}`,
      };
    }
    return {
      kind: "markdown",
      title: "Firekeep Studio commands",
      body: this.#canonical.map((definition) => `- **/${definition.name}** — ${definition.summary}`).join("\n"),
    };
  }
}

export function parseSlashCommand(raw: string): ParsedSlashCommand {
  const trimmed = raw.trim();
  if (!trimmed.startsWith("/")) throw new Error("slash commands must start with /");
  const tokens = tokenize(trimmed.slice(1));
  const name = tokens.shift()?.toLowerCase();
  if (!name) throw new Error("command name cannot be empty");
  const args: string[] = [];
  const flags: Record<string, string | true> = {};
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index];
    if (!token) continue;
    if (token.startsWith("--") && token.length > 2) {
      const [flagName, inlineValue] = token.slice(2).split(/=(.*)/s, 2);
      if (!flagName) throw new Error("flag name cannot be empty");
      if (inlineValue !== undefined && inlineValue !== "") {
        flags[flagName] = inlineValue;
      } else {
        const next = tokens[index + 1];
        if (next && !next.startsWith("--")) {
          flags[flagName] = next;
          index += 1;
        } else {
          flags[flagName] = true;
        }
      }
    } else {
      args.push(token);
    }
  }
  return { name, args, flags, raw };
}

function tokenize(input: string): string[] {
  const tokens: string[] = [];
  let current = "";
  let quote: "'" | '"' | null = null;
  let started = false;
  for (let index = 0; index < input.length; index += 1) {
    const character = input[index] as string;
    if (character === "\\") {
      const next = input[index + 1];
      if (next && (/\s/.test(next) || next === "\\" || next === '"' || next === "'")) {
        current += next;
        index += 1;
      } else current += character;
      started = true;
      continue;
    }
    if (quote) {
      if (character === quote) quote = null;
      else current += character;
      started = true;
      continue;
    }
    if (character === '"' || character === "'") {
      quote = character;
      started = true;
      continue;
    }
    if (/\s/.test(character)) {
      if (started) {
        tokens.push(current);
        current = "";
        started = false;
      }
      continue;
    }
    current += character;
    started = true;
  }
  if (quote) throw new Error("unterminated quote in slash command");
  if (started) tokens.push(current);
  return tokens;
}

function requireArg(command: ParsedSlashCommand, index: number, label: string): string {
  const value = command.args[index];
  if (!value) throw new Error(`missing ${label}`);
  return value;
}

function notice(title: string, body: string, tone: CommandResult["tone"] = "success"): CommandResult {
  return { kind: "notice", title, body, tone };
}

export function createCommandRegistry(service: StudioService, integrations: CommandIntegrations = {}): CommandRegistry {
  const commands = new CommandRegistry(service);
  const login = integrations.login ?? ((runtimeId: string, request: LoginRequest) => service.login(runtimeId, request));

  commands.register({
    name: "help",
    summary: "Browse commands and usage.",
    usages: ["/help", "/help <command>"],
    execute: async (command) => commands.help(command.args[0]),
  });

  const setPrimary = async (runtimeId: string): Promise<CommandResult> => {
    await service.setPrimary(runtimeId);
    return notice("Primary runtime changed", `${service.runtimes.require(runtimeId).descriptor.displayName} will run future turns.`);
  };
  commands.register({
    name: "runtime",
    aliases: ["agents"],
    summary: "List, inspect, and choose agent runtimes.",
    usages: ["/runtime list", "/runtime status [id|all]", "/runtime use <runtime-id>", "/runtime models [runtime-id]"],
    execute: async (command) => {
      const action = command.args[0] ?? "list";
      if (action === "use") return setPrimary(requireArg(command, 1, "runtime id"));
      if (action === "models") {
        const runtimeId = command.args[1] ?? service.snapshot().primaryRuntimeId;
        if (!runtimeId) throw new Error("choose a runtime first");
        const models = await service.listModels(runtimeId);
        return {
          kind: "table",
          title: `${service.runtimes.require(runtimeId).descriptor.displayName} models`,
          body: `${models.length} available`,
          rows: models.map((model) => [model.id, model.displayName, model.isDefault ? "default" : ""]),
        };
      }
      if (action === "status") {
        const target = command.args[1] ?? "all";
        const diagnostics = target === "all" ? await service.probeAll() : [await service.probeRuntime(target)];
        return {
          kind: "table",
          title: "Runtime status",
          body: `${diagnostics.length} runtime${diagnostics.length === 1 ? "" : "s"} checked`,
          rows: diagnostics.map((item) => [item.runtimeId, item.connection.state, item.auth.state, item.connection.detail]),
        };
      }
      if (action !== "list") throw new Error(`unknown runtime action: ${action}`);
      const snapshot = service.snapshot();
      return {
        kind: "table",
        title: "Agent runtimes",
        body: "Any chat-capable runtime can be primary.",
        rows: service.runtimes.list().map((runtime) => [
          runtime.descriptor.id,
          runtime.descriptor.displayName,
          runtime.descriptor.id === snapshot.primaryRuntimeId ? "primary" : "",
          runtime.descriptor.capabilities.join(", "),
        ]),
      };
    },
  });
  commands.register({ name: "primary", aliases: ["use"], summary: "Choose the primary runtime.", usages: ["/primary <runtime-id>", "/use <runtime-id>"], execute: async (command) => setPrimary(requireArg(command, 0, "runtime id")) });

  commands.register({
    name: "mission",
    summary: "Define and run an evidence-backed primary, verification, repair, and review workflow.",
    usages: [
      "/mission new <goal>",
      "/mission status",
      "/mission primary <runtime-id>",
      "/mission reviewer add|remove <runtime-id>",
      "/mission check add <command> [--name <label>] [--timeout <duration>]",
      "/mission check remove <check-id>",
      "/mission budget <tokens|off>",
      "/mission repairs <0-3>",
      "/mission run",
      "/mission continue",
      "/mission repair --note <direction>",
      "/mission approve [--note <text>]",
      "/mission result partial|failure [--note <text>]",
      "/mission cancel",
      "/mission report",
    ],
    execute: async (command) => {
      const action = command.args[0] ?? "status";
      if (action === "new") {
        const mission = await service.createMission(command.args.slice(1).join(" "));
        return missionNotice(mission, "Mission drafted");
      }
      if (action === "primary") {
        const mission = await service.setMissionPrimary(requireArg(command, 1, "runtime id"));
        return missionNotice(mission, "Mission primary changed");
      }
      if (action === "reviewer") {
        const reviewerAction = requireArg(command, 1, "reviewer action");
        const runtimeId = requireArg(command, 2, "runtime id");
        const mission = reviewerAction === "add"
          ? await service.addMissionReviewer(runtimeId)
          : reviewerAction === "remove"
            ? await service.removeMissionReviewer(runtimeId)
            : (() => { throw new Error(`unknown mission reviewer action: ${reviewerAction}`); })();
        return missionNotice(mission, "Mission reviewers changed");
      }
      if (action === "check") {
        const checkAction = requireArg(command, 1, "check action");
        if (checkAction === "add") {
          const commandText = command.args.slice(2).filter((item) => item !== "--").join(" ");
          const timeout = command.flags.timeout;
          const mission = await service.addMissionCheck(commandText, {
            ...(typeof command.flags.name === "string" ? { name: command.flags.name } : {}),
            ...(typeof timeout === "string" ? { timeoutMs: parseDuration(timeout) } : {}),
          });
          return missionNotice(mission, "Mission check added");
        }
        if (checkAction === "remove") return missionNotice(await service.removeMissionCheck(requireArg(command, 2, "check id")), "Mission check removed");
        if (checkAction !== "list") throw new Error(`unknown mission check action: ${checkAction}`);
        return missionStatus(service.mission());
      }
      if (action === "budget") {
        const value = requireArg(command, 1, "token guard");
        return missionNotice(await service.setMissionTokenBudget(value === "off" ? null : parseTokenAmount(value)), "Mission token guard changed");
      }
      if (action === "repairs") {
        const value = Number(requireArg(command, 1, "repair count"));
        return missionNotice(await service.setMissionRepairLimit(value), "Mission repair limit changed");
      }
      if (action === "run") return missionNotice(await service.runMission(), "Mission advanced");
      if (action === "continue") return missionNotice(await service.continueMission(), "Mission continued");
      if (action === "repair") {
        const note = command.flags.note;
        if (typeof note !== "string") throw new Error("mission repair requires --note <direction>");
        return missionNotice(await service.repairMission(note), "Mission repair advanced");
      }
      if (action === "approve") {
        const note = command.flags.note;
        return missionNotice(await service.completeMission("success", typeof note === "string" ? note : undefined), "Mission accepted");
      }
      if (action === "result") {
        const result = requireArg(command, 1, "task result") as MissionTaskResult;
        if (result !== "partial" && result !== "failure") throw new Error("mission result must be partial or failure; use /mission approve for success");
        const note = command.flags.note;
        return missionNotice(await service.completeMission(result, typeof note === "string" ? note : undefined), "Mission result recorded");
      }
      if (action === "cancel") return notice("Mission cancel", service.cancelMission() ? "Cancellation requested." : "No active mission.", "neutral");
      if (action === "report") return { kind: "markdown", title: "Mission report", body: service.missionReport() };
      if (action !== "status") throw new Error(`unknown mission action: ${action}`);
      return missionStatus(service.mission());
    },
  });

  commands.register({
    name: "compare",
    summary: "Ask multiple runtimes independently in fresh, read-only contexts.",
    usages: ["/compare [runtime-id...] --prompt <text>", "/compare all --prompt <text>"],
    execute: async (command) => {
      const prompt = typeof command.flags.prompt === "string" ? command.flags.prompt : undefined;
      if (!prompt) throw new Error("compare requires --prompt <text>");
      const runtimeIds = command.args[0] === "all"
        ? service.runtimes.list().filter((runtime) => runtime.descriptor.capabilities.includes("chat")).map((runtime) => runtime.descriptor.id)
        : command.args.length ? command.args : undefined;
      const results = await service.compare(prompt, runtimeIds);
      return notice("Comparison complete", `${results.length} independent runtime${results.length === 1 ? "" : "s"} answered in safe contexts.`, "success");
    },
  });

  commands.register({
    name: "consensus",
    summary: "Synthesize recent independent answers without hiding disagreement.",
    usages: ["/consensus [runtime-id] [--focus <text>]"],
    execute: async (command) => {
      const focus = typeof command.flags.focus === "string" ? command.flags.focus : undefined;
      await service.synthesize(command.args[0], focus);
      return notice("Consensus complete", "Studio preserved the source answers and added a fresh synthesis.", "success");
    },
  });

  commands.register({
    name: "reviewer",
    aliases: ["reviewers"],
    summary: "Configure independent review runtimes and automatic review mode.",
    usages: ["/reviewer list", "/reviewer add <runtime-id>", "/reviewer remove <runtime-id>", "/reviewer clear", "/reviewer mode off|manual|after-turn"],
    execute: async (command) => {
      const action = command.args[0] ?? "list";
      if (action === "add") await service.addReviewer(requireArg(command, 1, "runtime id"));
      else if (action === "remove") await service.removeReviewer(requireArg(command, 1, "runtime id"));
      else if (action === "clear") await service.clearReviewers();
      else if (action === "mode") {
        const mode = requireArg(command, 1, "reviewer mode") as ReviewerMode;
        if (!["off", "manual", "after-turn"].includes(mode)) throw new Error(`invalid reviewer mode: ${mode}`);
        await service.setReviewerMode(mode);
      } else if (action !== "list") throw new Error(`unknown reviewer action: ${action}`);
      const state = service.snapshot();
      return notice("Reviewers", state.reviewerRuntimeIds.length ? `${state.reviewerRuntimeIds.join(", ")} · ${state.reviewerMode}` : `None configured · ${state.reviewerMode}`, "neutral");
    },
  });

  commands.register({
    name: "review",
    summary: "Run a fresh, read-only independent review.",
    usages: ["/review [runtime-id|all] [--focus <text>]"],
    execute: async (command) => {
      const target = command.args[0];
      const focus = typeof command.flags.focus === "string" ? command.flags.focus : undefined;
      const results = await service.runReview(target && target !== "all" ? target : undefined, focus);
      return notice("Review complete", `${results.length} reviewer${results.length === 1 ? "" : "s"} finished.`);
    },
  });

  commands.register({
    name: "handoff",
    summary: "Explicitly hand the session to another primary runtime.",
    usages: ["/handoff <runtime-id> [--note <text>]"],
    execute: async (command) => {
      const runtimeId = requireArg(command, 0, "runtime id");
      const noteValue = command.flags.note;
      await service.handoff(runtimeId, typeof noteValue === "string" ? noteValue : undefined);
      return notice("Handoff complete", `${service.runtimes.require(runtimeId).descriptor.displayName} is now primary.`);
    },
  });

  commands.register({
    name: "connect",
    summary: "Connect an agent runtime account.",
    usages: ["/connect <runtime-id> [--method browser|device|api-key]"],
    execute: async (command) => {
      const runtimeId = requireArg(command, 0, "runtime id");
      const method = command.flags.method;
      const parsedMethod = typeof method === "string" ? parseLoginMethod(method) : undefined;
      if (parsedMethod === "api-key") throw new Error("Use Runtime Center's secure Connect dialog for API keys; slash commands are intentionally non-secret");
      const result = await login(runtimeId, parsedMethod ? { method: parsedMethod } : {});
      return loginResultNotice("Connection started", result);
    },
  });
  commands.register({ name: "disconnect", summary: "Disconnect a runtime account.", usages: ["/disconnect <runtime-id>"], execute: async (command) => { const id = requireArg(command, 0, "runtime id"); await service.logout(id); return notice("Disconnected", service.runtimes.require(id).descriptor.displayName); } });
  commands.register({
    name: "account",
    aliases: ["auth"],
    summary: "Inspect, connect, or disconnect provider accounts.",
    usages: ["/account list", "/account login <runtime-id> [method]", "/account logout <runtime-id>"],
    execute: async (command) => {
      const action = command.args[0] ?? "list";
      if (action === "login") {
        const runtimeId = requireArg(command, 1, "runtime id");
        const method = command.args[2] ? parseLoginMethod(command.args[2]) : undefined;
        if (method === "api-key") throw new Error("Use Runtime Center's secure Connect dialog for API keys; slash commands are intentionally non-secret");
        const result = await login(runtimeId, method ? { method } : {});
        return loginResultNotice("Account login", result);
      }
      if (action === "logout") {
        const runtimeId = requireArg(command, 1, "runtime id");
        await service.logout(runtimeId);
        return notice("Account logout", `${runtimeId} disconnected.`);
      }
      if (action !== "list") throw new Error(`unknown account action: ${action}`);
      const diagnostics = await service.probeAll();
      return { kind: "table", title: "Accounts", body: "Credentials remain provider-owned.", rows: diagnostics.map((item) => [item.runtimeId, item.auth.state, item.auth.label ?? item.auth.detail ?? ""])};
    },
  });

  commands.register({
    name: "model",
    summary: "Inspect or select a model for a runtime.",
    usages: ["/model [runtime-id]", "/model [runtime-id] <model-id>"],
    execute: async (command) => {
      const runtimeId = command.args[0] ?? service.snapshot().primaryRuntimeId;
      if (!runtimeId) throw new Error("choose a runtime first");
      const modelId = command.args[1];
      if (modelId) {
        await service.setModel(runtimeId, modelId);
        return notice("Model selected", `${runtimeId} · ${modelId}`);
      }
      const models = await service.listModels(runtimeId);
      return { kind: "table", title: `${runtimeId} models`, body: `${models.length} available`, rows: models.map((model) => [model.id, model.displayName, model.isDefault ? "default" : ""]) };
    },
  });

  commands.register({ name: "effort", summary: "Inspect or select live reasoning effort for a runtime.", usages: ["/effort [runtime-id]", "/effort [runtime-id] default|low|medium|high|xhigh|max"], execute: async (command) => {
    const choices = ["default", "low", "medium", "high", "xhigh", "max"];
    const oneArgIsChoice = command.args.length === 1 && choices.includes(command.args[0] as string);
    const runtimeId = command.args.length > 1 ? requireArg(command, 0, "runtime id") : oneArgIsChoice || command.args.length === 0 ? service.snapshot().primaryRuntimeId : command.args[0];
    if (!runtimeId) throw new Error("choose a runtime first");
    const choice = command.args.length > 1 ? command.args[1] : oneArgIsChoice ? command.args[0] : undefined;
    if (choice) {
      if (!choices.includes(choice)) throw new Error(`invalid effort: ${choice}`);
      await service.setEffort(runtimeId, choice === "default" ? null : choice as RuntimeEffort);
      return notice("Reasoning effort", `${runtimeId} · ${choice}`);
    }
    const efforts = await service.listEfforts(runtimeId);
    return notice("Reasoning options", `${runtimeId} · provider default${efforts.length ? ` · ${efforts.join(", ")}` : " · no configurable levels reported"}`);
  } });
  commands.register({ name: "permissions", summary: "Set the local permission posture for a runtime.", usages: ["/permissions [runtime-id] safe|standard|unrestricted"], execute: async (command) => { const runtimeId = command.args.length > 1 ? requireArg(command, 0, "runtime id") : service.snapshot().primaryRuntimeId; if (!runtimeId) throw new Error("choose a runtime first"); const mode = command.args.length > 1 ? command.args[1] : command.args[0]; if (!mode || !["safe", "standard", "unrestricted"].includes(mode)) throw new Error(`invalid permission mode: ${mode ?? "missing"}`); await service.setPermissionMode(runtimeId, mode as RuntimePermissionMode); return notice("Permission mode", `${runtimeId} · ${mode}`, mode === "unrestricted" ? "warning" : "success"); } });
  commands.register({ name: "voice", summary: "Control spoken assistant replies.", usages: ["/voice on|off|status"], execute: async (command) => { const action = command.args[0] ?? "status"; if (action === "on") await service.setVoice(true); else if (action === "off") await service.setVoice(false); else if (action !== "status") throw new Error(`unknown voice action: ${action}`); return notice("Spoken replies", service.snapshot().voiceEnabled ? "On" : "Off", "neutral"); } });
  commands.register({ name: "theme", summary: "Choose the Studio appearance.", usages: ["/theme system|dark|light"], execute: async (command) => { const theme = requireArg(command, 0, "theme") as ThemeMode; if (!["system", "dark", "light"].includes(theme)) throw new Error(`invalid theme: ${theme}`); await service.setTheme(theme); return notice("Theme", theme); } });
  commands.register({ name: "workspace", aliases: ["project"], summary: "Choose the working folder used by every agent runtime.", usages: ["/workspace show", "/workspace choose", "/workspace clear"], execute: async (command) => {
    const action = command.args[0] ?? "show";
    if (action === "choose") {
      if (!integrations.selectWorkspace) throw new Error("workspace picker is unavailable");
      const selected = await integrations.selectWorkspace();
      if (!selected) return notice("Workspace unchanged", service.snapshot().workspacePath ?? "No explicit workspace selected.", "neutral");
      await service.setWorkspace(selected);
    } else if (action === "clear") await service.setWorkspace(null);
    else if (action !== "show") throw new Error(`unknown workspace action: ${action}`);
    const selected = service.snapshot().workspacePath;
    return notice("Workspace", selected ?? "No workspace selected.", selected ? "success" : "neutral");
  } });
  commands.register({ name: "session", summary: "Create and manage Studio sessions.", usages: ["/session new [name]", "/session list", "/session rename <name>", `/session color <${SESSION_COLORS.join("|")}>`, "/session resume <id>", "/session delete <id> --confirm <id>"], execute: async (command) => {
    const action = command.args[0] ?? "list";
    if (action === "new") { await service.startNewSession(); const name = command.args.slice(1).join(" "); if (name) await service.renameSession(name); return notice("New session", name || service.snapshot().activeSessionId); }
    if (action === "rename") { await service.renameSession(command.args.slice(1).join(" ")); return notice("Session renamed", command.args.slice(1).join(" ")); }
    if (action === "color") { const color = requireArg(command, 1, "session color"); if (!isSessionColor(color)) throw new Error(`invalid session color: ${color}; choose ${SESSION_COLORS.join(", ")}`); await service.updateSession(service.snapshot().activeSessionId, { color }); return notice("Session color", color, "success"); }
    if (action === "resume") { const id = requireArg(command, 1, "session id"); await service.resumeSession(id); return notice("Session resumed", id); }
    if (action === "delete") { const id = requireArg(command, 1, "session id"); const confirmation = command.flags.confirm; if (typeof confirmation !== "string") throw new Error("session deletion requires --confirm <session-id>"); await service.deleteSession(id, confirmation); return notice("Session deleted", `${id} was permanently removed from this device.`, "warning"); }
    if (action !== "list") throw new Error(`unknown session action: ${action}`);
    const sessions = await service.listSessions();
    return { kind: "table", title: "Studio sessions", body: `${sessions.length} local session${sessions.length === 1 ? "" : "s"}`, rows: sessions.map((item) => [item.id, item.name, item.color, String(item.eventCount), item.updatedAt]) };
  } });
  commands.register({ name: "doctor", aliases: ["status"], summary: "Check runtime connectivity and authentication.", usages: ["/doctor [runtime-id|all]", "/status [runtime-id|all]"], execute: async (command) => { const target = command.args[0] ?? "all"; const diagnostics = target === "all" ? await service.probeAll() : [await service.probeRuntime(target)]; return { kind: "table", title: "Studio doctor", body: `${diagnostics.length} checks`, rows: diagnostics.map((item) => [item.runtimeId, item.connection.state, item.auth.state, item.connection.detail]) }; } });
  commands.register({ name: "cancel", summary: "Cancel the active run.", usages: ["/cancel"], execute: async () => notice("Cancel", service.cancel() ? "Cancellation requested." : "No active run.", "neutral") });
  commands.register({ name: "clear", summary: "Start a clean local transcript while preserving settings.", usages: ["/clear"], execute: async () => { await service.startNewSession(); return notice("Transcript cleared", "A new Studio session is active."); } });
  commands.register({ name: "budget", summary: "Set a session token guard across primary, review, compare, and synthesis runs.", usages: ["/budget show", "/budget set <tokens>", "/budget off"], execute: async (command) => {
    const action = command.args[0] ?? "show";
    if (action === "set") await service.setTokenBudget(parseTokenAmount(requireArg(command, 1, "token amount")));
    else if (action === "off") await service.setTokenBudget(null);
    else if (action !== "show") throw new Error(`unknown budget action: ${action}`);
    const snapshot = service.snapshot();
    const coverage = `${snapshot.usage.measuredRuns}/${snapshot.usage.totalRuns} runs reported usage`;
    return notice("Session token guard", snapshot.tokenBudget === null
      ? `Off · ${snapshot.usage.tokens.toLocaleString()} total provider traffic (${snapshot.usage.freshTokens.toLocaleString()} fresh, ${snapshot.usage.cachedTokens.toLocaleString()} cached) · ${coverage}`
      : `${snapshot.usage.tokens.toLocaleString()} / ${snapshot.tokenBudget.toLocaleString()} total provider traffic · ${coverage}. The guard includes cached context, stops the next run, and cannot interrupt a provider mid-turn.`, "neutral");
  } });
  commands.register({ name: "usage", summary: "Show measured token and cost totals by runtime.", usages: ["/usage"], execute: async () => {
    const usage = service.snapshot().usage;
    return {
      kind: "table",
      title: "Session usage",
      body: `${usage.freshTokens.toLocaleString()} fresh · ${usage.cachedTokens.toLocaleString()} cached · ${usage.tokens.toLocaleString()} total provider traffic across ${usage.measuredRuns}/${usage.totalRuns} runs`,
      rows: Object.entries(usage.byRuntime).map(([runtimeId, item]) => [runtimeId, `${item.freshTokens.toLocaleString()} fresh`, `${item.cachedTokens.toLocaleString()} cached`, `${item.tokens.toLocaleString()} total`, item.costUsd ? `$${item.costUsd.toFixed(4)}` : "cost unavailable", `${item.runs} runs`]),
    };
  } });
  commands.register({ name: "export", summary: "Export this session locally without uploading it.", usages: ["/export markdown", "/export json"], execute: async (command) => {
    const format = command.args[0] ?? "markdown";
    if (format !== "markdown" && format !== "json") throw new Error(`invalid export format: ${format}`);
    const content = service.exportSession(format);
    if (!integrations.exportSession) return { kind: "markdown", title: `Session export · ${format}`, body: content };
    const active = service.snapshot().activeSessionId;
    const summary = (await service.listSessions()).find((session) => session.id === active);
    const exported = await integrations.exportSession(format, content, summary?.name ?? "Firekeep Studio session");
    return notice(exported.saved ? "Session exported" : "Export cancelled", exported.detail, exported.saved ? "success" : "neutral");
  } });
  commands.register({
    name: "firekeep",
    summary: "Manage the existing Firekeep Client Kit and Keep connection.",
    usages: [
      "/firekeep status",
      "/firekeep doctor [--report]",
      "/firekeep version",
      "/firekeep personal on|off|status|toggle",
      "/firekeep connect <user@host> [--agent-id <id>] [--remote-dir <path>]",
      "/firekeep update",
      "/firekeep night-shift",
    ],
    execute: async (command) => {
      if (!integrations.firekeep) throw new Error("Firekeep Client Kit integration is unavailable");
      const action = command.args[0] ?? "status";
      const args = [...command.args.slice(1)];
      if (command.flags.report === true) args.push("--report");
      for (const name of ["agent-id", "remote-dir"] as const) {
        const value = command.flags[name];
        if (typeof value === "string") args.push(`--${name}`, value);
      }
      const result = await integrations.firekeep.execute(action, args);
      return {
        kind: "markdown",
        title: result.ok ? `Firekeep ${action}` : `Firekeep ${action} failed`,
        body: result.output || (result.ok ? "Completed." : `Exited with code ${result.exitCode ?? "unknown"}.`),
        tone: result.ok ? "success" : "danger",
      };
    },
  });
  commands.register({
    name: "kiro",
    summary: "Use Kiro CLI as a peer runtime or hand work to the optional Kiro IDE.",
    usages: ["/kiro status", "/kiro use", "/kiro connect [browser|device]", "/kiro open [workspace]"],
    execute: async (command) => {
      const action = command.args[0] ?? "status";
      if (action === "use") return setPrimary("kiro");
      if (action === "connect") {
        const method = (command.args[1] ?? "browser") as LoginMethod;
        if (method !== "browser" && method !== "device") throw new Error("Kiro supports browser or device login");
        return loginResultNotice("Kiro connection", await login("kiro", { method }));
      }
      if (action === "open") {
        if (!integrations.kiroIde) throw new Error("Kiro IDE launcher is unavailable");
        const opened = await integrations.kiroIde.open(command.args.slice(1).join(" ") || service.snapshot().workspacePath || undefined);
        return notice("Kiro IDE", opened.message, "success");
      }
      if (action !== "status") throw new Error(`unknown Kiro action: ${action}`);
      const cli = await service.probeRuntime("kiro");
      const ide = integrations.kiroIde ? await integrations.kiroIde.probe() : { available: false, detail: "IDE launcher unavailable" };
      return { kind: "table", title: "Kiro connectivity", body: "Kiro CLI is the Studio runtime; Kiro IDE is an optional handoff.", rows: [
        ["Kiro CLI", cli.connection.state, cli.auth.state, cli.connection.detail],
        ["Kiro IDE", ide.available ? "ready" : "missing", "external", ide.detail],
      ] };
    },
  });
  commands.register({ name: "shortcuts", summary: "Show keyboard shortcuts.", usages: ["/shortcuts"], execute: async () => ({ kind: "markdown", title: "Keyboard shortcuts", body: "- `Ctrl/Cmd+K` — command palette\n- `Ctrl/Cmd+Enter` — send\n- `Escape` — cancel or close\n- `Ctrl/Cmd+Shift+R` — review" }) });

  return commands;
}

function missionNotice(mission: MissionSnapshot, detail: string): CommandResult {
  const tone: CommandResult["tone"] = mission.phase === "succeeded"
    ? "success"
    : mission.phase === "failed" || mission.phase === "cancelled"
      ? "danger"
      : mission.phase === "paused" || mission.phase === "partial"
        ? "warning"
        : "neutral";
  return notice(
    `Mission · ${mission.phase}`,
    `${detail}\n\n${mission.goal}\n\n${mission.outcome ? `Result: **${mission.outcome.taskResult}** · source: \`${mission.outcome.taskResultSource}\`` : "Task result: **unknown**"}`,
    tone,
  );
}

function missionStatus(mission: MissionSnapshot | null): CommandResult {
  if (!mission) return notice("Mission", "No mission in this session. Start one with `/mission new <goal>`.", "neutral");
  const latestByCheck = new Map<string, MissionSnapshot["checkReceipts"][number]>();
  for (const receipt of mission.checkReceipts) latestByCheck.set(receipt.checkId, receipt);
  return {
    kind: "table",
    title: `Mission · ${mission.phase}`,
    body: [
      mission.goal,
      `${mission.measuredTokens.toLocaleString()} measured tokens${mission.tokenBudget === null ? " · guard off" : ` / ${mission.tokenBudget.toLocaleString()}`}`,
      mission.blockReason ?? "",
    ].filter(Boolean).join(" · "),
    tone: mission.phase === "failed" || mission.phase === "cancelled" ? "danger" : mission.phase === "paused" ? "warning" : "neutral",
    rows: mission.checks.map((check) => {
      const receipt = latestByCheck.get(check.id);
      return [check.name, check.command, receipt ? (receipt.passed ? "passed" : "failed") : "pending"];
    }),
  };
}

function loginResultNotice(title: string, result: LoginResult): CommandResult {
  const detail = result.state === "device" ? `${result.message}\n\nDevice code: **${result.code}**` : result.message;
  return notice(title, detail, result.state === "complete" ? "success" : "neutral");
}

function parseTokenAmount(value: string): number {
  const match = value.trim().replace(/,/g, "").match(/^(\d+(?:\.\d+)?)([km])?$/i);
  if (!match) throw new Error("token amount must look like 12000, 12k, or 1.5m");
  const amount = Number(match[1]);
  const multiplier = match[2]?.toLowerCase() === "m" ? 1_000_000 : match[2] ? 1_000 : 1;
  const tokens = Math.round(amount * multiplier);
  if (!Number.isSafeInteger(tokens) || tokens < 1) throw new Error("token amount must be a positive whole number");
  return tokens;
}

function parseDuration(value: string): number {
  const match = value.trim().match(/^(\d+(?:\.\d+)?)(ms|s|m)?$/i);
  if (!match) throw new Error("duration must look like 500ms, 30s, or 10m");
  const amount = Number(match[1]);
  const unit = match[2]?.toLowerCase() ?? "ms";
  const milliseconds = Math.round(amount * (unit === "m" ? 60_000 : unit === "s" ? 1_000 : 1));
  if (!Number.isSafeInteger(milliseconds) || milliseconds < 1_000 || milliseconds > 30 * 60_000) {
    throw new Error("mission check timeout must be between 1 second and 30 minutes");
  }
  return milliseconds;
}

function parseLoginMethod(value: string): LoginMethod {
  if (!["browser", "device", "api-key", "console", "sso"].includes(value)) throw new Error(`invalid login method: ${value}`);
  return value as LoginMethod;
}
