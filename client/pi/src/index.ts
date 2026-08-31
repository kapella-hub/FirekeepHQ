/**
 * firekeep-pi — Firekeep hook bridge for the Pi coding agent (https://pi.dev).
 *
 * Bridges Pi's extension event surface to the SAME seven hook cores the claude,
 * kiro and opencode adapters use, via the dispatcher
 * (`{python} -m firekeep_client.hooks <core> --runtime pi`), stdin JSON in,
 * stdout JSON / exit codes out. No Firekeep logic lives here: this file is a
 * translation layer and nothing else, so a core change reaches Pi with no
 * republish.
 *
 * WHY PI GETS MORE THAN OPENCODE. Pi is the first non-Claude runtime with a
 * native model-facing context channel: `before_agent_start` may return
 * `{ systemPrompt }`, so the pre-flight briefing lands in the context window
 * instead of the terminal. The opencode bridge has to `console.log` the same
 * text (see adapters/opencode.py: "opencode has no systemMessage channel, so
 * the briefing/inbox text is LOGGED, not injected into model context"). Pi also
 * exposes `tool_call` with a first-class `{ block, reason }` return, so the
 * pre-edit gate HARD-BLOCKS rather than throwing (opencode) or advising (kiro).
 *
 * Deliberately NOT using the dispatcher's `hookSpecificOutput.additionalContext`
 * decoration: that shape is Claude Code's, and `_MODEL_CONTEXT_RUNTIMES` in
 * hooks/__main__.py is `{"claude"}` on purpose — "handing them a Claude
 * Code-shaped payload would be the same guess in a new place". Pi's channel is
 * its own, so this bridge reads the core's plain `systemMessage` and appends it
 * to Pi's own `systemPrompt`.
 *
 * AVAILABILITY OVER ENFORCEMENT. Every spawn is wrapped: a missing interpreter,
 * an unreachable Keep, a timeout or malformed stdout degrades to "allow and
 * continue". A broken bridge must never break the user's session — the same
 * contract the cores hold for an unreachable server.
 */

import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type {
	ExtensionAPI,
	ExtensionContext,
	SessionStartEvent,
	SessionShutdownEvent,
	ToolCallEvent,
	ToolResultEvent,
	BeforeAgentStartEvent,
} from "@earendil-works/pi-coding-agent";

/** Wall-clock ceilings per core. A hook that outlives these is abandoned, not awaited. */
const TIMEOUT_MS = {
	session_start: 15_000,
	prompt: 8_000,
	pre_tool: 8_000,
	post_tool: 5_000,
	stop: 5_000,
	session_end: 8_000,
	precompact: 8_000,
} as const;

type CoreName = keyof typeof TIMEOUT_MS;

/**
 * Pi tool name -> the Claude-shaped name the hook cores understand.
 * `pre_tool._EDIT_TOOLS` matches on the Claude names only; an unmapped tool is
 * not forwarded at all, which is why `read`/`grep`/`find`/`ls` are absent —
 * they are not gated and forwarding them would be pure overhead per call.
 * `powershell` maps to Bash: both are "run a shell command" from the gate's
 * point of view, and the gate reads `command`, not the shell dialect.
 */
const TOOL_NAMES: Record<string, string> = {
	edit: "Edit",
	write: "Write",
	bash: "Bash",
	powershell: "Bash",
};

/** Pi's own config lives under `.pi`; the adapter drops its sidecar beside Firekeep's config. */
const SIDECAR = path.join(os.homedir(), ".firekeep", "pi-extension.json");

let cachedPython: string | null | undefined;

/**
 * Resolve the interpreter that owns `firekeep_client`.
 *
 * Order matters: the sidecar is written by `firekeep install --runtime pi` and
 * names the kit's venv exactly, so it wins over anything on PATH. The bare
 * fallbacks keep this package usable for a Pi user who installed it from npm
 * without the client kit — they will simply get no hooks until a `firekeep_client`
 * is importable, which is the honest degradation.
 */
function resolvePython(): string | null {
	if (cachedPython !== undefined) return cachedPython;
	cachedPython = (() => {
		const fromEnv = process.env.FIREKEEP_PYTHON;
		if (fromEnv && fs.existsSync(fromEnv)) return fromEnv;
		try {
			const raw = fs.readFileSync(SIDECAR, "utf8");
			const python = JSON.parse(raw)?.python;
			if (typeof python === "string" && fs.existsSync(python)) return python;
		} catch {
			// No sidecar, unreadable, or malformed — fall through to PATH.
		}
		return process.platform === "win32" ? "python" : "python3";
	})();
	return cachedPython;
}

interface CoreResult {
	status: number | null;
	stdout: string;
}

/** Run one hook core. Returns null when the core could not be run at all. */
function runCore(core: CoreName, payload: unknown, extra: string[] = []): CoreResult | null {
	const python = resolvePython();
	if (!python) return null;
	try {
		const res = spawnSync(
			python,
			["-m", "firekeep_client.hooks", core, "--runtime", "pi", ...extra],
			{
				input: JSON.stringify(payload ?? {}),
				timeout: TIMEOUT_MS[core],
				encoding: "utf8",
				windowsHide: true,
			},
		);
		// `error` is set for ENOENT and for a timeout kill; both mean "no verdict".
		if (res.error) return null;
		return { status: res.status, stdout: res.stdout ?? "" };
	} catch {
		return null; // a broken bridge must never break the session
	}
}

