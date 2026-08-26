import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import type { StudioUpdateState } from "../shared/ipc.js";
import { PINNED_FIREKEEP_RELEASE_KEY, verifyMinisign } from "./release-signing.js";

export const STUDIO_UPDATE_CHANNEL_URL = "https://github.com/kapella-hub/firekeep-dist/releases/download/studio-latest";

export interface UpdateArtifact {
  readonly fileName: string;
  readonly url: string;
  readonly sha256: string;
  readonly size: number;
}

export interface PlatformUpdate {
  readonly automatic: boolean;
  readonly installer: UpdateArtifact;
  readonly updater: UpdateArtifact;
}

export interface StudioUpdateManifest {
  readonly schema: 1;
  readonly channel: "stable";
  readonly version: string;
  readonly publishedAt: string;
  readonly releaseUrl: string;
  readonly platforms: Readonly<Partial<Record<"win32" | "darwin", PlatformUpdate>>>;
}

export interface SignedManifestSource {
  load(): Promise<{ readonly bytes: Uint8Array; readonly signature: string }>;
}

export interface NativeUpdateClient {
  check(): Promise<{ readonly version: string } | null>;
  download(progress: (percent: number) => void): Promise<string>;
  install(): void;
}

export interface StudioUpdateControl {
  snapshot(): StudioUpdateState;
  check(): Promise<StudioUpdateState>;
  install(): Promise<StudioUpdateState>;
}

interface StudioUpdaterOptions {
  readonly currentVersion: string;
  readonly packaged: boolean;
  readonly platform: NodeJS.Platform;
  readonly manifestSource: SignedManifestSource;
  readonly publicKey?: string;
  readonly nativeUpdater: NativeUpdateClient;
  readonly openExternal: (url: string) => Promise<void>;
  readonly requestQuit: () => void;
}

const VERSION_PATTERN = /^\d+\.\d+\.\d+$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;

