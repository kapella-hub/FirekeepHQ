import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { access, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve, sep } from "node:path";
import { createServer } from "node:net";

const executable = resolve(packagedExecutable());
await access(executable);
const port = await reservePort();
const userData = await mkdtemp(join(tmpdir(), "firekeep-studio-smoke-"));
const startupTrace = join(userData, "startup.log");
const child = spawn(executable, ["--enable-logging=stderr", `--remote-debugging-port=${port}`, `--user-data-dir=${userData}`], {
  detached: process.platform !== "win32",
  env: { ...process.env, FIREKEEP_STUDIO_PACKAGE_SMOKE: "1", FIREKEEP_STUDIO_PACKAGE_SMOKE_LOG: startupTrace },
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
  let sessionEditor;
  let agentGrid = null;
  let lightTheme;
  let responsiveInspector;
  let commandSurface;
  try {
    layout = await waitForStudioLayout(cdp, 10_000);
    assertStudioLayout(layout);
    await setThemeForSmoke(cdp, "dark");
    const screenshotPath = process.env.FIREKEEP_STUDIO_SMOKE_SCREENSHOT;
    if (screenshotPath) {
      await cdp.send("Runtime.evaluate", {
        expression: `(() => {
          const studio = document.querySelector(".studio");
          const icon = document.querySelector(".brand-icon");
          if (!studio || !icon) return false;
          studio.setAttribute("data-busy", "true");
          if (!icon.querySelector(".brand-activity")) {
            const activity = document.createElement("span");
            activity.className = "brand-activity";
            icon.append(activity);
          }
          return true;
        })()`,
        returnByValue: true,
      });
      await new Promise((resolvePromise) => setTimeout(resolvePromise, 180));
      const capture = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      await writeFile(screenshotPath, Buffer.from(capture.data, "base64"));
    }
    sessionEditor = await openSessionEditorForSmoke(cdp);
    assertSessionEditor(sessionEditor);
    const sessionScreenshotPath = process.env.FIREKEEP_STUDIO_SMOKE_SESSION_SCREENSHOT;
    if (sessionScreenshotPath) {
      const capture = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      await writeFile(sessionScreenshotPath, Buffer.from(capture.data, "base64"));
    }
    await closeSessionEditorForSmoke(cdp);
    const gridScreenshotPath = process.env.FIREKEEP_STUDIO_SMOKE_GRID_SCREENSHOT;
    agentGrid = await openAgentGridForSmoke(cdp);
    assertAgentGrid(agentGrid);
    if (gridScreenshotPath) {
      const capture = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      await writeFile(gridScreenshotPath, Buffer.from(capture.data, "base64"));
    }
    await closeAgentGridForSmoke(cdp);
    lightTheme = await setThemeForSmoke(cdp, "light");
    assertLightTheme(lightTheme);
    const lightScreenshotPath = process.env.FIREKEEP_STUDIO_SMOKE_LIGHT_SCREENSHOT;
    if (lightScreenshotPath) {
      const capture = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      await writeFile(lightScreenshotPath, Buffer.from(capture.data, "base64"));
    }
    responsiveInspector = await inspectResponsiveInspector(cdp);
    assertResponsiveInspector(responsiveInspector);
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
  process.stdout.write(`${JSON.stringify({ executable, title: target.title, url: target.url, layout, sessionEditor, agentGrid, lightTheme, responsiveInspector, commandSurface })}\n`);
} finally {
  terminateExactTree(child);
  const temporaryRoot = `${await realpath(tmpdir())}${sep}`.toLowerCase();
  const resolvedUserData = await realpath(userData);
  if (!`${resolvedUserData}${sep}`.toLowerCase().startsWith(temporaryRoot)) {
    throw new Error(`refusing to remove non-temporary smoke profile: ${resolvedUserData}`);
  }
  await rm(resolvedUserData, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
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

async function waitForStudioLayout(cdp, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const result = await cdp.send("Runtime.evaluate", {
      expression: `(() => {
        const rect = (element) => {
          const value = element.getBoundingClientRect();
          return { left: value.left, top: value.top, right: value.right, bottom: value.bottom, width: value.width, height: value.height };
        };
        const inspector = document.querySelector(".inspector-toggle");
        const primary = document.querySelector(".runtime-picker");
        const picker = primary?.querySelector(".runtime-picker-trigger");
        const label = primary?.querySelector(".runtime-picker-copy small");
        const value = primary?.querySelector(".runtime-picker-copy strong");
        const toolbar = document.querySelector(".conversation-toolbar");
        const update = document.querySelector(".studio-update-button");
        const sessionTitle = document.querySelector(".session-title-display > .session-title");
        const sessionCustomize = document.querySelector(".session-title-customize");
        if (!inspector || !primary || !picker || !label || !value || !toolbar || !update || !sessionTitle || !sessionCustomize) return null;
        return {
          viewport: { width: innerWidth, height: innerHeight },
          inspector: rect(inspector),
          primary: rect(primary),
          label: rect(label),
          value: rect(value),
          picker: rect(picker),
          toolbar: rect(toolbar),
          update: rect(update),
          updateLabel: update.getAttribute("aria-label") ?? "",
          sessionTitleParent: sessionTitle.parentElement?.tagName ?? "",
          sessionCustomizeLabel: sessionCustomize.getAttribute("aria-label") ?? "",
        };
      })()`,
      returnByValue: true,
    });
    if (result.result?.value) return result.result.value;
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 100));
  }
  throw new Error("Studio controls did not render in time");
}

function assertStudioLayout(layout) {
  if (layout.viewport.width - layout.inspector.right < 138) {
    throw new Error(`inspector toggle intrudes into Windows caption controls: ${JSON.stringify(layout.inspector)}`);
  }
  if (layout.picker.width < 220 || layout.picker.right > layout.toolbar.right || layout.picker.bottom > layout.toolbar.bottom) {
    throw new Error(`primary runtime picker is clipped: ${JSON.stringify({ picker: layout.picker, toolbar: layout.toolbar })}`);
  }
  if (layout.label.bottom > layout.value.top + 1) {
    throw new Error(`primary runtime label overlaps its value: ${JSON.stringify({ label: layout.label, value: layout.value })}`);
  }
  if (!layout.updateLabel.toLowerCase().includes("studio update") || layout.update.right > layout.inspector.left) {
    throw new Error(`Studio update control is missing or overlaps the inspector toggle: ${JSON.stringify({ update: layout.update, inspector: layout.inspector, label: layout.updateLabel })}`);
  }
  if (layout.sessionTitleParent !== "DIV" || !layout.sessionCustomizeLabel.toLowerCase().includes("customize current session")) {
    throw new Error(`titlebar session editing is not palette-only: ${JSON.stringify({ parent: layout.sessionTitleParent, label: layout.sessionCustomizeLabel })}`);
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

async function openAgentGridForSmoke(cdp) {
  const opened = await cdp.send("Runtime.evaluate", {
    expression: `(() => {
      const button = document.querySelector('[aria-label="Open agent grid"]');
      if (!button) return false;
      button.click();
      return true;
    })()`,
    returnByValue: true,
  });
  if (!opened.result?.value) throw new Error("agent-grid toggle did not render");
  await new Promise((resolvePromise) => setTimeout(resolvePromise, 150));

  for (let index = 0; index < 2; index += 1) {
    const menuOpened = await cdp.send("Runtime.evaluate", {
      expression: `(() => {
        const add = document.querySelector('[aria-label="Add agent pane"]');
        if (!add) return false;
        add.click();
        return true;
      })()`,
      returnByValue: true,
    });
    if (!menuOpened.result?.value) break;
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 80));
    const selected = await cdp.send("Runtime.evaluate", {
      expression: `(() => {
        const option = document.querySelector('.pane-menu [role="menuitem"]');
        if (!option) return false;
        option.click();
        return true;
      })()`,
      returnByValue: true,
    });
    if (!selected.result?.value) break;
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 100));
  }

  const result = await cdp.send("Runtime.evaluate", {
    expression: `(() => {
      const rect = (element) => {
        const value = element.getBoundingClientRect();
        return { left: value.left, top: value.top, right: value.right, bottom: value.bottom, width: value.width, height: value.height };
      };
      const grid = document.querySelector('.agent-grid');
      const panes = [...document.querySelectorAll('.agent-pane')];
      const active = panes.filter((pane) => pane.classList.contains('active'));
      const composer = document.querySelector('.composer-shell textarea');
      if (!grid || !composer) return null;
      return {
        grid: rect(grid),
        panes: panes.map(rect),
        paneCount: panes.length,
        activeCount: active.length,
        placeholder: composer.getAttribute('placeholder') ?? '',
      };
    })()`,
    returnByValue: true,
  });
  if (!result.result?.value) throw new Error("agent grid did not render in time");
  return result.result.value;
}