/** The human-facing text a dict core emits, or "" when there is none. */
function messageOf(res: CoreResult | null): string {
	if (!res?.stdout) return "";
	try {
		const msg = JSON.parse(res.stdout)?.systemMessage;
		return typeof msg === "string" ? msg.trim() : "";
	} catch {
		return ""; // non-JSON stdout is not an error: the core may have logged instead
	}
}

/** Show text to the human without assuming a UI exists (print/JSON modes have none). */
function surface(ctx: ExtensionContext, text: string): void {
	if (!text) return;
	if (ctx.hasUI) {
		try {
			ctx.ui.notify(text, "info");
			return;
		} catch {
			// fall through to stdout
		}
	}
	console.log(`[firekeep] ${text}`);
}

export default function firekeepPi(pi: ExtensionAPI) {
	// Text captured at session_start, held until the first before_agent_start can
	// put it in the context window. Pi has no "inject at session start" channel —
	// the system prompt is only assembled per agent run — so the briefing waits
	// one beat rather than being logged and lost.
	let pendingContext: string[] = [];

	pi.on("session_start", async (event: SessionStartEvent, ctx) => {
		// "reload" is an extension reload, not a new working session: re-running
		// session_start there would register a second presence for one human.
		if (event.reason === "reload") return;
		const res = runCore("session_start", {
			session_id: ctx.sessionManager.getSessionId() ?? "",
			cwd: ctx.cwd,
			reason: event.reason,
		});
		const msg = messageOf(res);
		if (msg) {
			pendingContext.push(msg);
			surface(ctx, msg);
		}
	});

	// The prompt core is the heartbeat + inbox poll, AND the model-context event.
	pi.on("before_agent_start", async (event: BeforeAgentStartEvent, ctx) => {
		const res = runCore("prompt", { prompt: event.prompt, cwd: ctx.cwd });
		const msg = messageOf(res);
		if (msg) surface(ctx, msg);

		const blocks = [...pendingContext, msg].filter(Boolean);
		pendingContext = [];
		if (blocks.length === 0) return;

		// Pi's native model channel. Appending (not replacing) keeps every
		// resource Pi already assembled — AGENTS.md, skills, tool guidelines.
		return {
			systemPrompt: `${event.systemPrompt}\n\n${blocks.join("\n\n")}\n`,
		};
	});

	// The safety gate. `--block-exit 2` remaps every nonzero core code to 2, so
	// this bridge blocks on exactly one value and treats everything else —
	// including a crashed or missing interpreter — as allow.
	pi.on("tool_call", async (event: ToolCallEvent, _ctx) => {
		const claudeName = TOOL_NAMES[event.toolName];
		if (!claudeName) return; // ungated tool: no spawn, no cost

		const input = event.input as Record<string, unknown>;
		const res = runCore(
			"pre_tool",
			{
				tool_name: claudeName,
				tool_input: {
					// Pi calls it `path`; the cores read Claude's `file_path`.
					file_path: typeof input.path === "string" ? input.path : undefined,
					command: typeof input.command === "string" ? input.command : undefined,
				},
			},
			["--block-exit", "2"],
		);
		if (res?.status !== 2) return;

		const reason = messageOf(res) || "Firekeep blocked this call (policy or a lease held by another agent).";
		// `terminate` asks Pi to stop after the batch rather than let the model
		// immediately retry a call that policy just refused.
		return { block: true, reason, terminate: true };
	});

	pi.on("tool_result", async (event: ToolResultEvent, _ctx) => {
		const claudeName = TOOL_NAMES[event.toolName];
		if (!claudeName) return;
		const input = event.input as Record<string, unknown>;
		runCore("post_tool", {
			tool_name: claudeName,
			tool_input: {
				file_path: typeof input.path === "string" ? input.path : undefined,
				command: typeof input.command === "string" ? input.command : undefined,
			},
			is_error: event.isError,
		});
	});

	// Claude fires Stop at every turn end; `agent_settled` is Pi's equivalent —
	// it fires once the loop has stopped retrying, not once per streamed turn.
	pi.on("agent_settled", async (_event, ctx) => {
		surface(ctx, messageOf(runCore("stop", { cwd: ctx.cwd })));
	});

	// Pi is the second runtime after Claude Code with a real compaction event.
	pi.on("session_before_compact", async (_event, ctx) => {
		runCore("precompact", { session_id: ctx.sessionManager.getSessionId() ?? "" });
	});

	pi.on("session_shutdown", async (event: SessionShutdownEvent, ctx) => {
		if (event.reason === "reload") return; // symmetry with session_start
		runCore("session_end", {
			session_id: ctx.sessionManager.getSessionId() ?? "",
			reason: event.reason,
		});
	});
}
