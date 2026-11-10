import { app, BrowserWindow, clipboard, dialog, ipcMain, nativeTheme, Notification, session, shell } from "electron";
import { appendFileSync } from "node:fs";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { JsonSettingsStore } from "../core/settings-store.js";
import { createCommandRegistry } from "../core/slash-commands.js";
import { StudioService, type StudioPersistedState, type ThemeMode } from "../core/studio-service.js";
import { STUDIO_EVENT_CHANNEL, STUDIO_INVOKE_CHANNEL, type StudioPushEvent } from "../shared/ipc.js";
import { ElectronUpdateClient } from "./electron-update-client.js";
import { FirekeepClient, defaultFirekeepCommand, isMissingExecutable } from "./firekeep-client.js";
import { loadConfiguredDashboardUrl } from "./firekeep-config.js";
import { LoopbackDecisionBoardClient } from "./decision-board-client.js";
import { StudioDecisionBoardReceiver } from "./decision-board-receiver.js";
import { AttentionNotifier } from "./attention-notifier.js";
import { StudioController } from "./ipc-controller.js";
import { KiroIdeLauncher } from "./kiro-ide.js";
import { removeLegacySecrets } from "./legacy-cleanup.js";
import { ProcessMissionCheckRunner } from "./mission-check-runner.js";
import { createRuntimeRegistry } from "./runtime/index.js";
import { JsonlSessionStore } from "./session-store.js";
import { SessionWorktrees } from "./session-worktrees.js";
import { KeepGateway } from "./keep/keep-gateway.js";
import { McpStdioClient } from "./keep/mcp-stdio-client.js";
import { readReportFile, removeReportFile } from "./keep/report-file.js";
import { ReceiptTracker } from "../core/keep/receipt-tracker.js";
import { detectWorkspaceTestCommand } from "./workspace-detect.js";
import { WorkspaceChangesService } from "./workspace-changes.js";
import { windowThemeColors } from "./window-theme.js";
import { HttpSignedManifestSource, StudioUpdater } from "./studio-updater.js";

const currentDirectory = fileURLToPath(new URL(".", import.meta.url));
let activeController: StudioController | null = null;
let activeDecisionReceiver: StudioDecisionBoardReceiver | null = null;
let activeUpdater: StudioUpdater | null = null;
let activeKeep: KeepGateway | null = null;
let shutdownStarted = false;

function packageSmokeTrace(phase: string): void {
  if (process.env.FIREKEEP_STUDIO_PACKAGE_SMOKE !== "1") return;
  process.stderr.write(`[studio-smoke] ${phase}\n`);
  const path = process.env.FIREKEEP_STUDIO_PACKAGE_SMOKE_LOG;
  if (path) {
    try { appendFileSync(path, `${phase}\n`, { encoding: "utf8" }); }
    catch { /* The smoke test still has stderr and timeout evidence. */ }
  }
}