function assertAgentGrid(agentGrid) {
  if (agentGrid.paneCount !== 3 || agentGrid.activeCount !== 1) {
    throw new Error(`agent grid selection is invalid: ${JSON.stringify(agentGrid)}`);
  }
  if (!agentGrid.placeholder.toLowerCase().includes("pane")) {
    throw new Error(`agent grid composer is not pane-targeted: ${JSON.stringify(agentGrid.placeholder)}`);
  }
  for (const pane of agentGrid.panes) {
    if (pane.width < 240 || pane.height < 160 || pane.left < agentGrid.grid.left || pane.right > agentGrid.grid.right + 1) {
      throw new Error(`agent pane is clipped or unusable: ${JSON.stringify({ pane, grid: agentGrid.grid })}`);
    }
  }
  const firstTop = agentGrid.panes[0].top;
  if (agentGrid.panes.some((pane) => Math.abs(pane.top - firstTop) > 1)) {
    throw new Error(`three agent panes did not share one tmux-style row: ${JSON.stringify(agentGrid.panes)}`);
  }
}

async function closeAgentGridForSmoke(cdp) {
  const result = await cdp.send("Runtime.evaluate", {
    expression: `(() => {
      const button = document.querySelector('[aria-label="Close agent grid"]');
      if (!button) return false;
      button.click();
      return true;
    })()`,
    returnByValue: true,
  });
  if (!result.result?.value) throw new Error("agent grid could not return to the conversation");
}

