import { app, BrowserWindow, clipboard, dialog, ipcMain, session, shell } from "electron";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { JsonSettingsStore } from "../core/settings-store.js";
import { createCommandRegistry } from "../core/slash-commands.js";
import { StudioService, type StudioPersistedState } from "../core/studio-service.js";
import { STUDIO_EVENT_CHANNEL, STUDIO_INVOKE_CHANNEL, type StudioPushEvent } from "../shared/ipc.js";
import { ElectronSecretStore } from "./electron-secret-store.js";
import { FirekeepClient } from "./firekeep-client.js";
import { loadConfiguredDashboardUrl } from "./firekeep-config.js";
import { LoopbackDecisionBoardClient } from "./decision-board-client.js";
import { StudioDecisionBoardReceiver } from "./decision-board-receiver.js";
import { StudioController } from "./ipc-controller.js";
import { KiroIdeLauncher } from "./kiro-ide.js";
import { ProcessMissionCheckRunner } from "./mission-check-runner.js";
import { allowsMicrophoneCheck, allowsMicrophoneRequest } from "./permissions.js";
import { createRuntimeRegistry } from "./runtime/index.js";
import { JsonlSessionStore } from "./session-store.js";

const currentDirectory = fileURLToPath(new URL(".", import.meta.url));
let activeController: StudioController | null = null;
let activeDecisionReceiver: StudioDecisionBoardReceiver | null = null;
let shutdownStarted = false;

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1540,
    height: 980,
    minWidth: 1000,
    minHeight: 680,
    show: false,
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "hidden",
    ...(process.platform === "darwin" ? {} : { titleBarOverlay: { color: "#101311", symbolColor: "#d6ddd7", height: 42 } }),
    backgroundColor: "#0d100e",
    webPreferences: {
      preload: join(currentDirectory, "../preload/index.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  window.once("ready-to-show", () => window.show());
  window.webContents.setWindowOpenHandler(({ url }) => {
    try {
      const parsed = new URL(url);
      if (parsed.protocol === "https:" || parsed.protocol === "http:") void shell.openExternal(parsed.toString());
    } catch { /* Invalid links stay closed. */ }
    return { action: "deny" };
  });
  const loaded = process.env.ELECTRON_RENDERER_URL
    ? window.loadURL(process.env.ELECTRON_RENDERER_URL)
    : window.loadFile(join(currentDirectory, "../renderer/index.html"));
  void loaded.then(() => {
    // Attach after the trusted renderer loads so the guard cannot block Electron's
    // own initial file:// navigation. All later top-level navigation stays closed.
    window.webContents.on("will-navigate", (event, url) => {
      const current = window.webContents.getURL();
      if (url === current) return;
      event.preventDefault();
      try {
        const parsed = new URL(url);
        if (parsed.protocol === "https:" || parsed.protocol === "http:") void shell.openExternal(parsed.toString());
      } catch { /* Invalid navigation stays blocked. */ }
    });
  }).catch((error) => console.error("Studio renderer failed to load", error));
  return window;
}

function broadcast(event: StudioPushEvent): void {
  for (const window of BrowserWindow.getAllWindows()) window.webContents.send(STUDIO_EVENT_CHANNEL, event);
}

async function createController(): Promise<StudioController> {
  const userData = app.getPath("userData");
  const warning = (message: string, error: unknown): void => console.warn(message, error instanceof Error ? error.message : String(error));
  const secrets = new ElectronSecretStore(join(userData, "secrets.json"), warning);
  const decisionBoards = new LoopbackDecisionBoardClient();
  const receiver = new StudioDecisionBoardReceiver(async (url) => {
    broadcast({ type: "decision.available", board: await decisionBoards.load(url) });
  });
  const receiverEnvironment = await receiver.start();
  activeDecisionReceiver = receiver;
  Object.assign(process.env, receiverEnvironment);
  const service = new StudioService({
    runtimes: createRuntimeRegistry(secrets),
    settings: new JsonSettingsStore<StudioPersistedState>(join(userData, "settings.json"), { onWarning: warning }),
    sessions: new JsonlSessionStore(join(userData, "sessions"), warning),
    cwd: () => app.getPath("home"),
    missionChecks: new ProcessMissionCheckRunner(),
    confirmMission: async (summary) => {
      const result = await dialog.showMessageBox({
        type: "warning",
        title: "Run Firekeep mission",
        message: "Allow this mission to run agents and the listed local verification commands?",
        detail: summary,
        buttons: ["Run mission", "Cancel"],
        defaultId: 1,
        cancelId: 1,
        noLink: true,
      });
      return result.response === 0;
    },
  });
  await service.initialize();
  service.subscribe((event) => {
    broadcast({ type: "runtime.event", event });
    broadcast({ type: "snapshot", snapshot: service.snapshot() });
  });
  const login = async (runtimeId: string, request: Parameters<StudioService["login"]>[1]) => {
    const result = await service.login(runtimeId, request);
    if (result.state === "browser" || result.state === "device") {
      const url = new URL(result.url);
      if (url.protocol !== "https:" && url.protocol !== "http:") throw new Error("provider returned an unsupported login URL");
      await shell.openExternal(url.toString());
    }
    return result;
  };
  const exportSession = async (format: "markdown" | "json", content: string, suggestedName: string) => {
    const extension = format === "markdown" ? "md" : "json";
    const safeName = suggestedName.replace(/[<>:"/\\|?*\x00-\x1F]/g, "-").trim().slice(0, 80) || "Firekeep Studio session";
    const selected = await dialog.showSaveDialog({
      title: "Export Firekeep Studio session",
      defaultPath: `${safeName}.${extension}`,
      filters: [{ name: format === "markdown" ? "Markdown" : "JSON", extensions: [extension] }],
    });
    if (selected.canceled || !selected.filePath) return { saved: false, detail: "No file was written." };
    await writeFile(selected.filePath, content, { encoding: "utf8", mode: 0o600 });
    return { saved: true, detail: `Saved to ${selected.filePath}` };
  };
  const selectWorkspace = async (): Promise<string | null> => {
    const current = service.snapshot().workspacePath;
    const selected = await dialog.showOpenDialog({
      title: "Choose the workspace for your agents",
      defaultPath: current ?? app.getPath("documents"),
      properties: ["openDirectory", "createDirectory"],
    });
    return selected.canceled ? null : selected.filePaths[0] ?? null;
  };
  const commands = createCommandRegistry(service, {
    firekeep: new FirekeepClient(),
    kiroIde: new KiroIdeLauncher(),
    login,
    exportSession,
    selectWorkspace,
  });
  const dashboardUrl = await loadConfiguredDashboardUrl();
  return new StudioController(service, commands, app.getVersion(), (url) => shell.openExternal(url), selectWorkspace, dashboardUrl, decisionBoards, {
    readText: () => clipboard.readText(),
    writeText: (text) => clipboard.writeText(text),
  });
}

app.whenReady().then(async () => {
  const controller = await createController();
  activeController = controller;
  session.defaultSession.setPermissionRequestHandler((webContents, permission, callback, details) => {
    callback(allowsMicrophoneRequest({
      permission,
      attachedToStudioWindow: BrowserWindow.fromWebContents(webContents) !== null,
      isMainFrame: details.isMainFrame,
      ...(permission === "media" && "mediaTypes" in details && details.mediaTypes
        ? { mediaTypes: details.mediaTypes }
        : {}),
    }));
  });
  session.defaultSession.setPermissionCheckHandler((webContents, permission, _origin, details) => allowsMicrophoneCheck({
    permission,
    attachedToStudioWindow: webContents !== null && BrowserWindow.fromWebContents(webContents) !== null,
    isMainFrame: details.isMainFrame,
    ...(details.mediaType ? { mediaType: details.mediaType } : {}),
  }));
  ipcMain.handle(STUDIO_INVOKE_CHANNEL, async (_event, action: unknown) => {
    const result = await controller.dispatch(action);
    if (result.type === "state" || result.type === "command") broadcast({ type: "snapshot", snapshot: result.snapshot });
    if (result.type === "state" && result.sessions) broadcast({ type: "sessions", sessions: result.sessions });
    return result;
  });
  createWindow();
  app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
}).catch((error) => {
  console.error("Firekeep Studio failed to start", error);
  dialog.showErrorBox("Firekeep Studio could not start", error instanceof Error ? error.message : String(error));
  app.quit();
});

app.on("before-quit", (event) => {
  if (shutdownStarted) return;
  event.preventDefault();
  shutdownStarted = true;
  void Promise.all([
    activeController?.service.shutdown() ?? Promise.resolve(),
    activeDecisionReceiver?.close() ?? Promise.resolve(),
  ])
    .catch((error) => console.warn("Studio shutdown did not finish cleanly", error))
    .finally(() => app.quit());
});

app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
