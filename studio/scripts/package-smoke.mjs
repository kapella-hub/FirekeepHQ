import { spawn, spawnSync } from "node:child_process";
import { access, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve, sep } from "node:path";
import { createServer } from "node:net";

const executable = resolve(packagedExecutable());
await access(executable);
const port = await reservePort();
const userData = await mkdtemp(join(tmpdir(), "firekeep-studio-smoke-"));
const child = spawn(executable, [`--remote-debugging-port=${port}`, `--user-data-dir=${userData}`], {
  detached: process.platform !== "win32",
  stdio: "ignore",
  windowsHide: true,
});

try {
  const target = await waitForPage(port, 45_000);
  if (target.title !== "Firekeep Studio") throw new Error(`unexpected packaged title: ${target.title}`);
  if (!target.url.startsWith("file:") || !target.url.includes("app.asar")) {
    throw new Error(`packaged renderer did not load from app.asar: ${target.url}`);
  }
  const cdp = await connectCdp(target.webSocketDebuggerUrl);
  let layout;
  let sessionEditor;
  let agentGrid = null;
  try {
    layout = await waitForStudioLayout(cdp, 10_000);
    assertStudioLayout(layout);
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
    if (gridScreenshotPath) {
      agentGrid = await openAgentGridForSmoke(cdp);
      assertAgentGrid(agentGrid);
      const capture = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      await writeFile(gridScreenshotPath, Buffer.from(capture.data, "base64"));
    }
  } finally {
    await cdp.close();
  }
  process.stdout.write(`${JSON.stringify({ executable, title: target.title, url: target.url, layout, sessionEditor, agentGrid })}\n`);
} finally {
  terminateExactTree(child.pid);
  const temporaryRoot = `${await realpath(tmpdir())}${sep}`.toLowerCase();
  const resolvedUserData = await realpath(userData);
  if (!`${resolvedUserData}${sep}`.toLowerCase().startsWith(temporaryRoot)) {
    throw new Error(`refusing to remove non-temporary smoke profile: ${resolvedUserData}`);
  }
  await rm(resolvedUserData, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}

function packagedExecutable() {
  if (process.platform === "win32") return join("release", "win-unpacked", "Firekeep Studio.exe");
  if (process.platform === "darwin") return join("release", "mac", "Firekeep Studio.app", "Contents", "MacOS", "Firekeep Studio");
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
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
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
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(String(event.data));
    if (!message.id || !pending.has(message.id)) return;
    const { resolve: resolvePromise, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(message.error.message));
    else resolvePromise(message.result ?? {});
  });
  return {
    send(method, params = {}) {
      const id = ++nextId;
      return new Promise((resolvePromise, reject) => {
        pending.set(id, { resolve: resolvePromise, reject });
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
        if (!inspector || !primary || !picker || !label || !value || !toolbar) return null;
        return {
          viewport: { width: innerWidth, height: innerHeight },
          inspector: rect(inspector),
          primary: rect(primary),
          label: rect(label),
          value: rect(value),
          picker: rect(picker),
          toolbar: rect(toolbar),
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
}

async function openSessionEditorForSmoke(cdp) {
  const opened = await cdp.send("Runtime.evaluate", {
    expression: `(() => {
      const button = document.querySelector('.session-row');
      if (!button) return false;
      button.click();
      return true;
    })()`,
    returnByValue: true,
  });
  if (!opened.result?.value) throw new Error("session row did not render");
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
  if (agentGrid.paneCount < 1 || agentGrid.activeCount !== 1) {
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
}

function terminateExactTree(pid) {
  if (!pid) return;
  if (process.platform === "win32") {
    spawnSync("taskkill.exe", ["/pid", String(pid), "/t", "/f"], { stdio: "ignore", windowsHide: true });
    return;
  }
  try { process.kill(-pid, "SIGTERM"); }
  catch { /* The exact packaged process already exited. */ }
}
