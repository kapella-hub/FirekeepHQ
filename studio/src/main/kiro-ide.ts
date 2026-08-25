import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { posix, win32 } from "node:path";

export interface KiroIdeProbe {
  readonly available: boolean;
  readonly detail: string;
  readonly executable?: string;
}

interface KiroIdeLauncherOptions {
  readonly platform?: NodeJS.Platform;
  readonly env?: NodeJS.ProcessEnv;
  readonly exists?: (path: string) => boolean;
  readonly cwd?: () => string;
  readonly launch?: (executable: string, args: readonly string[]) => void;
}

export class KiroIdeLauncher {
  readonly #platform: NodeJS.Platform;
  readonly #env: NodeJS.ProcessEnv;
  readonly #exists: (path: string) => boolean;
  readonly #cwd: () => string;
  readonly #launch: (executable: string, args: readonly string[]) => void;

  constructor(options: KiroIdeLauncherOptions = {}) {
    this.#platform = options.platform ?? process.platform;
    this.#env = options.env ?? process.env;
    this.#exists = options.exists ?? existsSync;
    this.#cwd = options.cwd ?? (() => process.cwd());
    this.#launch = options.launch ?? ((executable, args) => {
      const child = spawn(executable, [...args], { detached: true, stdio: "ignore", windowsHide: true });
      child.once("error", (error) => console.warn("Kiro IDE launch failed", error.message));
      child.unref();
    });
  }

  async probe(): Promise<KiroIdeProbe> {
    const executable = this.#candidates().find((candidate) => this.#exists(candidate));
    return executable
      ? { available: true, executable, detail: `Installed at ${executable}` }
      : { available: false, detail: "Kiro IDE was not found. Kiro CLI remains fully available in Studio." };
  }

  async open(workspace?: string): Promise<{ readonly message: string }> {
    const probe = await this.probe();
    if (!probe.available || !probe.executable) throw new Error(probe.detail);
    const target = this.#pathApi().resolve(workspace?.trim() || this.#cwd());
    if (!this.#exists(target)) throw new Error(`workspace does not exist: ${target}`);
    this.#launch(probe.executable, [target]);
    return { message: `Opened ${target}. Kiro IDE is an explicit external handoff; Kiro CLI remains the Studio runtime.` };
  }

  #candidates(): string[] {
    const pathApi = this.#pathApi();
    const explicit = this.#env.KIRO_IDE_PATH?.trim();
    const candidates = explicit ? [explicit] : [];
    if (this.#platform === "win32") {
      for (const root of [this.#env.LOCALAPPDATA, this.#env.ProgramFiles, this.#env["ProgramFiles(x86)"]]) {
        if (!root) continue;
        candidates.push(pathApi.join(root, "Programs", "Kiro", "Kiro.exe"), pathApi.join(root, "Kiro", "Kiro.exe"));
      }
      candidates.push(...this.#pathCandidates(["Kiro.exe", "kiro.exe"]));
    } else if (this.#platform === "darwin") {
      candidates.push("/Applications/Kiro.app/Contents/MacOS/Kiro", pathApi.join(this.#env.HOME ?? "", "Applications", "Kiro.app", "Contents", "MacOS", "Kiro"));
      candidates.push(...this.#pathCandidates(["kiro"]));
    } else {
      candidates.push("/usr/local/bin/kiro", "/usr/bin/kiro", ...this.#pathCandidates(["kiro"]));
    }
    return [...new Set(candidates.filter(Boolean))];
  }

  #pathCandidates(names: readonly string[]): string[] {
    const pathApi = this.#pathApi();
    return (this.#env.PATH ?? "").split(pathApi.delimiter).filter(Boolean).flatMap((directory) => names.map((name) => pathApi.join(directory, name)));
  }

  #pathApi(): typeof posix | typeof win32 {
    return this.#platform === "win32" ? win32 : posix;
  }
}
