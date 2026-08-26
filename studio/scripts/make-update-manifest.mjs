import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { access, readFile, stat, writeFile } from "node:fs/promises";
import { basename, join } from "node:path";
import { fileURLToPath } from "node:url";

const VERSION_PATTERN = /^\d+\.\d+\.\d+$/;

function argumentsOf(values) {
  const result = new Map();
  for (let index = 0; index < values.length; index += 2) {
    const name = values[index];
    const value = values[index + 1];
    if (!name?.startsWith("--") || value === undefined) throw new Error(`invalid argument near ${name ?? "end of command"}`);
    result.set(name.slice(2), value);
  }
  return result;
}

async function artifact(directory, fileName, baseUrl) {
  const path = join(directory, fileName);
  try { await access(path); }
  catch { throw new Error(`required release artifact is missing: ${fileName}`); }
  const details = await stat(path);
  if (!details.isFile() || !details.size) throw new Error(`release artifact is empty or not a file: ${fileName}`);
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return {
    fileName,
    url: `${baseUrl}/${encodeURIComponent(fileName)}`,
    sha256: hash.digest("hex"),
    size: details.size,
  };
}

function rewriteMetadata(text, version, baseUrl, artifactNames, label) {
  const declaredVersion = /^version:\s*([^\s]+)\s*$/m.exec(text)?.[1];
  if (declaredVersion !== version) throw new Error(`${label} version ${declaredVersion ?? "missing"} does not match ${version}`);
  return text.replace(/^(\s*(?:-\s+url:|path:)\s+)([^\s]+)\s*$/gm, (line, prefix, rawValue) => {
    const value = rawValue.replace(/^['"]|['"]$/g, "");
    const fileName = basename(value);
    return artifactNames.has(fileName) ? `${prefix}${baseUrl}/${encodeURIComponent(fileName)}` : line;
  });
}

async function main() {
  const args = argumentsOf(process.argv.slice(2));
  const version = args.get("version") ?? "";
  const directory = args.get("directory") ?? "";
  const baseUrlText = args.get("base-url") ?? "";
  const publishedAt = args.get("published-at") ?? "";
  const macAutomaticText = args.get("mac-automatic") ?? "";
  if (!VERSION_PATTERN.test(version)) throw new Error("--version must use major.minor.patch");
  if (!directory) throw new Error("--directory is required");
  const packageJson = JSON.parse(await readFile(fileURLToPath(new URL("../package.json", import.meta.url)), "utf8"));
  if (packageJson.version !== version) throw new Error(`package version ${packageJson.version} does not match release ${version}`);
  const baseUrl = new URL(baseUrlText);
  if (baseUrl.protocol !== "https:" || baseUrl.username || baseUrl.password || baseUrl.search || baseUrl.hash) throw new Error("--base-url must be a credential-free HTTPS URL without query or fragment");
  const normalizedBaseUrl = baseUrl.toString().replace(/\/$/, "");
  if (Number.isNaN(Date.parse(publishedAt))) throw new Error("--published-at must be an ISO date");
  if (macAutomaticText !== "true" && macAutomaticText !== "false") throw new Error("--mac-automatic must be true or false");

  const windowsInstallerName = `Firekeep-Studio-${version}-Setup.exe`;
  const macInstallerName = `Firekeep-Studio-${version}-universal.dmg`;
  const macUpdaterName = `Firekeep-Studio-${version}-universal.zip`;
  const [windowsInstaller, macInstaller, macUpdater] = await Promise.all([
    artifact(directory, windowsInstallerName, normalizedBaseUrl),
    artifact(directory, macInstallerName, normalizedBaseUrl),
    artifact(directory, macUpdaterName, normalizedBaseUrl),
    artifact(directory, `${windowsInstallerName}.blockmap`, normalizedBaseUrl),
    artifact(directory, `${macUpdaterName}.blockmap`, normalizedBaseUrl),
    access(join(directory, "latest.yml")).catch(() => { throw new Error("required release artifact is missing: latest.yml"); }),
    access(join(directory, "latest-mac.yml")).catch(() => { throw new Error("required release artifact is missing: latest-mac.yml"); }),
  ]);
  const releaseTag = normalizedBaseUrl.split("/").at(-1);
  if (releaseTag !== `studio-v${version}`) throw new Error(`release URL tag ${releaseTag ?? "missing"} does not match studio-v${version}`);
  const releaseUrl = `https://github.com/kapella-hub/firekeep-dist/releases/tag/${releaseTag}`;
  const manifest = {
    schema: 1,
    channel: "stable",
    version,
    publishedAt: new Date(publishedAt).toISOString().replace(".000Z", "Z"),
    releaseUrl,
    platforms: {
      win32: { automatic: true, installer: windowsInstaller, updater: windowsInstaller },
      darwin: { automatic: macAutomaticText === "true", installer: macInstaller, updater: macUpdater },
    },
  };

  const artifactNames = new Set([windowsInstallerName, macInstallerName, macUpdaterName]);
  const windowsMetadataPath = join(directory, "latest.yml");
  const macMetadataPath = join(directory, "latest-mac.yml");
  const [windowsMetadata, macMetadata] = await Promise.all([readFile(windowsMetadataPath, "utf8"), readFile(macMetadataPath, "utf8")]);
  await Promise.all([
    writeFile(join(directory, "studio-update.json"), `${JSON.stringify(manifest, null, 2)}\n`, { encoding: "utf8" }),
    writeFile(windowsMetadataPath, rewriteMetadata(windowsMetadata, version, normalizedBaseUrl, artifactNames, "latest.yml"), { encoding: "utf8" }),
    writeFile(macMetadataPath, rewriteMetadata(macMetadata, version, normalizedBaseUrl, artifactNames, "latest-mac.yml"), { encoding: "utf8" }),
  ]);
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
