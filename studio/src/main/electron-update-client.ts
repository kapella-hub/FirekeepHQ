import { createRequire } from "node:module";
import type { AppUpdater, ProgressInfo, UpdateDownloadedEvent } from "electron-updater";
import type { NativeUpdateClient } from "./studio-updater.js";

const require = createRequire(import.meta.url);

export class ElectronUpdateClient implements NativeUpdateClient {
  readonly #updater: AppUpdater;

  constructor(updater?: AppUpdater) {
    // electron-updater is CommonJS and exposes autoUpdater through a lazy getter.
    // Loading that getter as a top-level ESM named import can stall Electron before
    // app.whenReady() in a packaged app, so resolve it only after app readiness.
    this.#updater = updater ?? (require("electron-updater") as { readonly autoUpdater: AppUpdater }).autoUpdater;
    this.#updater.autoDownload = false;
    this.#updater.autoInstallOnAppQuit = false;
    this.#updater.allowPrerelease = false;
    this.#updater.logger = console;
  }

  async check(): Promise<{ readonly version: string } | null> {
    const result = await this.#updater.checkForUpdates();
    return result ? { version: result.updateInfo.version } : null;
  }

  async download(progress: (percent: number) => void): Promise<string> {
    const onProgress = (value: ProgressInfo): void => progress(value.percent);
    let downloadedFile: string | null = null;
    const onDownloaded = (event: UpdateDownloadedEvent): void => { downloadedFile = event.downloadedFile; };
    this.#updater.on("download-progress", onProgress);
    this.#updater.on("update-downloaded", onDownloaded);
    try {
      const files = await this.#updater.downloadUpdate();
      const file = files[0] ?? downloadedFile;
      if (!file) throw new Error("The native updater did not return the downloaded update path");
      return file;
    } finally {
      this.#updater.removeListener("download-progress", onProgress);
      this.#updater.removeListener("update-downloaded", onDownloaded);
    }
  }

  install(): void {
    this.#updater.quitAndInstall(false, true);
  }
}
