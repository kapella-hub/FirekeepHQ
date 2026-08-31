/**
 * One validation scenario, driven through Pi's SDK rather than its CLI.
 *
 * The CLI cannot be used here: `--provider` is validated during argument
 * parsing, which happens BEFORE extensions load, so a faux provider registered
 * by an extension can never be named on the command line — and `--api-key`
 * refuses to apply without a `--model`. The SDK takes the model object
 * directly, so the whole credential path is bypassed and no network call, key,
 * or spend is involved.
 *
 * Reads its scenario from the environment, writes observations to
 * FIREKEEP_PROBE_RESULT as JSON, and always exits 0 — run.mjs owns the
 * assertions so a probe crash is reported as data, not as a silent pass.
 */

import * as fs from "node:fs";
import * as nodePath from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createAgentSession, DefaultResourceLoader, getAgentDir, SessionManager } from "@earendil-works/pi-coding-agent";

// pi-ai must be the SAME module instance pi-coding-agent loaded, not our own
// top-level copy: the api registry is module state, so registering the faux
// stream into a second instance leaves the agent's registry empty and the turn
// dies with "No API provider registered for api: ...". The package's `exports`
// declare only an `import` condition, so require.resolve cannot walk it —
// address the nested dist files directly instead.
const AGENT_DIR_ROOT = nodePath.dirname(
	fileURLToPath(import.meta.resolve("@earendil-works/pi-coding-agent")),
);
const nestedPiAi = (rel) => {
	let dir = AGENT_DIR_ROOT;
	for (let i = 0; i < 6; i++) {
		const candidate = nodePath.join(dir, "node_modules", "@earendil-works", "pi-ai", "dist", rel);
		if (fs.existsSync(candidate)) return pathToFileURL(candidate).href;
		dir = nodePath.dirname(dir);
	}
	// Hoisted/deduped install: one shared copy, so our own specifier is the same instance.
	return null;
};
const compatUrl = nestedPiAi("compat.js");
const fauxUrl = nestedPiAi(nodePath.join("providers", "faux.js"));
const { registerFauxProvider } = await import(compatUrl ?? "@earendil-works/pi-ai/compat");
const { fauxAssistantMessage, fauxText, fauxToolCall } = await import(
	fauxUrl ?? "@earendil-works/pi-ai/providers/faux"
);

const EXT = process.env.FIREKEEP_PROBE_EXTENSION;
const TARGET = process.env.FIREKEEP_PROBE_TARGET;
const RESULT = process.env.FIREKEEP_PROBE_RESULT;

const observed = { systemPrompt: null, modelCalled: false, error: null, toolCalls: [] };

const faux = registerFauxProvider({
	provider: "firekeep-faux",
	api: "firekeep-faux-api",
	models: [{ id: "gate-probe", name: "Firekeep gate probe" }],
});

faux.setResponses([
	// Turn 1: capture exactly what the model was handed, then ask for a write.
	(context) => {
		observed.modelCalled = true;
		observed.systemPrompt = context.systemPrompt ?? "";
		return fauxAssistantMessage(
			[fauxToolCall("write", { path: TARGET, content: "GATE-BREACH: the write was not blocked.\n" })],
			{ stopReason: "toolUse" },
		);
	},
	// Turn 2: stop, whatever became of the tool call.
	fauxAssistantMessage([fauxText("done")], { stopReason: "stop" }),
]);

const resourceLoader = new DefaultResourceLoader({
	cwd: process.cwd(),
	agentDir: getAgentDir(),
	// The extension under test, loaded from source exactly as a user would get
	// it from npm. jiti compiles the TS; there is no build step to skip.
	additionalExtensionPaths: [EXT],
	extensionFactories: [
		// `registerFauxProvider` registers the stream implementation but no
		// PROVIDER entry, so Pi's credential check finds nothing for it and
		// refuses to dispatch. Register the provider properly here, pointing at
		// the faux api and resolving a throwaway key from the environment —
		// the same `apiKey: "$VAR"` shape the custom-provider example uses.
		(pi) => {
			pi.registerProvider("firekeep-faux", {
				api: "firekeep-faux-api",
				baseUrl: "http://firekeep.invalid",
				apiKey: "$FIREKEEP_FAUX_API_KEY",
				models: [
					{
						id: "gate-probe",
						name: "Firekeep gate probe",
						reasoning: false,
						input: ["text"],
						cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
						contextWindow: 200000,
						maxTokens: 8192,
					},
				],
			});
		},
	],
});

try {
	await resourceLoader.reload();
	// Prefer the registered provider's model (it carries the credential
	// mapping); fall back to the raw faux model if lookup fails.
	const { session } = await createAgentSession({
		cwd: process.cwd(),
		model: faux.getModel(),
		resourceLoader,
		sessionManager: SessionManager.inMemory(),
	});
	try {
		session.subscribe((event) => {
			if (event.type === "tool_execution_start") observed.toolCalls.push(event.toolName);
		});
		await session.prompt(process.env.FIREKEEP_PROBE_PROMPT ?? "Write the target file.");
		observed.messages = (session.state?.messages ?? []).map((m) => ({
			role: m.role,
			stopReason: m.stopReason,
			errorMessage: m.errorMessage,
			content: JSON.stringify(m.content ?? "").slice(0, 300),
		}));
		observed.model = { id: faux.getModel()?.id, provider: faux.getModel()?.provider, api: faux.getModel()?.api };
	} finally {
		session.dispose();
	}
} catch (err) {
	observed.error = String(err?.stack ?? err);
}

fs.writeFileSync(RESULT, JSON.stringify(observed, null, 1), "utf8");
