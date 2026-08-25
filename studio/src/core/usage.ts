import type { RuntimeUsage } from "./runtime.js";

interface JsonObject { readonly [key: string]: unknown }

export interface ClaudeUsageSample {
  readonly messageId: string;
  readonly usage: RuntimeUsage;
}

export function usageTokens(usage: RuntimeUsage): number {
  if (usage.totalTokens !== undefined) return Math.max(0, usage.totalTokens);
  return Math.max(0, usage.inputTokens ?? 0)
    + Math.max(0, usage.cacheCreationInputTokens ?? 0)
    + Math.max(0, usage.cachedInputTokens ?? 0)
    + Math.max(0, usage.outputTokens ?? 0);
}

export function cachedUsageTokens(usage: RuntimeUsage): number {
  return Math.min(usageTokens(usage), Math.max(0, usage.cachedInputTokens ?? 0));
}

export function freshUsageTokens(usage: RuntimeUsage): number {
  return usageTokens(usage) - cachedUsageTokens(usage);
}

export function parseClaudeUsage(rawUsage: unknown, metadata: unknown = {}): RuntimeUsage {
  const raw = object(rawUsage);
  const envelope = object(metadata);
  const inputTokens = number(raw.input_tokens);
  const cacheCreationInputTokens = number(raw.cache_creation_input_tokens);
  const cachedInputTokens = number(raw.cache_read_input_tokens);
  const outputTokens = number(raw.output_tokens);
  const costUsd = number(envelope.total_cost_usd);
  const durationMs = number(envelope.duration_ms);
  const measured = [inputTokens, cacheCreationInputTokens, cachedInputTokens, outputTokens].some((value) => value !== undefined);
  const totalTokens = measured
    ? (inputTokens ?? 0) + (cacheCreationInputTokens ?? 0) + (cachedInputTokens ?? 0) + (outputTokens ?? 0)
    : undefined;
  return {
    ...(inputTokens !== undefined ? { inputTokens } : {}),
    ...(cacheCreationInputTokens !== undefined ? { cacheCreationInputTokens } : {}),
    ...(cachedInputTokens !== undefined ? { cachedInputTokens } : {}),
    ...(outputTokens !== undefined ? { outputTokens } : {}),
    ...(totalTokens !== undefined ? { totalTokens } : {}),
    ...(costUsd !== undefined ? { costUsd } : {}),
    ...(durationMs !== undefined ? { durationMs } : {}),
  };
}

export function claudeUsageSample(rawEvent: unknown): ClaudeUsageSample | null {
  const event = object(rawEvent);
  if (event.type !== "assistant") return null;
  const message = object(event.message);
  const messageId = string(message.id);
  if (!messageId) return null;
  const usage = parseClaudeUsage(message.usage);
  return usage.totalTokens === undefined ? null : { messageId, usage };
}

export function sumRuntimeUsage(values: Iterable<RuntimeUsage>): RuntimeUsage {
  const items = [...values];
  const sum = (key: keyof RuntimeUsage): number | undefined => {
    let found = false;
    let total = 0;
    for (const item of items) {
      const value = item[key];
      if (typeof value !== "number" || !Number.isFinite(value)) continue;
      found = true;
      total += value;
    }
    return found ? total : undefined;
  };
  const totalTokens = items.length ? items.reduce((total, item) => total + usageTokens(item), 0) : undefined;
  const inputTokens = sum("inputTokens");
  const cacheCreationInputTokens = sum("cacheCreationInputTokens");
  const cachedInputTokens = sum("cachedInputTokens");
  const outputTokens = sum("outputTokens");
  const reasoningTokens = sum("reasoningTokens");
  const costUsd = sum("costUsd");
  const durationMs = sum("durationMs");
  return {
    ...(inputTokens !== undefined ? { inputTokens } : {}),
    ...(cacheCreationInputTokens !== undefined ? { cacheCreationInputTokens } : {}),
    ...(cachedInputTokens !== undefined ? { cachedInputTokens } : {}),
    ...(outputTokens !== undefined ? { outputTokens } : {}),
    ...(reasoningTokens !== undefined ? { reasoningTokens } : {}),
    ...(totalTokens !== undefined ? { totalTokens } : {}),
    ...(costUsd !== undefined ? { costUsd } : {}),
    ...(durationMs !== undefined ? { durationMs } : {}),
  };
}

function object(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {};
}

function string(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}

function number(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}
