const { app, BrowserWindow } = require("electron");
const { readFileSync, writeFileSync } = require("node:fs");
const { join } = require("node:path");

app.disableHardwareAcceleration();
app.whenReady().then(async () => {
  const root = join(__dirname, "..");
  const svg = readFileSync(join(root, "resources", "icon.svg"), "utf8");
  const window = new BrowserWindow({
    width: 512,
    height: 512,
    show: false,
    transparent: true,
    frame: false,
    webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false },
  });
  const encoded = Buffer.from(svg).toString("base64");
  const loaded = new Promise((resolve, reject) => {
    window.webContents.once("did-finish-load", resolve);
    window.webContents.once("did-fail-load", (_event, code, description) => reject(new Error(`icon page failed to load (${code}): ${description}`)));
  });
  await window.loadURL(`data:text/html,<style>*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:transparent}img{display:block;width:512px;height:512px}</style><img src="data:image/svg+xml;base64,${encoded}">`);
  await loaded;
  const image = await window.webContents.capturePage({ x: 0, y: 0, width: 512, height: 512 });
  writeFileSync(join(root, "resources", "icon.png"), image.toPNG());
  window.destroy();
  app.quit();
}).catch((error) => {
  console.error(error);
  app.exit(1);
});
