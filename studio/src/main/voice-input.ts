import { win32 } from "node:path";
import type { VoiceInputOutcome } from "../shared/ipc.js";
import { runProcess, type ProcessResult, type RunProcessOptions } from "./runtime/process.js";

export interface VoiceInput {
  transcribe(language?: string): Promise<VoiceInputOutcome>;
  cancel(): boolean;
}

type ProcessRunner = (command: string, args: readonly string[], options?: RunProcessOptions) => Promise<ProcessResult>;

interface WindowsVoiceInputOptions {
  readonly platform?: NodeJS.Platform;
  readonly systemRoot?: string;
  readonly run?: ProcessRunner;
}

const WINDOWS_SPEECH_SCRIPT = String.raw`
$ErrorActionPreference = "Stop"
trap { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Speech

$requestedLanguage = [Environment]::GetEnvironmentVariable("FIREKEEP_STUDIO_VOICE_LANGUAGE")
try { $requestedCulture = [Globalization.CultureInfo]::GetCultureInfo($requestedLanguage) }
catch { $requestedCulture = [Globalization.CultureInfo]::GetCultureInfo("en-US") }

$recognizers = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()
$selected = $recognizers | Where-Object { $_.Culture.Name -eq $requestedCulture.Name } | Select-Object -First 1
if ($null -eq $selected) {
  $selected = $recognizers | Where-Object { $_.Culture.TwoLetterISOLanguageName -eq $requestedCulture.TwoLetterISOLanguageName } | Select-Object -First 1
}
if ($null -eq $selected) { $selected = $recognizers | Select-Object -First 1 }
if ($null -eq $selected) { throw "No Windows speech recognizer is installed." }

$engine = [System.Speech.Recognition.SpeechRecognitionEngine]::new($selected)
try {
  $engine.InitialSilenceTimeout = [TimeSpan]::FromSeconds(10)
  $engine.BabbleTimeout = [TimeSpan]::FromSeconds(10)
  $engine.EndSilenceTimeout = [TimeSpan]::FromMilliseconds(900)
  $engine.EndSilenceTimeoutAmbiguous = [TimeSpan]::FromMilliseconds(1400)
  $engine.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
  $engine.SetInputToDefaultAudioDevice()
  $result = $engine.Recognize([TimeSpan]::FromSeconds(45))
  $payload = if ($null -eq $result) {
    @{ text = ""; confidence = 0; recognizer = $selected.Description }
  } else {
    @{ text = $result.Text; confidence = $result.Confidence; recognizer = $selected.Description }
  }
  [Console]::Out.WriteLine(($payload | ConvertTo-Json -Compress))
} finally {
  $engine.Dispose()
}
`;

const ENCODED_WINDOWS_SPEECH_SCRIPT = Buffer.from(WINDOWS_SPEECH_SCRIPT, "utf16le").toString("base64");

export class WindowsVoiceInput implements VoiceInput {
  readonly #platform: NodeJS.Platform;
  readonly #systemRoot: string | undefined;
  readonly #run: ProcessRunner;
  #active: AbortController | null = null;

  constructor(options: WindowsVoiceInputOptions = {}) {
    this.#platform = options.platform ?? process.platform;
    this.#systemRoot = options.systemRoot ?? process.env.SystemRoot;
    this.#run = options.run ?? runProcess;
  }

  async transcribe(language = "en-US"): Promise<VoiceInputOutcome> {
    if (this.#platform !== "win32") return unavailable("Voice input currently requires the Windows desktop speech recognizer.");
    if (!this.#systemRoot) return unavailable("Windows voice input is unavailable because SystemRoot is not configured.");
    if (this.#active) throw new Error("Voice input is already listening.");

    const active = new AbortController();
    this.#active = active;
    try {
      const result = await this.#run(
        win32.join(this.#systemRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
        ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", ENCODED_WINDOWS_SPEECH_SCRIPT],
        {
          env: { ...process.env, FIREKEEP_STUDIO_VOICE_LANGUAGE: language.trim() || "en-US" },
          timeoutMs: 60_000,
          outputLimit: 32_768,
          signal: active.signal,
          killTree: true,
        },
      );
      if (active.signal.aborted) return { state: "cancelled", text: "", detail: "Voice input stopped." };
      if (result.timedOut) throw new Error("Voice input timed out after 60 seconds.");
      if (result.exitCode !== 0) throw new Error(`Voice input failed: ${failureDetail(result)}`);
      return parseTranscription(result.stdout);
    } catch (error) {
      if (active.signal.aborted) return { state: "cancelled", text: "", detail: "Voice input stopped." };
      const code = error && typeof error === "object" && "code" in error ? error.code : undefined;
      if (code === "ENOENT") return unavailable("Windows PowerShell is unavailable, so Studio cannot start local voice input.");
      throw error;
    } finally {
      if (this.#active === active) this.#active = null;
    }
  }

  cancel(): boolean {
    if (!this.#active) return false;
    this.#active.abort();
    return true;
  }
}

function parseTranscription(stdout: string): VoiceInputOutcome {
  const line = stdout.trim().split(/\r?\n/).at(-1);
  if (!line) return { state: "empty", text: "", detail: "No speech was detected. Check the microphone and try again." };
  let value: unknown;
  try { value = JSON.parse(line); }
  catch { throw new Error("Voice input returned an unreadable transcription."); }
  if (!value || typeof value !== "object" || !("text" in value) || typeof value.text !== "string") {
    throw new Error("Voice input returned an invalid transcription.");
  }
  const text = value.text.trim();
  return text
    ? { state: "complete", text, detail: "Transcribed with Windows Speech Recognition." }
    : { state: "empty", text: "", detail: "No speech was detected. Check the microphone and try again." };
}

function failureDetail(result: ProcessResult): string {
  const detail = (result.stderr || result.stdout).trim().split(/\r?\n/).find((line) => line.trim())?.trim();
  return detail?.slice(0, 500) || `Windows Speech Recognition exited with code ${result.exitCode ?? "unknown"}.`;
}

function unavailable(detail: string): VoiceInputOutcome {
  return { state: "unavailable", text: "", detail };
}
