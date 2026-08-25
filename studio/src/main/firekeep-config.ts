import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

const DASHBOARD_PORT = 8040;

export function dashboardUrlFromConfig(text: string): string | null {
  const server = iniSection(text, "server");
  const kind = server.get("kind")?.toLowerCase();
  try {
    if (kind === "ports") {
      const scheme = server.get("scheme")?.toLowerCase();
      const host = server.get("host");
      if (!httpScheme(scheme) || !host) return null;
      const url = new URL(`${scheme}://${host}:${DASHBOARD_PORT}/`);
      return safeHttpUrl(url) && url.port === String(DASHBOARD_PORT) && url.pathname === "/" ? url.toString() : null;
    }
    if (kind === "paths") {
      const baseUrl = server.get("base_url");
      if (!baseUrl) return null;
      const url = new URL(baseUrl);
      if (!safeHttpUrl(url) || url.search || url.hash) return null;
      return url.toString().replace(/\/$/, "");
    }
  } catch {
    return null;
  }
  return null;
}

export async function loadConfiguredDashboardUrl(configPath = process.env.FIREKEEP_CONFIG?.trim() || join(homedir(), ".firekeep", "config")): Promise<string | null> {
  try {
    return dashboardUrlFromConfig(await readFile(configPath, "utf8"));
  } catch {
    return null;
  }
}

function iniSection(text: string, wanted: string): Map<string, string> {
  const values = new Map<string, string>();
  let active = false;
  for (const rawLine of text.replace(/^\uFEFF/, "").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || line.startsWith(";")) continue;
    const section = /^\[([^\]]+)\]$/.exec(line);
    if (section) {
      active = section[1]?.trim().toLowerCase() === wanted;
      continue;
    }
    if (!active) continue;
    const delimiter = line.search(/[=:]/);
    if (delimiter < 1) continue;
    const key = line.slice(0, delimiter).trim().toLowerCase();
    const value = line.slice(delimiter + 1).replace(/\s[;#].*$/, "").trim();
    values.set(key, value);
  }
  return values;
}

function httpScheme(value: string | undefined): value is "http" | "https" {
  return value === "http" || value === "https";
}

function safeHttpUrl(url: URL): boolean {
  return (url.protocol === "http:" || url.protocol === "https:")
    && Boolean(url.hostname)
    && !url.username
    && !url.password;
}