async function setThemeForSmoke(cdp, desired) {
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const result = await cdp.send("Runtime.evaluate", {
      expression: `(() => {
        const theme = document.documentElement.dataset.theme ?? '';
        if (theme === ${JSON.stringify(desired)}) return { theme, clicked: false };
        const button = document.querySelector('.appearance-button');
        if (!button) return null;
        button.click();
        return { theme, clicked: true };
      })()`,
      returnByValue: true,
    });
    if (!result.result?.value) throw new Error("appearance control did not render");
    if (result.result.value.theme === desired) break;
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 120));
  }
  const result = await cdp.send("Runtime.evaluate", {
    expression: `(() => ({
      theme: document.documentElement.dataset.theme ?? '',
      body: getComputedStyle(document.body).backgroundColor,
      titlebar: getComputedStyle(document.querySelector('.titlebar')).backgroundImage,
      label: document.querySelector('.appearance-button')?.getAttribute('aria-label') ?? '',
    }))()`,
    returnByValue: true,
  });
  return result.result?.value;
}

function assertLightTheme(theme) {
  if (!theme || theme.theme !== "light" || theme.body !== "rgb(241, 243, 239)" || !theme.label.includes("Appearance: Light")) {
    throw new Error(`light appearance did not reach the packaged renderer: ${JSON.stringify(theme)}`);
  }
}

async function inspectResponsiveInspector(cdp) {
  await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1000, height: 760, deviceScaleFactor: 1, mobile: false });
  await new Promise((resolvePromise) => setTimeout(resolvePromise, 240));
  const geometry = await cdp.send("Runtime.evaluate", {
    expression: `(() => {
      const panel = document.querySelector('.inspector');
      const toggle = document.querySelector('.inspector-toggle');
      if (!panel || !toggle) return null;
      const bounds = panel.getBoundingClientRect();
      const style = getComputedStyle(panel);
      toggle.click();
      return { viewport: innerWidth, left: bounds.left, right: bounds.right, width: bounds.width, position: style.position, display: style.display };
    })()`,
    returnByValue: true,
  });
  await new Promise((resolvePromise) => setTimeout(resolvePromise, 100));
  const hidden = await cdp.send("Runtime.evaluate", {
    expression: `(() => {
      const hidden = !document.querySelector('.inspector');
      document.querySelector('.inspector-toggle')?.click();
      return hidden;
    })()`,
    returnByValue: true,
  });
  await new Promise((resolvePromise) => setTimeout(resolvePromise, 100));
  await cdp.send("Emulation.clearDeviceMetricsOverride");
  return geometry.result?.value ? { ...geometry.result.value, hidden: Boolean(hidden.result?.value) } : null;
}

function assertResponsiveInspector(value) {
  if (!value || value.position !== "fixed" || value.display === "none" || !value.hidden || Math.abs(value.right - value.viewport) > 1 || value.width < 280) {
    throw new Error(`responsive inspector is clipped or cannot be toggled: ${JSON.stringify(value)}`);
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
