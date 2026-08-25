import { randomBytes, timingSafeEqual } from "node:crypto";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { normalizeDecisionBoardUrl } from "../shared/decision-board.js";

const MAX_NOTIFICATION_BYTES = 2_048;

export interface DecisionBoardReceiverEnvironment {
  readonly FIREKEEP_DECISION_SURFACE: "studio";
  readonly FIREKEEP_DECISION_NOTIFY_URL: string;
  readonly FIREKEEP_DECISION_NOTIFY_TOKEN: string;
}

export class StudioDecisionBoardReceiver {
  readonly #token = randomBytes(32).toString("base64url");
  readonly #onBoard: (url: string) => Promise<void>;
  readonly #seen = new Set<string>();
  #server: Server | null = null;

  constructor(onBoard: (url: string) => Promise<void>) {
    this.#onBoard = onBoard;
  }

  async start(): Promise<DecisionBoardReceiverEnvironment> {
    if (this.#server) throw new Error("Decision Board receiver is already running");
    const server = createServer((request, response) => {
      if (request.method !== "POST" || request.url !== "/decision") {
        response.writeHead(404).end();
        return;
      }
      if (!this.#authorized(request.headers.authorization)) {
        response.writeHead(401).end();
        return;
      }
      const chunks: Buffer[] = [];
      let size = 0;
      let tooLarge = false;
      request.on("data", (chunk: Buffer) => {
        size += chunk.length;
        if (size > MAX_NOTIFICATION_BYTES) tooLarge = true;
        else chunks.push(chunk);
      });
      request.on("end", () => {
        if (tooLarge) { response.writeHead(413).end(); return; }
        let value: unknown;
        try { value = JSON.parse(Buffer.concat(chunks).toString("utf8")); }
        catch { response.writeHead(400).end(); return; }
        const candidate = value && typeof value === "object" ? (value as Record<string, unknown>).board_url : null;
        const url = typeof candidate === "string" ? normalizeDecisionBoardUrl(candidate) : null;
        if (!url) { response.writeHead(400).end(); return; }
        response.writeHead(202, { "Content-Type": "application/json", "Cache-Control": "no-store" }).end("{}");
        if (this.#seen.has(url)) return;
        this.#seen.add(url);
        queueMicrotask(() => { void this.#onBoard(url).catch(() => { this.#seen.delete(url); }); });
      });
    });
    server.on("clientError", (_error, socket) => socket.end("HTTP/1.1 400 Bad Request\r\n\r\n"));
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", () => { server.off("error", reject); resolve(); });
    });
    this.#server = server;
    const address = server.address() as AddressInfo;
    return {
      FIREKEEP_DECISION_SURFACE: "studio",
      FIREKEEP_DECISION_NOTIFY_URL: `http://127.0.0.1:${address.port}/decision`,
      FIREKEEP_DECISION_NOTIFY_TOKEN: this.#token,
    };
  }

  async close(): Promise<void> {
    const server = this.#server;
    this.#server = null;
    if (!server) return;
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }

  #authorized(header: string | undefined): boolean {
    if (!header?.startsWith("Bearer ")) return false;
    const candidate = Buffer.from(header.slice(7), "utf8");
    const expected = Buffer.from(this.#token, "utf8");
    return candidate.length === expected.length && timingSafeEqual(candidate, expected);
  }
}