function object(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} must be an object`);
  return value as Record<string, unknown>;
}

function textField(value: unknown, label: string, max = 2_048): string {
  if (typeof value !== "string" || !value || value.length > max) throw new Error(`${label} is invalid`);
  return value;
}

function httpsUrl(value: unknown, label: string): string {
  const text = textField(value, label);
  const parsed = new URL(text);
  if (parsed.protocol !== "https:" || parsed.username || parsed.password) throw new Error(`${label} must be a credential-free HTTPS URL`);
  return parsed.toString();
}

function artifact(value: unknown, label: string): UpdateArtifact {
  const item = object(value, label);
  const fileName = textField(item.fileName, `${label}.fileName`, 255);
  if (fileName.includes("/") || fileName.includes("\\") || fileName === "." || fileName === "..") throw new Error(`${label}.fileName must be a base name`);
  const sha256 = textField(item.sha256, `${label}.sha256`, 64).toLowerCase();
  if (!SHA256_PATTERN.test(sha256)) throw new Error(`${label}.sha256 is invalid`);
  if (!Number.isSafeInteger(item.size) || (item.size as number) <= 0 || (item.size as number) > 2_000_000_000) throw new Error(`${label}.size is invalid`);
  return { fileName, url: httpsUrl(item.url, `${label}.url`), sha256, size: item.size as number };
}

function platformUpdate(value: unknown, label: string): PlatformUpdate {
  const item = object(value, label);
  if (typeof item.automatic !== "boolean") throw new Error(`${label}.automatic is invalid`);
  return { automatic: item.automatic, installer: artifact(item.installer, `${label}.installer`), updater: artifact(item.updater, `${label}.updater`) };
}

export function parseStudioUpdateManifest(bytes: Uint8Array): StudioUpdateManifest {
  let parsed: unknown;
  try { parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)); }
  catch { throw new Error("Studio update manifest is not valid UTF-8 JSON"); }
  const item = object(parsed, "Studio update manifest");
  if (item.schema !== 1 || item.channel !== "stable") throw new Error("Studio update manifest has an unsupported schema or channel");
  const version = textField(item.version, "Studio update version", 64);
  if (!VERSION_PATTERN.test(version)) throw new Error("Studio update version is invalid");
  const publishedAt = textField(item.publishedAt, "Studio update publication date", 64);
  if (Number.isNaN(Date.parse(publishedAt))) throw new Error("Studio update publication date is invalid");
  const platformsValue = object(item.platforms, "Studio update platforms");
  const platforms: Partial<Record<"win32" | "darwin", PlatformUpdate>> = {};
  if (platformsValue.win32 !== undefined) platforms.win32 = platformUpdate(platformsValue.win32, "Studio Windows update");
  if (platformsValue.darwin !== undefined) platforms.darwin = platformUpdate(platformsValue.darwin, "Studio macOS update");
  return { schema: 1, channel: "stable", version, publishedAt, releaseUrl: httpsUrl(item.releaseUrl, "Studio release URL"), platforms };
}

function compareVersions(left: string, right: string): number {
  const leftParts = left.split(".").map(Number);
  const rightParts = right.split(".").map(Number);
  if (leftParts.length !== 3 || rightParts.length !== 3 || leftParts.some(Number.isNaN) || rightParts.some(Number.isNaN)) throw new Error("Studio update versions must use major.minor.patch");
  for (let index = 0; index < 3; index += 1) {
    const difference = (leftParts[index] ?? 0) - (rightParts[index] ?? 0);
    if (difference) return difference;
  }
  return 0;
}

async function sha256(path: string): Promise<string> {
  const hash = createHash("sha256");
  await new Promise<void>((resolve, reject) => {
    const stream = createReadStream(path);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.once("error", reject);
    stream.once("end", resolve);
  });
  return hash.digest("hex");
}

async function verifyArtifact(path: string, expected: UpdateArtifact): Promise<void> {
  const details = await stat(path);
  const digest = await sha256(path);
  if (details.size !== expected.size || digest !== expected.sha256) throw new Error("The downloaded update did not match the signed release manifest. Nothing was installed.");
}

function initialState(options: StudioUpdaterOptions): StudioUpdateState {
  if (!options.packaged) return { phase: "disabled", currentVersion: options.currentVersion, availableVersion: null, progressPercent: null, automatic: false, detail: "Update checks are disabled in development builds." };
  if (options.platform !== "win32" && options.platform !== "darwin") return { phase: "disabled", currentVersion: options.currentVersion, availableVersion: null, progressPercent: null, automatic: false, detail: "Studio updates are not published for this platform yet." };
  return { phase: "idle", currentVersion: options.currentVersion, availableVersion: null, progressPercent: null, automatic: true, detail: "Studio checks for verified updates automatically." };
}

export class HttpSignedManifestSource implements SignedManifestSource {
  constructor(readonly channelUrl = STUDIO_UPDATE_CHANNEL_URL) {}

  async load(): Promise<{ readonly bytes: Uint8Array; readonly signature: string }> {
    const nonce = Date.now().toString(36);
    const [bytes, signatureBytes] = await Promise.all([
      this.#read(`${this.channelUrl}/studio-update.json?check=${nonce}`, 256_000),
      this.#read(`${this.channelUrl}/studio-update.json.minisig?check=${nonce}`, 16_000),
    ]);
    return { bytes, signature: new TextDecoder("utf-8", { fatal: true }).decode(signatureBytes) };
  }

  async #read(url: string, limit: number): Promise<Uint8Array> {
    const response = await fetch(url, { cache: "no-store", redirect: "follow", signal: AbortSignal.timeout(15_000) });
    if (!response.ok) throw new Error(`Studio update channel returned HTTP ${response.status}`);
    if (!response.url.startsWith("https://")) throw new Error("Studio update channel redirected outside HTTPS");
    const declared = Number(response.headers.get("content-length") ?? 0);
    if (declared > limit) throw new Error("Studio update channel response is too large");
    if (!response.body) throw new Error("Studio update channel returned an empty response");
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) {
        await reader.cancel();
        throw new Error("Studio update channel response is too large");
      }
      chunks.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return bytes;
  }
}

export class StudioUpdater implements StudioUpdateControl {
  readonly #listeners = new Set<(state: StudioUpdateState) => void>();
  readonly #publicKey: string;
  #state: StudioUpdateState;
  #platformUpdate: PlatformUpdate | null = null;
  #checkPromise: Promise<StudioUpdateState> | null = null;
  #timer: ReturnType<typeof setTimeout> | null = null;

  constructor(readonly options: StudioUpdaterOptions) {
    this.#publicKey = options.publicKey ?? PINNED_FIREKEEP_RELEASE_KEY;
    this.#state = initialState(options);
  }

  snapshot(): StudioUpdateState { return this.#state; }

  subscribe(listener: (state: StudioUpdateState) => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  start(delayMs = 8_000): void {
    if (this.#state.phase === "disabled" || this.#timer) return;
    this.#timer = setTimeout(() => { this.#timer = null; void this.check(); }, delayMs);
    this.#timer.unref?.();
  }

  dispose(): void {
    if (this.#timer) clearTimeout(this.#timer);
    this.#timer = null;
  }

  check(): Promise<StudioUpdateState> {
    if (this.#state.phase === "disabled") return Promise.resolve(this.#state);
    if (this.#state.phase === "ready") return Promise.resolve(this.#state);
    if (this.#checkPromise) return this.#checkPromise;
    this.#checkPromise = this.#performCheck().finally(() => { this.#checkPromise = null; });
    return this.#checkPromise;
  }

  async install(): Promise<StudioUpdateState> {
    if (this.#state.phase === "ready") {
      this.options.requestQuit();
      return this.#state;
    }
    if (this.#state.phase === "available" && this.#platformUpdate && !this.#platformUpdate.automatic) {
      await this.options.openExternal(this.#platformUpdate.installer.url);
      this.#set({ ...this.#state, detail: `Opened the signed release's ${this.#state.availableVersion ?? "latest"} macOS installer download.` });
      return this.#state;
    }
    return this.check();
  }

  installDownloaded(): boolean {
    if (this.#state.phase !== "ready") return false;
    this.options.nativeUpdater.install();
    return true;
  }

  async #performCheck(): Promise<StudioUpdateState> {
    this.#set({ ...this.#state, phase: "checking", progressPercent: null, detail: "Checking the signed Studio release channel…" });
    try {
      const signed = await this.options.manifestSource.load();
      const trustedComment = verifyMinisign(signed.bytes, signed.signature, this.#publicKey);
      const manifest = parseStudioUpdateManifest(signed.bytes);
      const signedVersion = /(?:^|\s)version:(\S+)/.exec(trustedComment)?.[1];
      if (signedVersion !== manifest.version) throw new Error("Studio update signature does not name the manifest version");
      if (compareVersions(manifest.version, this.options.currentVersion) <= 0) {
        this.#platformUpdate = null;
        this.#set({ phase: "current", currentVersion: this.options.currentVersion, availableVersion: null, progressPercent: null, automatic: true, detail: `Firekeep Studio ${this.options.currentVersion} is current.` });
        return this.#state;
      }
      const platform = this.options.platform === "win32" || this.options.platform === "darwin" ? this.options.platform : null;
      const platformUpdate = platform ? manifest.platforms[platform] : undefined;
      if (!platformUpdate) throw new Error("The signed Studio release does not include this platform");
      this.#platformUpdate = platformUpdate;
      if (!platformUpdate.automatic) {
        this.#set({ phase: "available", currentVersion: this.options.currentVersion, availableVersion: manifest.version, progressPercent: null, automatic: false, detail: `Firekeep Studio ${manifest.version} is ready for macOS.` });
        return this.#state;
      }

      const native = await this.options.nativeUpdater.check();
      if (!native || native.version !== manifest.version) throw new Error("The native update metadata does not match the signed Studio release");
      this.#set({ phase: "downloading", currentVersion: this.options.currentVersion, availableVersion: manifest.version, progressPercent: 0, automatic: true, detail: `Downloading verified Studio ${manifest.version}…` });
      const path = await this.options.nativeUpdater.download((percent) => {
        const progressPercent = Math.max(0, Math.min(100, Math.round(percent)));
        this.#set({ ...this.#state, progressPercent, detail: `Downloading verified Studio ${manifest.version} · ${progressPercent}%` });
      });
      await verifyArtifact(path, platformUpdate.updater);
      this.#set({ phase: "ready", currentVersion: this.options.currentVersion, availableVersion: manifest.version, progressPercent: 100, automatic: true, detail: `Studio ${manifest.version} is verified and ready. Restart now or close Studio to finish.` });
    } catch (error) {
      this.#platformUpdate = null;
      this.#set({ phase: "error", currentVersion: this.options.currentVersion, availableVersion: null, progressPercent: null, automatic: false, detail: error instanceof Error ? error.message : String(error) });
    }
    return this.#state;
  }

  #set(state: StudioUpdateState): void {
    this.#state = state;
    for (const listener of this.#listeners) listener(state);
  }
}
