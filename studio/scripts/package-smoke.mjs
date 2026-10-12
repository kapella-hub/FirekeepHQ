import { spawn, spawnSync } from "node:child_process";
import { existsSync, readdirSync } from "node:fs";
import { access, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { delimiter, join, resolve, sep } from "node:path";
import { createServer } from "node:net";

const executable = resolve(packagedExecutable());
await access(executable);
const port = await reservePort();
const userData = await mkdtemp(join(tmpdir(), "firekeep-studio-smoke-"));
const startupTrace = join(userData, "startup.log");
// Studio finds the Client Kit through USERPROFILE/.firekeep/current, firekeep(.exe) on PATH and
// homedir()/.firekeep/config (or FIREKEEP_CONFIG). This smoke must see the kit-not-installed state
// on any machine, so every one of those is isolated for the packaged process.
const kitlessHome = join(userData, "kitless-home");
await mkdir(kitlessHome, { recursive: true });
const child = spawn(executable, ["--enable-logging=stderr", `--remote-debugging-port=${port}`, `--user-data-dir=${userData}`], {
  detached: process.platform !== "win32",
  env: kitlessEnv({ ...process.env, FIREKEEP_STUDIO_PACKAGE_SMOKE: "1", FIREKEEP_STUDIO_PACKAGE_SMOKE_LOG: startupTrace }, kitlessHome),
  stdio: ["ignore", "pipe", "pipe"],
  windowsHide: true,
});
const childOutput = boundedChildOutput(child);

try {
  let target;
  try {
    target = await waitForPage(port, 45_000);
  } catch (error) {
    const details = childOutput().trim();
    let trace = "";
    try { trace = (await readFile(startupTrace, "utf8")).trim(); } catch { /* The app did not reach its main module. */ }
    throw new Error(`${error instanceof Error ? error.message : String(error)}${trace ? `\nStartup trace:\n${trace}` : ""}${details ? `\nPackaged process output:\n${details}` : ""}`);
  }
  if (target.title !== "Firekeep Studio") throw new Error(`unexpected packaged title: ${target.title}`);
  if (!target.url.startsWith("file:") || !target.url.includes("app.asar")) {
    throw new Error(`packaged renderer did not load from app.asar: ${target.url}`);
  }
  const cdp = await connectCdp(target.webSocketDebuggerUrl);
  let layout;
  let startCard;
  let keepPanel;
  let settings;
  let palette;
  let sessionEditor;
  let lightTheme;
  let drawer;
  let commandSurface;
  try {
    await cdp.send("Page.bringToFront");
    await setViewport(cdp, 1440, 900);
    layout = await waitForStudioLayout(cdp, 10_000);
    assertStudioLayout(layout);
    startCard = await waitForStartCard(cdp, 10_000);
    assertStartCard(startCard);
    keepPanel = await inspectKeepPanel(cdp, layout);
    assertKeepPanel(keepPanel);
    settings = await inspectSettings(cdp);
    assertSettings(settings);
    await setThemeForSmoke(cdp, "dark");
    const screenshotPath = process.env.FIREKEEP_STUDIO_SMOKE_SCREENSHOT;
    if (screenshotPath) {
      const capture = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      await writeFile(screenshotPath, Buffer.from(capture.data, "base64"));
    }
    palette = await inspectPalette(cdp);
    assertPalette(palette);
    sessionEditor = await openSessionEditorForSmoke(cdp);
    assertSessionEditor(sessionEditor);
    const sessionScreenshotPath = process.env.FIREKEEP_STUDIO_SMOKE_SESSION_SCREENSHOT;
    if (sessionScreenshotPath) {
      const capture = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      await writeFile(sessionScreenshotPath, Buffer.from(capture.data, "base64"));
    }
    await closeSessionEditorForSmoke(cdp);
    lightTheme = await setThemeForSmoke(cdp, "light");
    assertLightTheme(lightTheme);
    const lightScreenshotPath = process.env.FIREKEEP_STUDIO_SMOKE_LIGHT_SCREENSHOT;
    if (lightScreenshotPath) {
      const capture = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      await writeFile(lightScreenshotPath, Buffer.from(capture.data, "base64"));
    }
    drawer = await inspectResponsiveDrawer(cdp);
    assertResponsiveDrawer(drawer);
    await setThemeForSmoke(cdp, "dark");
    commandSurface = await runCommandForSmoke(cdp);
    assertCommandSurface(commandSurface);
    const commandScreenshotPath = process.env.FIREKEEP_STUDIO_SMOKE_COMMAND_SCREENSHOT;
    if (commandScreenshotPath) {
      const capture = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      await writeFile(commandScreenshotPath, Buffer.from(capture.data, "base64"));
    }
  } finally {
    await cdp.close();
  }
  process.stdout.write(`${JSON.stringify({ executable, title: target.title, url: target.url, layout, startCard, keepPanel, settings, palette, sessionEditor, lightTheme, drawer, commandSurface })}\n`);
} finally {
  terminateExactTree(child);
  const temporaryRoot = `${await realpath(tmpdir())}${sep}`.toLowerCase();
  const resolvedUserData = await realpath(userData);
  if (!`${resolvedUserData}${sep}`.toLowerCase().startsWith(temporaryRoot)) {
    throw new Error(`refusing to remove non-temporary smoke profile: ${resolvedUserData}`);
  }
  await rm(resolvedUserData, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}

function kitlessEnv(base, home) {
  const env = { ...base };
  for (const key of Object.keys(env)) if (/^FIREKEEP_CONFIG$/i.test(key)) delete env[key];
  env.USERPROFILE = home;
  env.HOME = home;
  delete env.HOMEDRIVE;
  delete env.HOMEPATH;
  const pathKey = Object.keys(env).find((key) => key.toLowerCase() === "path") ?? "PATH";
  const hasKit = (directory) => {
    if (/[\\/]\.firekeep([\\/]|$)/i.test(directory)) return true;
    try { return readdirSync(directory).some((name) => /^firekeep(\.(exe|cmd|bat|ps1))?$/i.test(name)); }
    catch { return false; }
  };
  env[pathKey] = (env[pathKey] ?? "").split(delimiter).filter((directory) => directory && !hasKit(directory)).join(delimiter);
  return env;
}

function boundedChildOutput(childProcess, limit = 32_000) {
  let output = "";
  const append = (label, chunk) => {
    output += `${label}${String(chunk)}`;
    if (output.length > limit) output = output.slice(-limit);
  };
  childProcess.stdout?.on("data", (chunk) => append("stdout: ", chunk));
  childProcess.stderr?.on("data", (chunk) => append("stderr: ", chunk));
  return () => output;
}

function packagedExecutable() {
  if (process.platform === "win32") return join("release", "win-unpacked", "Firekeep Studio.exe");
  if (process.platform === "darwin") {
    const candidates = [join("release", "mac-universal", "Firekeep Studio.app", "Contents", "MacOS", "Firekeep Studio"), join("release", "mac", "Firekeep Studio.app", "Contents", "MacOS", "Firekeep Studio")];
    return candidates.find(existsSync) ?? candidates[0];
  }
  return join("release", "linux-unpacked", "firekeep-studio");
}

async function reservePort() {
  const server = createServer();
  await new Promise((resolvePromise, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolvePromise);
  });
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : null;
  await new Promise((resolvePromise) => server.close(resolvePromise));
  if (!port) throw new Error("could not reserve a loopback debug port");
  return port;
}

async function waitForPage(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let lastError = "no page target";
  while (Date.now() < deadline) {
    try {
      const attemptTimeout = Math.max(1, Math.min(1_000, deadline - Date.now()));
      const response = await fetch(`http://127.0.0.1:${port}/json/list`, { signal: AbortSignal.timeout(attemptTimeout) });
      if (response.ok) {
        const targets = await response.json();
        const page = Array.isArray(targets) ? targets.find((target) => target?.type === "page") : null;
        if (page && typeof page.title === "string" && typeof page.url === "string") {
          if (page.title === "Firekeep Studio" && page.url !== "about:blank") return page;
          lastError = `page target not ready (title=${JSON.stringify(page.title)}, url=${JSON.stringify(page.url)})`;
        }
      }
      lastError = `debug endpoint returned ${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 250));
  }
  throw new Error(`packaged renderer did not appear within ${timeoutMs}ms: ${lastError}`);
}

async function connectCdp(url) {
  if (typeof url !== "string" || !url.startsWith("ws://127.0.0.1:")) {
    throw new Error(`unexpected DevTools socket: ${url}`);
  }
  const socket = new WebSocket(url);
  await new Promise((resolvePromise, reject) => {
    const timeout = setTimeout(() => reject(new Error("DevTools socket did not open")), 5_000);
    socket.addEventListener("open", () => { clearTimeout(timeout); resolvePromise(); }, { once: true });
    socket.addEventListener("error", () => { clearTimeout(timeout); reject(new Error("DevTools socket failed")); }, { once: true });
  });
  let nextId = 0;
  const pending = new Map();
  const rejectPending = (reason) => {
    for (const { reject, timeout } of pending.values()) {
      clearTimeout(timeout);
      reject(reason);
    }
    pending.clear();
  };
  socket.addEventListener("close", () => rejectPending(new Error("DevTools socket closed")));
  socket.addEventListener("error", () => rejectPending(new Error("DevTools socket failed")));
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(String(event.data));
    if (!message.id || !pending.has(message.id)) return;
    const { resolve: resolvePromise, reject, timeout } = pending.get(message.id);
    pending.delete(message.id);
    clearTimeout(timeout);
    if (message.error) reject(new Error(message.error.message));
    else resolvePromise(message.result ?? {});
  });
  return {
    send(method, params = {}, timeoutMs = 5_000) {
      const id = ++nextId;
      return new Promise((resolvePromise, reject) => {
        if (socket.readyState !== WebSocket.OPEN) {
          reject(new Error(`DevTools socket is not open for ${method}`));
          return;
        }
        const timeout = setTimeout(() => {
          pending.delete(id);
          reject(new Error(`DevTools command timed out after ${timeoutMs}ms: ${method}`));
        }, timeoutMs);
        pending.set(id, { resolve: resolvePromise, reject, timeout });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
    async close() {
      if (socket.readyState >= WebSocket.CLOSING) return;
      await new Promise((resolvePromise) => {
        const timeout = setTimeout(resolvePromise, 2_000);
        socket.addEventListener("close", () => { clearTimeout(timeout); resolvePromise(); }, { once: true });
        socket.close();
      });
    },
  };
}

function rectSource() {
  return `(element) => {
  const value = element.getBoundingClientRect();
  return { left: value.left, top: value.top, right: value.right, bottom: value.bottom, width: value.width, height: value.height };
}`;
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(`page evaluation failed: ${result.exceptionDetails.text} ${result.exceptionDetails.exception?.description ?? ""}`);
  return result.result?.value;
}

function sleep(ms) { return new Promise((resolvePromise) => setTimeout(resolvePromise, ms)); }

async function setViewport(cdp, width, height) {
  await cdp.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
  await sleep(240);
}

async function pressKey(cdp, { key, code, keyCode, ctrl = false }) {
  const base = { key, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode, modifiers: ctrl ? 2 : 0 };
  await cdp.send("Input.dispatchKeyEvent", { type: "rawKeyDown", ...base });
  await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
  await sleep(200);
}

function pressCtrlBackslash(cdp) { return pressKey(cdp, { key: "\\", code: "Backslash", keyCode: 220, ctrl: true }); }
function pressCtrlK(cdp) { return pressKey(cdp, { key: "k", code: "KeyK", keyCode: 75, ctrl: true }); }
function pressEscape(cdp) { return pressKey(cdp, { key: "Escape", code: "Escape", keyCode: 27 }); }

async function waitForStudioLayout(cdp, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await evaluate(cdp, `(() => {
      const rect = ${rectSource()};
      const titlebar = document.querySelector(".titlebar");
      const rail = document.querySelector(".session-rail");
      const conversation = document.querySelector(".conversation");
      const keep = document.querySelector(".keep-panel");
      const settings = document.querySelector('.titlebar-actions button[aria-label="Settings"]');
      const keepToggle = document.querySelector('.titlebar-actions button[aria-label="Hide the Keep"], .titlebar-actions button[aria-label="Show the Keep"]');
      const agent = [...document.querySelectorAll("button.agent-chip")].find((button) => (button.getAttribute("aria-label") ?? "").startsWith("Agent:"));
      if (!titlebar || !rail || !conversation || !keep || !settings || !keepToggle) return null;
      return {
        viewport: { width: innerWidth, height: innerHeight },
        titlebar: rect(titlebar),
        rail: rect(rail),
        conversation: rect(conversation),
        keep: rect(keep),
        settings: rect(settings),
        keepToggle: rect(keepToggle),
        keepToggleLabel: keepToggle.getAttribute("aria-label"),
        agentChip: agent ? { ...rect(agent), label: agent.getAttribute("aria-label") } : null,
      };
    })()`);
    if (value) return value;
    await sleep(100);
  }
  throw new Error("Studio controls did not render in time");
}

function assertStudioLayout(layout) {
  const hidden = ["titlebar", "rail", "conversation", "keep"].filter((name) => layout[name].width <= 0 || layout[name].height <= 0);
  if (hidden.length) throw new Error(`0.5 shell regions are not visible: ${hidden.join(", ")} ${JSON.stringify(layout)}`);
  if (layout.keep.left < layout.conversation.right - 1 || layout.keep.width < 280) {
    throw new Error(`keep-panel is not a right-hand column of at least 280px: ${JSON.stringify({ keep: layout.keep, conversation: layout.conversation })}`);
  }
  if (layout.viewport.width - layout.settings.right < 138 || layout.viewport.width - layout.keepToggle.right < 138) {
    throw new Error(`titlebar controls intrude into Windows caption controls: ${JSON.stringify({ settings: layout.settings, keepToggle: layout.keepToggle })}`);
  }
  if (layout.keepToggleLabel !== "Hide the Keep") throw new Error(`Keep toggle should read "Hide the Keep" while open: ${layout.keepToggleLabel}`);
  if (layout.agentChip && (layout.agentChip.right > layout.conversation.right || layout.agentChip.bottom > layout.viewport.height || layout.agentChip.width < 40)) {
    throw new Error(`Agent chip is clipped: ${JSON.stringify(layout.agentChip)}`);
  }
}

async function waitForStartCard(cdp, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await evaluate(cdp, `(() => {
      const card = document.querySelector(".start-card");
      if (!card) return null;
      const bounds = card.getBoundingClientRect();
      return { visible: bounds.width > 0 && bounds.height > 0, width: bounds.width, heading: card.querySelector("h2")?.textContent?.trim() ?? "" };
    })()`);
    if (value) return value;
    await sleep(100);
  }
  throw new Error("start-card did not render in an empty session");
}

function assertStartCard(card) {
  if (!card.visible || card.heading !== "Start a session") throw new Error(`start-card is not visible in the empty session: ${JSON.stringify(card)}`);
}

async function inspectKeepPanel(cdp, layout) {
  // Kit-not-installed state: PATH and the home directory are isolated for the packaged process.
  let heading = "";
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    heading = (await evaluate(cdp, `document.querySelector(".keep-panel .keep-state-card h3")?.textContent?.trim() ?? ""`)) ?? "";
    if (heading) break;
    await sleep(150);
  }
  const conversationBefore = await evaluate(cdp, `document.querySelector(".conversation")?.getBoundingClientRect().width ?? 0`);
  await pressCtrlBackslash(cdp);
  const collapsed = await evaluate(cdp, `(() => ({
    panelGone: !document.querySelector(".keep-panel"),
    conversationWidth: document.querySelector(".conversation")?.getBoundingClientRect().width ?? 0,
    toggleLabel: document.querySelector('.titlebar-actions button[aria-label="Show the Keep"]') ? "Show the Keep" : "",
  }))()`);
  await pressCtrlBackslash(cdp);
  const restored = await evaluate(cdp, `(() => ({
    panelBack: Boolean(document.querySelector(".keep-panel")),
    toggleLabel: document.querySelector('.titlebar-actions button[aria-label="Hide the Keep"]') ? "Hide the Keep" : "",
  }))()`);
  return {
    heading,
    panelWidth: layout.keep.width,
    conversationBefore,
    conversationCollapsed: collapsed.conversationWidth,
    growth: collapsed.conversationWidth - conversationBefore,
    panelGoneOnCollapse: collapsed.panelGone,
    collapsedToggleLabel: collapsed.toggleLabel,
    panelBackOnRestore: restored.panelBack,
    restoredToggleLabel: restored.toggleLabel,
  };
}

function assertKeepPanel(value) {
  if (value.heading !== "Connect Studio to your team's Keep") {
    throw new Error(`keep-panel did not show the kit-not-installed state: ${JSON.stringify(value)}`);
  }
  if (!value.panelGoneOnCollapse || value.growth < 250 || value.collapsedToggleLabel !== "Show the Keep") {
    throw new Error(`Ctrl+\\ did not collapse the keep-panel and widen the conversation by 250px: ${JSON.stringify(value)}`);
  }
  if (!value.panelBackOnRestore || value.restoredToggleLabel !== "Hide the Keep") {
    throw new Error(`Ctrl+\\ did not restore the keep-panel: ${JSON.stringify(value)}`);
  }
}

async function inspectPalette(cdp) {
  await pressCtrlK(cdp);
  const opened = await evaluate(cdp, `(() => {
    const dialog = document.querySelector('[role="dialog"][aria-label="Command palette"]');
    if (!dialog) return null;
    const bounds = dialog.getBoundingClientRect();
    return { width: bounds.width, height: bounds.height };
  })()`);
  await pressEscape(cdp);
  const closed = await evaluate(cdp, `!document.querySelector('[role="dialog"][aria-label="Command palette"]')`);
  return { opened: Boolean(opened && opened.width > 0 && opened.height > 0), size: opened, closedOnEscape: Boolean(closed) };
}

function assertPalette(value) {
  if (!value.opened || !value.closedOnEscape) throw new Error(`Command palette (Ctrl+K / Escape) misbehaved: ${JSON.stringify(value)}`);
}

async function openSettings(cdp) {
  const clicked = await evaluate(cdp, `(() => {
    const button = document.querySelector('.titlebar-actions button[aria-label="Settings"]');
    if (!button) return false;
    button.click();
    return true;
  })()`);
  if (!clicked) throw new Error("top-bar Settings button did not render");
  await sleep(250);
}

async function clickSettingsTab(cdp, label) {
  const clicked = await evaluate(cdp, `(() => {
    const tab = [...document.querySelectorAll('[role="dialog"][aria-label="Settings"] [role="tab"]')].find((button) => button.textContent?.trim() === ${JSON.stringify(label)});
    if (!tab) return false;
    tab.click();
    return true;
  })()`);
  if (!clicked) throw new Error(`Settings tab did not render: ${label}`);
  await sleep(150);
}

async function inspectSettings(cdp) {
  await openSettings(cdp);
  await clickSettingsTab(cdp, "Agents & accounts");
  const agents = await evaluate(cdp, `(() => ({
    dialog: Boolean(document.querySelector('[role="dialog"][aria-label="Settings"]')),
    region: Boolean(document.querySelector('[role="region"][aria-label="Agents & accounts"]')),
    runtimeCards: document.querySelectorAll('[role="region"][aria-label="Agents & accounts"] .runtime-manager-grid > *').length,
  }))()`);
  await pressEscape(cdp);
  const closed = await evaluate(cdp, `!document.querySelector('[role="dialog"][aria-label="Settings"]')`);
  return { ...agents, closedOnEscape: Boolean(closed) };
}

function assertSettings(value) {
  if (!value.dialog || !value.region || value.runtimeCards < 1 || !value.closedOnEscape) {
    throw new Error(`Settings (Agents & accounts) is incomplete: ${JSON.stringify(value)}`);
  }
}

async function openSessionEditorForSmoke(cdp) {
  const rowClicked = await cdp.send("Runtime.evaluate", {
    expression: `(() => {
      const row = document.querySelector('.session-row');
      if (!row) return false;
      row.click();
      return true;
    })()`,
    returnByValue: true,
  });
  if (!rowClicked.result?.value) throw new Error("session row did not render");
  await new Promise((resolvePromise) => setTimeout(resolvePromise, 100));
  const rowOpenedEditor = await cdp.send("Runtime.evaluate", {
    expression: "Boolean(document.querySelector('.session-editor'))",
    returnByValue: true,
  });
  if (rowOpenedEditor.result?.value) throw new Error("session row unexpectedly opened the editor");

  const opened = await cdp.send("Runtime.evaluate", {
    expression: `(() => {
      const button = document.querySelector('.session-customize');
      if (!button) return false;
      button.click();
      return true;
    })()`,
    returnByValue: true,
  });
  if (!opened.result?.value) throw new Error("session customizer did not render");
  await new Promise((resolvePromise) => setTimeout(resolvePromise, 100));
  const result = await cdp.send("Runtime.evaluate", {
    expression: `(() => {
      const rect = (element) => {
        const value = element.getBoundingClientRect();
        return { left: value.left, top: value.top, right: value.right, bottom: value.bottom, width: value.width, height: value.height };
      };
      const rail = document.querySelector('.session-rail');
      const editor = document.querySelector('.session-editor');
      const input = editor?.querySelector('input[aria-label="Session name"]');
      const save = editor?.querySelector('[aria-label="Save session"]');
      const colors = editor?.querySelectorAll('[role="radio"]');
      if (!rail || !editor || !input || !save || !colors) return null;
      return { rail: rect(rail), editor: rect(editor), input: rect(input), colorCount: colors.length, saveVisible: rect(save).height > 0 };
    })()`,
    returnByValue: true,
  });
  if (!result.result?.value) throw new Error("session editor did not open");
  return result.result.value;
}

function assertSessionEditor(editor) {
  if (editor.colorCount !== 8 || !editor.saveVisible || editor.input.width < 150) {
    throw new Error(`session editor controls are incomplete: ${JSON.stringify(editor)}`);
  }
  if (editor.editor.left < editor.rail.left || editor.editor.right > editor.rail.right + 1 || editor.editor.bottom > editor.rail.bottom) {
    throw new Error(`session editor is clipped by the rail: ${JSON.stringify(editor)}`);
  }
}

async function closeSessionEditorForSmoke(cdp) {
  const closed = await cdp.send("Runtime.evaluate", {
    expression: `(() => {
      const cancel = [...document.querySelectorAll('.session-editor button')].find((button) => button.textContent?.trim() === 'Cancel');
      if (!cancel) return false;
      cancel.click();
      return true;
    })()`,
    returnByValue: true,
  });
  if (!closed.result?.value) throw new Error("session editor could not close");
}

async function setThemeForSmoke(cdp, desired) {
  const current = await evaluate(cdp, `document.documentElement.dataset.theme ?? ''`);
  if (current !== desired) {
    await openSettings(cdp);
    await clickSettingsTab(cdp, "Appearance");
    const picked = await evaluate(cdp, `(() => {
      const radio = document.querySelector('[role="dialog"][aria-label="Settings"] input[name="studio-theme"][value=${JSON.stringify(desired)}]');
      if (!radio) return false;
      radio.click();
      return true;
    })()`);
    if (!picked) throw new Error(`theme radio did not render: ${desired}`);
    await sleep(200);
    await pressEscape(cdp);
  }
  return evaluate(cdp, `(() => ({
    theme: document.documentElement.dataset.theme ?? '',
    body: getComputedStyle(document.body).backgroundColor,
    titlebar: getComputedStyle(document.querySelector('.titlebar')).backgroundImage,
    settingsClosed: !document.querySelector('[role="dialog"][aria-label="Settings"]'),
  }))()`);
}

function assertLightTheme(theme) {
  if (!theme || theme.theme !== "light" || theme.body !== "rgb(241, 243, 239)" || !theme.settingsClosed) {
    throw new Error(`light appearance did not reach the packaged renderer: ${JSON.stringify(theme)}`);
  }
}

async function inspectResponsiveDrawer(cdp) {
  await setViewport(cdp, 1000, 760);
  const geometry = await evaluate(cdp, `(() => {
    const panel = document.querySelector('.keep-panel');
    if (!panel) return null;
    const bounds = panel.getBoundingClientRect();
    const style = getComputedStyle(panel);
    return { viewport: innerWidth, left: bounds.left, right: bounds.right, width: bounds.width, position: style.position, display: style.display };
  })()`);
  await pressCtrlBackslash(cdp);
  const hidden = await evaluate(cdp, `!document.querySelector('.keep-panel')`);
  await pressCtrlBackslash(cdp);
  const restored = await evaluate(cdp, `Boolean(document.querySelector('.keep-panel'))`);
  await cdp.send("Emulation.clearDeviceMetricsOverride");
  await sleep(200);
  return geometry ? { ...geometry, hidden: Boolean(hidden), restored: Boolean(restored) } : null;
}

function assertResponsiveDrawer(value) {
  if (!value || value.position !== "fixed" || value.display === "none" || !value.hidden || !value.restored || Math.abs(value.right - value.viewport) > 1 || value.width < 280) {
    throw new Error(`responsive keep-panel drawer is clipped or cannot be toggled: ${JSON.stringify(value)}`);
  }
}

async function runCommandForSmoke(cdp) {
  const submitted = await cdp.send("Runtime.evaluate", {
    expression: `(() => {
      const textarea = document.querySelector('.composer-shell textarea');
      if (!textarea) return false;
      const setValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
      setValue?.call(textarea, '/help');
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    })()`,
    returnByValue: true,
  });
  if (!submitted.result?.value) throw new Error("composer did not render for command smoke");
  await new Promise((resolvePromise) => setTimeout(resolvePromise, 80));
  await cdp.send("Runtime.evaluate", {
    expression: `(() => {
      const send = document.querySelector('.send-button');
      if (!send || send.disabled) return false;
      send.click();
      return true;
    })()`,
    returnByValue: true,
  });
  const deadline = Date.now() + 5_000;
  while (Date.now() < deadline) {
    const result = await cdp.send("Runtime.evaluate", {
      expression: `(() => {
        const card = document.querySelector('.command-card');
        const transcript = document.querySelector('.transcript');
        if (!card || !transcript) return null;
        const bounds = card.getBoundingClientRect();
        return {
          title: card.querySelector('header span')?.textContent?.trim() ?? '',
          rowCount: card.querySelectorAll('tbody tr').length,
          contentLength: card.textContent?.trim().length ?? 0,
          width: bounds.width,
          visible: bounds.height > 0,
          transcriptWidth: transcript.getBoundingClientRect().width,
        };
      })()`,
      returnByValue: true,
    });
    if (result.result?.value) return result.result.value;
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 100));
  }
  throw new Error("/help did not render a packaged command card");
}

function assertCommandSurface(value) {
  if (!value || !value.visible || value.title !== "Firekeep Studio commands" || value.contentLength < 200 || value.width < 500 || value.width > value.transcriptWidth) {
    throw new Error(`packaged command surface is incomplete or clipped: ${JSON.stringify(value)}`);
  }
}

function terminateExactTree(childProcess) {
  const pid = childProcess.pid;
  if (!pid) return;
  try {
    if (process.platform === "win32") {
      spawnSync("taskkill.exe", ["/pid", String(pid), "/t", "/f"], { stdio: "ignore", windowsHide: true });
    } else {
      // This is a disposable, detached smoke-only process group. Electron may
      // handle SIGTERM without exiting, which leaves captured pipes alive.
      process.kill(-pid, "SIGKILL");
    }
  } catch {
    try { childProcess.kill("SIGKILL"); }
    catch { /* The exact packaged process already exited. */ }
  } finally {
    childProcess.stdout?.destroy();
    childProcess.stderr?.destroy();
    childProcess.unref();
  }
}
