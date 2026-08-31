/**
 * CLI-drivable faux model, for the END-TO-END validation run. NOT shipped.
 *
 * validation/probe.mjs covers the SDK path, but the SDK never emits
 * `session_start`, so it cannot prove the thing that matters most: that the
 * PRE-FLIGHT BRIEFING — fetched by the real dispatcher from a real Keep at
 * session start — reaches the model's system prompt. Only the CLI fires that
 * event, and the CLI cannot be given a faux model on the command line
 * (`--provider` is validated during argument parsing, before extensions load).
 *
 * So the model is registered from INSIDE an extension instead:
 *   1. `registerFauxProvider` supplies the stream implementation, and
 *   2. `pi.registerProvider` supplies the provider ENTRY with a credential
 *      mapping — without it Pi's key check refuses to dispatch, and
 *   3. `pi.setModel` selects it once both halves exist.
 *
 * pi-ai must be the same module instance pi-coding-agent loaded; see probe.mjs.
 */

import * as fs from "node:fs";
import * as nodePath from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const AGENT_ROOT = nodePath.dirname(
	fileURLToPath(import.meta.resolve("@earendil-works/pi-coding-agent")),
);
const nestedPiAi = (rel: string): string | null => {
	// When Pi runs from a GLOBAL install, this file's own module resolution finds
	// the repo's local pi-coding-agent copy instead of the one actually running —
	// and registering into that copy's registry produces "No API provider
	// registered for api: ...". The runner passes the running Pi's dist dir.
	const explicit = process.env.FIREKEEP_PI_AI_DIST;
	if (explicit) {
		const candidate = nodePath.join(explicit, rel);
		if (fs.existsSync(candidate)) return pathToFileURL(candidate).href;
	}
	let dir = AGENT_ROOT;
	for (let i = 0; i < 6; i++) {
		const candidate = nodePath.join(dir, "node_modules", "@earendil-works", "pi-ai", "dist", rel);
		if (fs.existsSync(candidate)) return pathToFileURL(candidate).href;
		dir = nodePath.dirname(dir);
	}
	return null;
};

const { registerFauxProvider } = await import(
	/* @vite-ignore */ nestedPiAi("compat.js") ?? "@earendil-works/pi-ai/compat"
);
const { fauxAssistantMessage, fauxText } = await import(
	/* @vite-ignore */ nestedPiAi(nodePath.join("providers", "faux.js")) ??
		"@earendil-works/pi-ai/providers/faux"
);

const SINK = process.env.FIREKEEP_VALIDATE_PROMPT_SINK ?? "captured-system-prompt.txt";

export default function harness(pi: ExtensionAPI) {
	const faux = registerFauxProvider({
		provider: "firekeep-faux",
		api: "firekeep-faux-api",
		models: [{ id: "gate-probe", name: "Firekeep gate probe" }],
	});

	// Capture what the model was ACTUALLY handed, then answer without tools so
	// the run terminates on its own.
	faux.setResponses([
		(context: { systemPrompt?: string }) => {
			fs.writeFileSync(SINK, context.systemPrompt ?? "<none>", "utf8");
			return fauxAssistantMessage([fauxText("ok")], { stopReason: "stop" });
		},
	]);

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

	pi.on("session_start", async () => {
		await pi.setModel(faux.getModel());
	});
}
