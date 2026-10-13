/**
 * The one network call of the plugin: a stateless JSON-RPC `tools/call` to the
 * MatchAwards MCP server.
 *
 * The plugin keeps no search logic of its own. Each tool posts its arguments to
 * the server unchanged and relays the server's answer, so the composite tools
 * (`assess_opportunity`, `find_teaming_partners`) and every server fix reach
 * OpenClaw users without a plugin release.
 *
 * Failures throw (`AgentTool.execute`'s contract) with the server's own text.
 * `McpError.retryable` marks the ones worth retrying: a rate limit, a timeout,
 * a server error or an unreachable host. Request bodies are never logged.
 */

import { readFileSync } from "node:fs";

export const DEFAULT_ENDPOINT = "https://matchawards.com/mcp";

/** How long one call may take before it is abandoned. */
export const TIMEOUT_MS = 30_000;

/** The plugin's config block (`configSchema` in `openclaw.plugin.json`). */
export interface PluginSettings {
  endpoint?: string | undefined;
}

/** A failed call. `retryable` is true when the same call may succeed later. */
export class McpError extends Error {
  readonly status: number | null;
  readonly retryable: boolean;
  /** Seconds to wait, from the server's `Retry-After` header, when it sent one. */
  readonly retryAfter: number | null;

  constructor(message: string, status: number | null, retryable: boolean, retryAfter: number | null = null) {
    super(message);
    this.name = "McpError";
    this.status = status;
    this.retryable = retryable;
    this.retryAfter = retryAfter;
  }
}

/** What a successful call hands back to the agent. */
export interface ToolResult {
  content: { type: "text"; text: string }[];
  details: unknown;
}

let cachedVersion: string | undefined;

/** This package's version, read once from package.json (one directory above src/ and dist/). */
export function packageVersion(): string {
  if (cachedVersion !== undefined) return cachedVersion;
  try {
    const raw = readFileSync(new URL("../package.json", import.meta.url), "utf8");
    const version = (JSON.parse(raw) as { version?: unknown }).version;
    cachedVersion = typeof version === "string" ? version : "0.0.0";
  } catch {
    cachedVersion = "0.0.0";
  }
  return cachedVersion;
}

/** The User-Agent every call carries, so the server counts this plugin as its own client. */
export function userAgent(): string {
  return `matchawards-openclaw-plugin/${packageVersion()}`;
}

/** The configured endpoint, or the hosted server when none is set. */
export function resolveEndpoint(settings: PluginSettings = {}): string {
  const configured = typeof settings.endpoint === "string" ? settings.endpoint.trim() : "";
  return configured === "" ? DEFAULT_ENDPOINT : configured;
}

let nextId = 0;

/** A JSON-RPC message from a JSON body, or from the `data:` lines of an event stream. */
function parseMessage(text: string, contentType: string): unknown {
  if (contentType.includes("text/event-stream")) {
    const messages = text
      .split(/\r?\n\r?\n/)
      .map((event) =>
        event
          .split(/\r?\n/)
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n"),
      )
      .filter((data) => data !== "");
    const last = messages.at(-1);
    if (last === undefined) throw new SyntaxError("empty event stream");
    return JSON.parse(last);
  }
  return JSON.parse(text);
}

function retryAfterSeconds(response: Response): number | null {
  const header = response.headers.get("Retry-After");
  if (header === null || !/^\d+$/.test(header.trim())) return null;
  return Number(header.trim());
}

/** The text items of a tool result's content, joined. */
function textOf(content: unknown): string {
  if (!Array.isArray(content)) return "";
  return content
    .filter(
      (item): item is { type: "text"; text: string } =>
        typeof item === "object" && item !== null && item.type === "text" && typeof item.text === "string",
    )
    .map((item) => item.text)
    .join("\n\n");
}

function errorMessageOf(message: unknown): string | null {
  const error = (message as { error?: { message?: unknown } } | null)?.error;
  return typeof error?.message === "string" ? error.message : null;
}

/**
 * Call one tool on the MCP server and return its result for the agent.
 *
 * `signal` is the gateway's cancellation for this call. A cancelled call
 * rethrows the abort unchanged rather than reporting a retryable fault, since
 * retrying is the opposite of what the user asked for.
 */
export async function callTool(
  settings: PluginSettings,
  name: string,
  args: Record<string, unknown> = {},
  signal?: AbortSignal,
): Promise<ToolResult> {
  // A timer rather than AbortSignal.timeout(), so it is cleared as soon as the body is read.
  const timeout = new AbortController();
  const timer = setTimeout(() => timeout.abort(new DOMException("timed out", "TimeoutError")), TIMEOUT_MS);
  const combined = signal ? AbortSignal.any([signal, timeout.signal]) : timeout.signal;
  const timedOut = (): McpError =>
    new McpError(`MatchAwards did not answer within ${TIMEOUT_MS / 1000} seconds; retry later.`, null, true);

  nextId += 1;
  let response: Response;
  let text: string;
  try {
    response = await fetch(resolveEndpoint(settings), {
      method: "POST",
      signal: combined,
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json, text/event-stream",
        "User-Agent": userAgent(),
      },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: nextId,
        method: "tools/call",
        params: { name, arguments: args },
      }),
    });
    text = await response.text();
  } catch (error) {
    if (signal?.aborted) throw error;
    if (timeout.signal.aborted) throw timedOut();
    const cause = error instanceof Error ? error.message : String(error);
    throw new McpError(`Could not reach MatchAwards (${cause}); retry later.`, null, true);
  } finally {
    clearTimeout(timer);
  }
  signal?.throwIfAborted();

  let message: unknown = null;
  try {
    message = parseMessage(text, response.headers.get("Content-Type") ?? "");
  } catch {
    message = null;
  }

  if (response.status === 429) {
    const retryAfter = retryAfterSeconds(response);
    const wait = retryAfter === null ? "a minute" : `${retryAfter} seconds`;
    const detail = errorMessageOf(message) ?? `MatchAwards rate limit reached. Wait ${wait}, then retry.`;
    throw new McpError(detail, 429, true, retryAfter);
  }
  if (!response.ok) {
    const detail = errorMessageOf(message) ?? `MatchAwards answered HTTP ${response.status}`;
    throw new McpError(detail, response.status, response.status >= 500);
  }
  if (message === null || typeof message !== "object") {
    throw new McpError(`MatchAwards returned an unreadable response (HTTP ${response.status}); retry later.`, response.status, true);
  }

  const rpcError = errorMessageOf(message);
  if (rpcError !== null) {
    throw new McpError(rpcError, response.status, false);
  }
  const result = (message as { result?: unknown }).result as
    | { content?: unknown; structuredContent?: unknown; isError?: unknown }
    | undefined;
  if (result === undefined || result === null || typeof result !== "object") {
    throw new McpError("MatchAwards returned no result; retry later.", response.status, true);
  }
  if (result.isError === true) {
    throw new McpError(textOf(result.content) || `MatchAwards could not run ${name}.`, response.status, false);
  }

  const structured = result.structuredContent;
  const body = textOf(result.content) || JSON.stringify(structured ?? null, null, 2);
  return { content: [{ type: "text", text: body }], details: structured ?? null };
}