function createWindow(theme: ThemeMode): BrowserWindow {
  const colors = windowThemeColors(theme, nativeTheme.shouldUseDarkColors);
  const window = new BrowserWindow({
    width: 1540,
    height: 980,
    minWidth: 1000,
    minHeight: 680,
    show: false,
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "hidden",
    ...(process.platform === "darwin" ? {} : { titleBarOverlay: { color: colors.overlay, symbolColor: colors.symbols, height: 44 } }),
    backgroundColor: colors.background,
    webPreferences: {
      preload: join(currentDirectory, "../preload/index.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  window.once("ready-to-show", () => window.show());
  window.on("focus", () => attention.focus());
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

function applyWindowTheme(window: BrowserWindow, theme: ThemeMode): void {
  const colors = windowThemeColors(theme, nativeTheme.shouldUseDarkColors);
  window.setBackgroundColor(colors.background);
  if (process.platform !== "darwin") window.setTitleBarOverlay({ color: colors.overlay, symbolColor: colors.symbols, height: 44 });
}

function applyThemeToAllWindows(theme: ThemeMode): void {
  for (const window of BrowserWindow.getAllWindows()) applyWindowTheme(window, theme);
}

function focusStudio(): void {
  const window = BrowserWindow.getAllWindows()[0];
  if (!window) return;
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
}

// Notifications are kept referenced until they close; an unreferenced one can be collected before its click fires.
const liveNotifications = new Set<Notification>();

const attention = new AttentionNotifier({
  enabled: () => activeController?.service.snapshot().notifications !== false,
  focused: () => BrowserWindow.getFocusedWindow() !== null,
  show: (notice) => {
    if (!Notification.isSupported()) return;
    const notification = new Notification({ title: notice.title, body: notice.body });
    liveNotifications.add(notification);
    notification.on("click", () => { liveNotifications.delete(notification); focusStudio(); });
    notification.on("close", () => liveNotifications.delete(notification));
    notification.on("failed", () => liveNotifications.delete(notification));
    notification.show();
  },
  setPending: (count) => {
    if (process.platform !== "win32") app.setBadgeCount(count);
    for (const window of BrowserWindow.getAllWindows()) window.flashFrame(count > 0);
  },
});

function broadcast(event: StudioPushEvent): void {
  for (const window of BrowserWindow.getAllWindows()) window.webContents.send(STUDIO_EVENT_CHANNEL, event);
  try {
    if (event.type === "runtime.event") attention.event(event.event);
    else if (event.type === "snapshot") attention.snapshot(event.snapshot);
    else if (event.type === "decision.available") attention.decision(event.board);
  } catch (error) {
    console.warn("Studio notification failed", error instanceof Error ? error.message : String(error));
  }
}

async function createController(): Promise<StudioController> {
  packageSmokeTrace("creating controller");
  const userData = app.getPath("userData");
  await removeLegacySecrets(userData);   // 0.4's encrypted API keys; 0.5 stores no secrets
  const warning = (message: string, error: unknown): void => console.warn(message, error instanceof Error ? error.message : String(error));
  const decisionBoards = new LoopbackDecisionBoardClient();
  const receiver = new StudioDecisionBoardReceiver(async (url) => {
    broadcast({ type: "decision.available", board: await decisionBoards.load(url) });
  });
  const receiverEnvironment = await receiver.start();
  packageSmokeTrace("decision receiver ready");
  activeDecisionReceiver = receiver;
  Object.assign(process.env, receiverEnvironment);
  const sessionsRoot = join(userData, "sessions");
  const firekeep = new FirekeepClient();
  const keep: KeepGateway = new KeepGateway({
    whoami: async () => {
      try {
        const result = await firekeep.execute("whoami", []);
        return { ok: result.ok, output: result.output, missing: false };
      } catch (error) {
        return { ok: false, output: "", missing: isMissingExecutable(error) };
      }
    },
    connect: async (env) => {
      // onExit needs the client it belongs to. It only fires after initialize, and connect resumes
      // (a microtask) before any child "close" event, so the holder is always set by then.
      const holder: { client: McpStdioClient | null } = { client: null };
      holder.client = await McpStdioClient.connect({
        command: defaultFirekeepCommand(),
        args: ["gateway"],
        env,
        clientVersion: app.getVersion(),
        onExit: () => { if (holder.client) keep.handleExit(holder.client); },
      });
      return holder.client;
    },
    onStatus: (status) => broadcast({ type: "keep-status", status }),
  });
  activeKeep = keep;
  const reportFile = (id: string, role: "primary" | "review"): string =>
    join(sessionsRoot, role === "review" ? `${id}.review.keep` : `${id}.keep`);
  const tracker = new ReceiptTracker({
    source: () => (keep.status().state === "connected" ? keep : null),
    readReport: async (id) => [
      ...await readReportFile(reportFile(id, "primary")),
      ...await readReportFile(reportFile(id, "review")),
    ],
  });
  const service = new StudioService({
    runtimes: createRuntimeRegistry(app.getVersion()),
    settings: new JsonSettingsStore<StudioPersistedState>(join(userData, "settings.json"), { onWarning: warning }),
    sessions: new JsonlSessionStore(sessionsRoot, warning),
    worktrees: new SessionWorktrees(join(userData, "worktrees")),
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
    keep: {
      tracker,
      reportPath: reportFile,
      removeReport: async (id) => {
        await Promise.all([removeReportFile(reportFile(id, "primary")), removeReportFile(reportFile(id, "review"))]);
      },
    },
  });
  await service.initialize();
  packageSmokeTrace("session state ready");
  service.subscribe((event) => {
    broadcast({ type: "runtime.event", event });
    broadcast({ type: "snapshot", snapshot: service.snapshot() });
  });
  // Sessions the human switched away from keep working. Their events never reach the renderer's
  // transcript; they raise notifications, and the moments that change what the rail and the
  // inbox show (a run starting or ending, an approval, a mission step) refresh both.
  const railMoments = new Set(["run.started", "run.completed", "run.failed", "approval.requested", "approval.resolved", "notice"]);
  // Scheduled missions run only while Studio is open; a time that passes while it is closed is noted, not replayed.
  const scheduleTick = setInterval(() => {
    void service.runDueSchedules().catch((error) => warning("A scheduled mission could not start", error));
  }, 30_000);
  scheduleTick.unref();
  service.subscribeBackground((event) => {
    try { if (event) attention.event(event); }
    catch (error) { warning("Studio notification failed", error); }
    if (event && !railMoments.has(event.payload.kind)) return;
    broadcast({ type: "snapshot", snapshot: service.snapshot() });
    void service.listSessions().then((sessions) => {
      attention.sessions(sessions);
      broadcast({ type: "sessions", sessions });
    }).catch((error) => warning("Studio could not list sessions", error));
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
  packageSmokeTrace("creating updater");
  const updates = new StudioUpdater({
    currentVersion: app.getVersion(),
    packaged: app.isPackaged,
    platform: process.platform,
    manifestSource: new HttpSignedManifestSource(),
    nativeUpdater: new ElectronUpdateClient(),
    openExternal: (url) => shell.openExternal(url),
    requestQuit: () => app.quit(),
  });
  updates.subscribe((state) => broadcast({ type: "update", state }));
  activeUpdater = updates;
  packageSmokeTrace("updater ready");
  const commands = createCommandRegistry(service, {
    firekeep,
    kiroIde: new KiroIdeLauncher(),
    login,
    exportSession,
    selectWorkspace,
    updates,
  });
  const dashboardUrl = await loadConfiguredDashboardUrl();
  packageSmokeTrace("controller ready");
  return new StudioController(service, commands, app.getVersion(), (url) => shell.openExternal(url), selectWorkspace, dashboardUrl, decisionBoards, {
    readText: () => clipboard.readText(),
    writeText: (text) => clipboard.writeText(text),
  }, updates, keep, () => detectWorkspaceTestCommand(service.snapshot().workspacePath), new WorkspaceChangesService({
    workspace: () => service.snapshot().workspacePath,
    confirmRevert: async (file) => {
      const untracked = file.status === "untracked" || file.status === "added";
      const result = await dialog.showMessageBox({
        type: "warning",
        title: "Revert file",
        message: untracked ? `Delete ${file.path}?` : `Discard the changes to ${file.path}?`,
        detail: untracked
          ? "This file is not in the last commit. Reverting deletes it from disk and it cannot be recovered from git."
          : "The file returns to its state in the last commit. Uncommitted changes to it are lost.",
        buttons: [untracked ? "Delete file" : "Discard changes", "Cancel"],
        defaultId: 1,
        cancelId: 1,
        noLink: true,
      });
      return result.response === 0;
    },
  }));
}

app.whenReady().then(async () => {
  // Windows attributes toasts to this id; it must match the installer's appId.
  if (process.platform === "win32") app.setAppUserModelId("ai.firekeep.studio");
  packageSmokeTrace("app ready");
  const controller = await createController();
  activeController = controller;
  // Studio needs no device or web permissions; deny every request and check.
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  session.defaultSession.setPermissionCheckHandler(() => false);
  ipcMain.handle(STUDIO_INVOKE_CHANNEL, async (_event, action: unknown) => {
    const result = await controller.invoke(action);
    if (result.type === "bootstrap" || result.type === "state" || result.type === "command") applyThemeToAllWindows(result.snapshot.theme);
    if (result.type === "state" || result.type === "command") broadcast({ type: "snapshot", snapshot: result.snapshot });
    if (result.type === "state" && result.sessions) broadcast({ type: "sessions", sessions: result.sessions });
    return result;
  });
  nativeTheme.on("updated", () => {
    const theme = activeController?.service.snapshot().theme ?? "system";
    if (theme === "system") applyThemeToAllWindows(theme);
  });
  createWindow(controller.service.snapshot().theme);
  packageSmokeTrace("window created");
  activeUpdater?.start();
  activeKeep?.start().catch((error) => console.warn("Keep gateway did not start", error instanceof Error ? error.message : String(error)));
  app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(controller.service.snapshot().theme); });
}).catch((error) => {
  const detail = error instanceof Error ? (error.stack ?? error.message) : String(error);
  packageSmokeTrace(`startup failed: ${detail}`);
  console.error("Firekeep Studio failed to start", error);
  process.stderr.write(`Firekeep Studio failed to start: ${detail}\n`);
  if (process.env.FIREKEEP_STUDIO_PACKAGE_SMOKE !== "1") {
    dialog.showErrorBox("Firekeep Studio could not start", error instanceof Error ? error.message : String(error));
  }
  app.quit();
});

app.on("before-quit", (event) => {
  if (shutdownStarted) return;
  event.preventDefault();
  shutdownStarted = true;
  activeUpdater?.dispose();
  void Promise.all([
    activeController?.service.shutdown() ?? Promise.resolve(),
    activeDecisionReceiver?.close() ?? Promise.resolve(),
    activeKeep?.stop() ?? Promise.resolve(),
  ])
    .catch((error) => console.warn("Studio shutdown did not finish cleanly", error))
    .finally(() => {
      try {
        if (!activeUpdater?.installDownloaded()) app.quit();
      } catch (error) {
        console.warn("Studio update could not start after shutdown", error);
        app.quit();
      }
    });
});

app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
