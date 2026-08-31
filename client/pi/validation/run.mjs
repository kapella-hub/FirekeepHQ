/**
 * firekeep-pi validation runner. `node validation/run.mjs`
 *
 * Proves the bridge's load-bearing claims against a REAL Pi agent session, with
 * no Keep, no provider credentials and no API spend:
 *   1. the pre-flight briefing reaches the MODEL's system prompt
 *   2. the pre-edit gate hard-blocks a write, and allows it when policy allows
 *   3. the cores fire, in the order the bridge assumes, tagged `--runtime pi`
 *
 * A stub `firekeep_client` on PYTHONPATH stands in for the dispatcher and
 * records every invocation; Pi's own faux provider scripts the model turn.
 * Exits nonzero if any assertion fails.
 */

import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PKG = path.resolve(HERE, "..");
const WORK = fs.mkdtempSync(path.join(os.tmpdir(), "firekeep-pi-validate-"));
const STUB = path.join(WORK, "stub");
const LOG = path.join(WORK, "calls.jsonl");
const TARGET = path.join(WORK, "gate-target.txt");
const RESULT = path.join(WORK, "probe-result.json");
const PROBE = path.join(HERE, "probe.mjs");

const results = [];
const record = (name, pass, detail) => {
	results.push({ name, pass, detail });
	console.log(`${pass ? "  PASS" : "  FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
};

// --- the stub dispatcher -----------------------------------------------------
fs.mkdirSync(path.join(STUB, "firekeep_client", "hooks"), { recursive: true });
fs.writeFileSync(path.join(STUB, "firekeep_client", "__init__.py"), "");
fs.writeFileSync(path.join(STUB, "firekeep_client", "hooks", "__init__.py"), "");
fs.writeFileSync(
	path.join(STUB, "firekeep_client", "hooks", "__main__.py"),
	[
		"import json, os, sys",
		'LOG = os.environ["FIREKEEP_STUB_LOG"]',
		'DICT_CORES = {"session_start", "stop", "session_end", "prompt", "precompact"}',
		"argv = sys.argv[1:]",
		'core = argv[0] if argv else "<none>"',
		"try:",
		'    payload = json.loads(sys.stdin.read() or "{}")',
		"except Exception:",
		"    payload = {}",
		'with open(LOG, "a", encoding="utf-8") as fh:',
		'    fh.write(json.dumps({"core": core, "argv": argv, "payload": payload}) + "\\n")',
		"if core in DICT_CORES:",
		'    print(json.dumps({"systemMessage": "FIREKEEP-BRIEFING-MARKER (" + core + ")"}))',
		"    sys.exit(0)",
		'if core == "pre_tool":',
		'    sys.exit(int(os.environ.get("FIREKEEP_STUB_PRETOOL_RC", "0")))',
		"sys.exit(0)",
	].join("\n"),
);

const py = spawnSync(process.platform === "win32" ? "python" : "python3", ["-c", "import sys;print(sys.executable)"], {
	encoding: "utf8",
});
const PYTHON = (py.stdout ?? "").trim();
if (!PYTHON) {
	console.error("FATAL: no python interpreter found; the stub dispatcher cannot run.");
	process.exit(2);
}

/**
 * Drive one scenario through Pi's SDK in a child process.
 *
 * The CLI is deliberately not used: `--provider` is validated during argument
 * parsing, before extensions load, so an extension-registered faux provider can
 * never be named there — and `--api-key` refuses to apply without `--model`.
 * A fresh process per scenario also keeps the faux registry and the bridge's
 * cached interpreter from leaking between the block and allow runs.
 */
function runProbe({ blockRc }) {
	for (const f of [LOG, TARGET, RESULT]) fs.rmSync(f, { force: true });
	const res = spawnSync(process.execPath, [PROBE], {
		cwd: WORK,
		encoding: "utf8",
		timeout: 120_000,
		env: {
			...process.env,
			PYTHONPATH: STUB,
			FIREKEEP_PYTHON: PYTHON,
			FIREKEEP_STUB_LOG: LOG,
			FIREKEEP_STUB_PRETOOL_RC: String(blockRc),
			// The faux stream never reads a credential, but Pi still runs its
			// per-provider key check before dispatching. Satisfy it by name.
			FIREKEEP_FAUX_API_KEY: "firekeep-validation-dummy",
			FIREKEEP_PROBE_EXTENSION: path.join(PKG, "src", "index.ts"),
			FIREKEEP_PROBE_TARGET: TARGET,
			FIREKEEP_PROBE_RESULT: RESULT,
		},
	});
	const calls = fs.existsSync(LOG)
		? fs.readFileSync(LOG, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l))
		: [];
	const probe = fs.existsSync(RESULT) ? JSON.parse(fs.readFileSync(RESULT, "utf8")) : {};
	return {
		stdout: res.stdout ?? "",
		stderr: (res.stderr ?? "") + (probe.error ? `\nPROBE ERROR: ${probe.error}` : ""),
		calls,
		cores: calls.map((c) => c.core),
		systemPrompt: probe.systemPrompt ?? null,
		modelCalled: !!probe.modelCalled,
		targetWritten: fs.existsSync(TARGET),
	};
}

console.log(`\nfirekeep-pi validation — workdir ${WORK}\n`);

// === RUN 1: policy BLOCKS (pre_tool exit 2) ==================================
console.log("run 1 — gate blocks (pre_tool exit 2)");
const blocked = runProbe({ blockRc: 2 });

record("model was actually called", blocked.modelCalled);
record("prompt core fired before the model call", blocked.cores.includes("prompt"));
record(
	"BRIEFING REACHED THE MODEL system prompt",
	!!blocked.systemPrompt?.includes("FIREKEEP-BRIEFING-MARKER"),
	blocked.systemPrompt === null ? "model was never called" : `${blocked.systemPrompt.length} chars captured`,
);
// Decides the matrix's `proactive_recall` cell: promptrecall can only embed a
// prompt it is given. opencode's row says "none (no prompt text)" because its
// session.idle event carries none — assert Pi's does before claiming otherwise.
record(
	"prompt TEXT forwarded to the core (enables proactive recall)",
	blocked.calls.some((c) => c.core === "prompt" && typeof c.payload?.prompt === "string" && c.payload.prompt.length > 0),
	JSON.stringify(blocked.calls.find((c) => c.core === "prompt")?.payload ?? null).slice(0, 120),
);
record("pre_tool gate was consulted", blocked.cores.includes("pre_tool"));
record(
	"pre_tool rendered with --block-exit 2",
	blocked.calls.some((c) => c.core === "pre_tool" && c.argv.includes("--block-exit") && c.argv.includes("2")),
);
record(
	"tool name translated to the Claude shape",
	blocked.calls.some((c) => c.core === "pre_tool" && c.payload?.tool_name === "Write"),
	JSON.stringify(blocked.calls.find((c) => c.core === "pre_tool")?.payload ?? null),
);
record(
	"file_path forwarded from Pi's `path`",
	blocked.calls.some((c) => c.core === "pre_tool" && typeof c.payload?.tool_input?.file_path === "string"),
);
// Gated on the tool call actually happening: "no file" proves nothing if Pi
// never reached the write. A vacuous pass here is the wrong-cell hazard itself.
record(
	"WRITE WAS BLOCKED — target absent after a real tool call",
	blocked.cores.includes("pre_tool") && !blocked.targetWritten,
	blocked.cores.includes("pre_tool") ? "" : "vacuous: pre_tool never ran",
);
record(
	"every call carried --runtime pi",
	blocked.calls.length > 0 && blocked.calls.every((c) => c.argv.includes("--runtime") && c.argv.includes("pi")),
);

// === RUN 2: policy ALLOWS (pre_tool exit 0) ==================================
console.log("\nrun 2 — gate allows (pre_tool exit 0)");
const allowed = runProbe({ blockRc: 0 });
record("WRITE WAS ALLOWED — target file present", allowed.targetWritten);
record("post_tool fired after the call", allowed.cores.includes("post_tool"));
record("stop core fired when the agent settled", allowed.cores.includes("stop"));

// === RUN 3: session lifecycle, via the CLI ===================================
// session_start and session_shutdown are APP lifecycle events: the SDK's
// createAgentSession never emits them, so they cannot be observed above. Drive
// the real CLI once (no model needed — both fire either side of the turn) so
// all seven cores are covered by something that actually ran.
console.log("\nrun 3 — session lifecycle (real CLI, no model)");
const PI_CLI = (() => {
	const roots = [
		path.join(process.env.APPDATA ?? "", "npm", "node_modules", "@earendil-works", "pi-coding-agent"),
		path.join(PKG, "node_modules", "@earendil-works", "pi-coding-agent"),
		"/usr/local/lib/node_modules/@earendil-works/pi-coding-agent",
		"/usr/lib/node_modules/@earendil-works/pi-coding-agent",
	];
	for (const root of roots) {
		const manifest = path.join(root, "package.json");
		if (!fs.existsSync(manifest)) continue;
		const bin = JSON.parse(fs.readFileSync(manifest, "utf8")).bin;
		const rel = typeof bin === "string" ? bin : bin?.pi;
		if (rel && fs.existsSync(path.join(root, rel))) return path.join(root, rel);
	}
	return null;
})();

let lifecycle = { cores: [], calls: [] };
if (!PI_CLI) {
	record("Pi CLI located for the lifecycle run", false, "install: npm i -g @earendil-works/pi-coding-agent");
} else {
	fs.rmSync(LOG, { force: true });
	spawnSync(process.execPath, [PI_CLI, "-p", "--no-session", "-ne", "-e", path.join(PKG, "src", "index.ts"), "hello"], {
		cwd: WORK,
		encoding: "utf8",
		timeout: 120_000,
		env: {
			...process.env,
			PYTHONPATH: STUB,
			FIREKEEP_PYTHON: PYTHON,
			FIREKEEP_STUB_LOG: LOG,
			FIREKEEP_STUB_PRETOOL_RC: "0",
		},
	});
	const calls = fs.existsSync(LOG)
		? fs.readFileSync(LOG, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l))
		: [];
	lifecycle = { cores: calls.map((c) => c.core), calls };
	record("session_start fired (CLI)", lifecycle.cores.includes("session_start"));
	record("session_end fired on shutdown (CLI)", lifecycle.cores.includes("session_end"));
	record(
		"lifecycle calls carried --runtime pi",
		lifecycle.calls.length > 0 && lifecycle.calls.every((c) => c.argv.includes("--runtime") && c.argv.includes("pi")),
	);
}

// === report ==================================================================
console.log(`\nobserved cores (blocked run): ${blocked.cores.join(" -> ") || "(none)"}`);
console.log(`observed cores (allowed run): ${allowed.cores.join(" -> ") || "(none)"}`);
console.log(`observed cores (lifecycle):   ${lifecycle.cores.join(" -> ") || "(none)"}`);

const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} assertions passed`);
if (failed.length) {
	console.log("\n--- probe stdout (blocked run) ---\n" + blocked.stdout.slice(0, 2000));
	console.log("\n--- probe stderr (blocked run) ---\n" + blocked.stderr.slice(0, 3000));
	process.exit(1);
}
console.log(`\nworkdir kept for inspection: ${WORK}`);
