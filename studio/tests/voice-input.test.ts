import { describe, expect, it, vi } from "vitest";
import type { ProcessResult, RunProcessOptions } from "../src/main/runtime/process.js";
import { WindowsVoiceInput } from "../src/main/voice-input.js";

const processResult = (stdout = "", exitCode: number | null = 0, stderr = ""): ProcessResult => ({
  exitCode,
  signal: null,
  stdout,
  stderr,
  timedOut: false,
  truncated: false,
  durationMs: 10,
});

describe("Windows voice input", () => {
  it("runs only the fixed local speech recognizer with bounded cancellation", async () => {
    const run = vi.fn(async (_command: string, _args: readonly string[], _options?: RunProcessOptions) => processResult('{"text":"hello firekeep","confidence":0.81}'));
    const voice = new WindowsVoiceInput({ platform: "win32", systemRoot: "C:\\Windows", run });

    await expect(voice.transcribe("en-US")).resolves.toEqual({
      state: "complete",
      text: "hello firekeep",
      detail: "Transcribed with Windows Speech Recognition.",
    });

    expect(run).toHaveBeenCalledOnce();
    const [command, args, options] = run.mock.calls[0] as [string, readonly string[], RunProcessOptions];
    expect(command).toBe("C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe");
    expect(args.slice(0, -1)).toEqual(["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand"]);
    expect(Buffer.from(args.at(-1) ?? "", "base64").toString("utf16le")).toContain("SetInputToDefaultAudioDevice");
    expect(options).toMatchObject({ timeoutMs: 60_000, outputLimit: 32_768, killTree: true });
    expect(options.env?.FIREKEEP_STUDIO_VOICE_LANGUAGE).toBe("en-US");
  });

  it("cancels the exact active recognizer and reports cancellation without stale text", async () => {
    const run = vi.fn((_command: string, _args: readonly string[], options?: RunProcessOptions) => new Promise<ProcessResult>((resolve) => {
      options?.signal?.addEventListener("abort", () => resolve(processResult("", null)), { once: true });
    }));
    const voice = new WindowsVoiceInput({ platform: "win32", systemRoot: "C:\\Windows", run });

    const pending = voice.transcribe();
    expect(run).toHaveBeenCalledOnce();
    expect(voice.cancel()).toBe(true);

    await expect(pending).resolves.toEqual({ state: "cancelled", text: "", detail: "Voice input stopped." });
    expect(voice.cancel()).toBe(false);
  });

  it("is honest off Windows and turns recognizer failures into actionable errors", async () => {
    const unused = vi.fn(async () => processResult());
    await expect(new WindowsVoiceInput({ platform: "linux", run: unused }).transcribe()).resolves.toMatchObject({ state: "unavailable", text: "" });
    expect(unused).not.toHaveBeenCalled();

    const failed = vi.fn(async () => processResult("", 1, "No Windows speech recognizer is installed."));
    await expect(new WindowsVoiceInput({ platform: "win32", systemRoot: "C:\\Windows", run: failed }).transcribe()).rejects.toThrow(/No Windows speech recognizer is installed/i);
  });

  it("rejects overlapping listeners and handles silence without fabricating text", async () => {
    let finish: ((result: ProcessResult) => void) | undefined;
    const run = vi.fn(async () => new Promise<ProcessResult>((resolve) => { finish = resolve; }));
    const voice = new WindowsVoiceInput({ platform: "win32", systemRoot: "C:\\Windows", run });

    const pending = voice.transcribe();
    await expect(voice.transcribe()).rejects.toThrow(/already listening/i);
    finish?.(processResult('{"text":"","confidence":0}'));

    await expect(pending).resolves.toMatchObject({ state: "empty", text: "" });
  });
});
