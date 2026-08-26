import { createHash } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  HttpSignedManifestSource,
  StudioUpdater,
  type SignedManifestSource,
  type StudioUpdateManifest,
} from "../src/main/studio-updater.js";
import { createSignedFixture } from "./helpers/update-signing.js";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  vi.unstubAllGlobals();
  await Promise.all(temporaryDirectories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

async function artifact(contents = "verified Studio installer"): Promise<{ readonly path: string; readonly sha256: string; readonly size: number }> {
  const directory = await mkdtemp(join(tmpdir(), "firekeep-studio-update-test-"));
  temporaryDirectories.push(directory);
  const path = join(directory, "Firekeep-Studio-0.4.0-Setup.exe");
  const bytes = Buffer.from(contents);
  await writeFile(path, bytes);
  return { path, sha256: createHash("sha256").update(bytes).digest("hex"), size: bytes.length };
}

function manifest(sha256: string, size: number, overrides: Partial<StudioUpdateManifest> = {}): StudioUpdateManifest {
  const release = "https://github.com/kapella-hub/firekeep-dist/releases/download/studio-v0.4.0";
  return {
    schema: 1,
    channel: "stable",
    version: "0.4.0",
    publishedAt: "2026-08-26T18:00:00Z",
    releaseUrl: "https://github.com/kapella-hub/firekeep-dist/releases/tag/studio-v0.4.0",
    platforms: {
      win32: {
        automatic: true,
        installer: { fileName: "Firekeep-Studio-0.4.0-Setup.exe", url: `${release}/Firekeep-Studio-0.4.0-Setup.exe`, sha256, size },
        updater: { fileName: "Firekeep-Studio-0.4.0-Setup.exe", url: `${release}/Firekeep-Studio-0.4.0-Setup.exe`, sha256, size },
      },
      darwin: {
        automatic: false,
        installer: { fileName: "Firekeep-Studio-0.4.0-universal.dmg", url: `${release}/Firekeep-Studio-0.4.0-universal.dmg`, sha256, size },
        updater: { fileName: "Firekeep-Studio-0.4.0-universal.zip", url: `${release}/Firekeep-Studio-0.4.0-universal.zip`, sha256, size },
      },
    },
    ...overrides,
  };
}

function source(value: StudioUpdateManifest, valid = true): { readonly source: SignedManifestSource; readonly publicKey: string } {
  const bytes = Buffer.from(`${JSON.stringify(value)}\n`);
  const fixture = createSignedFixture(bytes, `timestamp:1787770800 version:${value.version}`);
  return {
    source: { load: vi.fn(async () => ({ bytes, signature: valid ? fixture.signature : `${fixture.signature}x` })) },
    publicKey: fixture.publicKey,
  };
}

function native(downloadedFile: string) {
  return {
    check: vi.fn(async () => ({ version: "0.4.0" })),
    download: vi.fn(async (progress: (percent: number) => void) => { progress(42.4); return downloadedFile; }),
    install: vi.fn(),
  };
}

describe("StudioUpdater", () => {
  it("downloads a newer Windows release, verifies its signed hash, and installs only after shutdown", async () => {
    const file = await artifact();
    const signed = source(manifest(file.sha256, file.size));
    const client = native(file.path);
    const requestQuit = vi.fn();
    const updater = new StudioUpdater({
      currentVersion: "0.3.7",
      packaged: true,
      platform: "win32",
      manifestSource: signed.source,
      publicKey: signed.publicKey,
      nativeUpdater: client,
      openExternal: vi.fn(),
      requestQuit,
    });

    await updater.check();

    expect(updater.snapshot()).toMatchObject({ phase: "ready", availableVersion: "0.4.0", progressPercent: 100 });
    expect(client.download).toHaveBeenCalledOnce();
    expect(client.install).not.toHaveBeenCalled();
    await updater.install();
    expect(requestQuit).toHaveBeenCalledOnce();
    expect(client.install).not.toHaveBeenCalled();
    updater.installDownloaded();
    expect(client.install).toHaveBeenCalledOnce();
    await updater.check();
    expect(signed.source.load).toHaveBeenCalledOnce();
  });

  it("bounds streamed channel metadata even when content-length is absent", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      const response = new Response(url.includes(".minisig") ? "signature" : new Uint8Array(256_001), { status: 200 });
      Object.defineProperty(response, "url", { value: url });
      return response;
    }));
    const source = new HttpSignedManifestSource("https://updates.example.test");

    await expect(source.load()).rejects.toThrow(/response is too large/i);
  });

  it("refuses an artifact whose bytes do not match the signed release manifest", async () => {
    const expected = await artifact("expected");
    const downloaded = await artifact("tampered");
    const signed = source(manifest(expected.sha256, expected.size));
    const client = native(downloaded.path);
    const updater = new StudioUpdater({
      currentVersion: "0.3.7",
      packaged: true,
      platform: "win32",
      manifestSource: signed.source,
      publicKey: signed.publicKey,
      nativeUpdater: client,
      openExternal: vi.fn(),
      requestQuit: vi.fn(),
    });

    await updater.check();

    expect(updater.snapshot()).toMatchObject({ phase: "error" });
    expect(updater.snapshot().detail).toMatch(/did not match the signed release manifest/i);
    updater.installDownloaded();
    expect(client.install).not.toHaveBeenCalled();
  });

  it("offers the signed release's universal DMG when Apple signing is not armed", async () => {
    const file = await artifact();
    const value = manifest(file.sha256, file.size);
    const signed = source(value);
    const client = native(file.path);
    const openExternal = vi.fn(async () => undefined);
    const updater = new StudioUpdater({
      currentVersion: "0.3.7",
      packaged: true,
      platform: "darwin",
      manifestSource: signed.source,
      publicKey: signed.publicKey,
      nativeUpdater: client,
      openExternal,
      requestQuit: vi.fn(),
    });

    await updater.check();
    expect(updater.snapshot()).toMatchObject({ phase: "available", availableVersion: "0.4.0", automatic: false });
    expect(client.check).not.toHaveBeenCalled();

    await updater.install();
    expect(openExternal).toHaveBeenCalledWith(value.platforms.darwin?.installer.url);
  });

  it("fails closed before contacting the native updater when the channel signature is invalid", async () => {
    const file = await artifact();
    const signed = source(manifest(file.sha256, file.size), false);
    const client = native(file.path);
    const updater = new StudioUpdater({
      currentVersion: "0.3.7",
      packaged: true,
      platform: "win32",
      manifestSource: signed.source,
      publicKey: signed.publicKey,
      nativeUpdater: client,
      openExternal: vi.fn(),
      requestQuit: vi.fn(),
    });

    await updater.check();

    expect(updater.snapshot()).toMatchObject({ phase: "error" });
    expect(client.check).not.toHaveBeenCalled();
  });

  it("does no network work in an unpackaged development build", async () => {
    const file = await artifact();
    const signed = source(manifest(file.sha256, file.size));
    const client = native(file.path);
    const updater = new StudioUpdater({
      currentVersion: "0.3.7",
      packaged: false,
      platform: "win32",
      manifestSource: signed.source,
      publicKey: signed.publicKey,
      nativeUpdater: client,
      openExternal: vi.fn(),
      requestQuit: vi.fn(),
    });

    await updater.check();

    expect(updater.snapshot()).toMatchObject({ phase: "disabled" });
    expect(signed.source.load).not.toHaveBeenCalled();
  });
});
