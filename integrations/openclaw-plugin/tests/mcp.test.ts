/**
 * The one network call: request shape, result mapping and every failure path.
 * `fetch` is mocked throughout; nothing here touches the network.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_ENDPOINT,
  McpError,
  TIMEOUT_MS,
  callTool,
  packageVersion,
  resolveEndpoint,
  userAgent,
} from "../src/mcp.js";

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function rpcResult(result: unknown): Response {
  return jsonResponse(200, { jsonrpc: "2.0", id: 1, result });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function lastCall(): [string, RequestInit] {
  const call = fetchMock.mock.calls.at(-1);
  expect(call).toBeDefined();
  return call as [string, RequestInit];
}

function headersOf(init: RequestInit): Record<string, string> {
  return (init.headers ?? {}) as Record<string, string>;
}

async function failureOf(promise: Promise<unknown>): Promise<McpError> {
  const error = await promise.then(
    () => null,
    (e: unknown) => e,
  );
  expect(error).toBeInstanceOf(McpError);
  return error as McpError;
}

describe("the request", () => {
  it("posts a stateless JSON-RPC tools/call with the arguments unchanged", async () => {
    fetchMock.mockResolvedValue(rpcResult({ content: [{ type: "text", text: "{}" }], structuredContent: {} }));
    await callTool({}, "search_contracts", { naics: "541512", state: "VA" });
    const [url, init] = lastCall();
    expect(url).toBe(DEFAULT_ENDPOINT);
    expect(url).toBe("https://matchawards.com/mcp");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body).toEqual({
      jsonrpc: "2.0",
      id: expect.any(Number),
      method: "tools/call",
      params: { name: "search_contracts", arguments: { naics: "541512", state: "VA" } },
    });
  });

  it("sends JSON, accepts JSON or an event stream, and names this plugin and its version", async () => {
    fetchMock.mockResolvedValue(rpcResult({ content: [] }));
    await callTool({}, "lookup_naics", { query: "roofing" });
    const headers = headersOf(lastCall()[1]);
    expect(headers["Content-Type"]).toBe("application/json");
    expect(headers.Accept).toBe("application/json, text/event-stream");
    expect(headers["User-Agent"]).toBe(`matchawards-openclaw-plugin/${packageVersion()}`);
    expect(userAgent()).toMatch(/^matchawards-openclaw-plugin\/\d+\.\d+\.\d+/);
  });

  it("uses the version from package.json", async () => {
    const { readFileSync } = await import("node:fs");
    const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")) as {
      version: string;
    };
    expect(packageVersion()).toBe(pkg.version);
  });

  it("sends an empty arguments object when none are given", async () => {
    fetchMock.mockResolvedValue(rpcResult({ content: [] }));
    await callTool({}, "search_grants");
    const body = JSON.parse(lastCall()[1].body as string) as { params: { arguments: unknown } };
    expect(body.params.arguments).toEqual({});
  });

  it("calls a configured endpoint instead of the hosted one", async () => {
    fetchMock.mockResolvedValue(rpcResult({ content: [] }));
    await callTool({ endpoint: " http://127.0.0.1:8765/mcp " }, "search_jobs", {});
    expect(lastCall()[0]).toBe("http://127.0.0.1:8765/mcp");
    expect(resolveEndpoint({ endpoint: "   " })).toBe(DEFAULT_ENDPOINT);
    expect(resolveEndpoint({})).toBe(DEFAULT_ENDPOINT);
  });
});

describe("a successful result", () => {
  it("relays the text content and puts structuredContent in details", async () => {
    const structured = { results: [{ id: "1", url: "https://matchawards.com/x/posts/1" }], has_more: false };
    fetchMock.mockResolvedValue(
      rpcResult({
        content: [{ type: "text", text: JSON.stringify(structured, null, 2) }],
        structuredContent: structured,
        isError: false,
      }),
    );
    const result = await callTool({}, "search_contracts", {});
    expect(result.content).toEqual([{ type: "text", text: JSON.stringify(structured, null, 2) }]);
    expect(result.details).toEqual(structured);
  });

  it("falls back to the JSON of structuredContent when there is no text content", async () => {
    const structured = { result: { code: "541512" } };
    fetchMock.mockResolvedValue(rpcResult({ content: [], structuredContent: structured }));
    const result = await callTool({}, "lookup_naics", { code: "541512" });
    expect(result.content).toEqual([{ type: "text", text: JSON.stringify(structured, null, 2) }]);
    expect(result.details).toEqual(structured);
  });

  it("reads a result sent as an event stream", async () => {
    const structured = { results: [] };
    const message = { jsonrpc: "2.0", id: 1, result: { content: [{ type: "text", text: "{}" }], structuredContent: structured } };
    fetchMock.mockResolvedValue(
      new Response(`event: message\ndata: ${JSON.stringify(message)}\n\n`, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    );
    const result = await callTool({}, "search_awards", {});
    expect(result.details).toEqual(structured);
  });
});

describe("failures throw with the server's words", () => {
  it("marks a 429 retryable and carries the server's message and Retry-After", async () => {
    const message = "Rate limit reached. Wait 12 seconds, then retry.";
    fetchMock.mockResolvedValue(
      jsonResponse(429, { jsonrpc: "2.0", id: null, error: { code: -32000, message } }, { "Retry-After": "12" }),
    );
    const error = await failureOf(callTool({}, "search_contracts", {}));
    expect(error.message).toBe(message);
    expect(error.status).toBe(429);
    expect(error.retryable).toBe(true);
    expect(error.retryAfter).toBe(12);
  });

  it("words a 429 from Retry-After when the body has no message", async () => {
    fetchMock.mockResolvedValue(new Response("Too Many Requests", { status: 429, headers: { "Retry-After": "7" } }));
    const error = await failureOf(callTool({}, "search_contracts", {}));
    expect(error.message).toContain("7 seconds");
    expect(error.retryable).toBe(true);
    expect(error.retryAfter).toBe(7);
  });

  it("throws a JSON-RPC error's message", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, { jsonrpc: "2.0", id: 1, error: { code: -32602, message: "Invalid params: limit" } }),
    );
    const error = await failureOf(callTool({}, "search_jobs", { limit: 500 }));
    expect(error.message).toBe("Invalid params: limit");
    expect(error.retryable).toBe(false);
  });

  it("throws the text of a result with isError", async () => {
    const text = "MatchAwards has no awards for UEI ABCDEFGHIJKL.";
    fetchMock.mockResolvedValue(rpcResult({ content: [{ type: "text", text }], isError: true }));
    const error = await failureOf(callTool({}, "get_company", { uei: "ABCDEFGHIJKL" }));
    expect(error.message).toBe(text);
    expect(error.retryable).toBe(false);
  });

  it("marks a 5xx retryable", async () => {
    fetchMock.mockResolvedValue(new Response("Bad Gateway", { status: 502 }));
    const error = await failureOf(callTool({}, "search_contracts", {}));
    expect(error.status).toBe(502);
    expect(error.retryable).toBe(true);
    expect(error.message).toContain("HTTP 502");
  });

  it("does not retry a 4xx other than 429", async () => {
    fetchMock.mockResolvedValue(new Response("Invalid Host header", { status: 421 }));
    const error = await failureOf(callTool({}, "search_contracts", {}));
    expect(error.retryable).toBe(false);
  });

  it("treats an unreadable 200 as retryable, never as an empty answer", async () => {
    fetchMock.mockResolvedValue(new Response("<html>maintenance</html>", { status: 200 }));
    const error = await failureOf(callTool({}, "search_contracts", {}));
    expect(error.retryable).toBe(true);
    expect(error.message).toContain("unreadable");
  });

  it("turns a network failure into a retryable error", async () => {
    fetchMock.mockRejectedValue(new TypeError("fetch failed"));
    const error = await failureOf(callTool({}, "search_contracts", {}));
    expect(error.retryable).toBe(true);
    expect(error.status).toBeNull();
    expect(error.message).toContain("fetch failed");
  });

  it("never puts the request body in an error", async () => {
    fetchMock.mockResolvedValue(new Response("nope", { status: 400 }));
    const error = await failureOf(callTool({}, "search_contracts", { keyword: "secret-keyword-xyz" }));
    expect(error.message).not.toContain("secret-keyword-xyz");
  });
});

describe("cancellation and the timeout", () => {
  it("hands fetch a signal that aborts when the caller's does", async () => {
    fetchMock.mockResolvedValue(rpcResult({ content: [] }));
    const controller = new AbortController();
    await callTool({}, "search_contracts", {}, controller.signal);
    const signal = lastCall()[1].signal as AbortSignal;
    expect(signal).toBeInstanceOf(AbortSignal);
    expect(signal.aborted).toBe(false);
    controller.abort();
    expect(signal.aborted).toBe(true);
  });

  it("rethrows the caller's abort instead of calling it retryable", async () => {
    const controller = new AbortController();
    controller.abort();
    fetchMock.mockRejectedValue(new DOMException("This operation was aborted", "AbortError"));
    const error = await callTool({}, "search_contracts", {}, controller.signal).catch((e: unknown) => e);
    expect(error).not.toBeInstanceOf(McpError);
    expect((error as Error).name).toBe("AbortError");
  });

  it("rethrows an abort that lands while the body is read", async () => {
    const controller = new AbortController();
    fetchMock.mockImplementation(async () => ({
      ok: true,
      status: 200,
      headers: new Headers({ "Content-Type": "application/json" }),
      text: async () => {
        controller.abort();
        throw new DOMException("This operation was aborted", "AbortError");
      },
    }));
    const error = await callTool({}, "search_contracts", {}, controller.signal).catch((e: unknown) => e);
    expect(error).not.toBeInstanceOf(McpError);
    expect((error as Error).message).toMatch(/abort/i);
  });

  it("gives up after the timeout with a retryable error", async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => reject(init.signal?.reason));
        }),
    );
    const pending = callTool({}, "search_contracts", {}).catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(TIMEOUT_MS + 10);
    const error = (await pending) as McpError;
    expect(error).toBeInstanceOf(McpError);
    expect(error.retryable).toBe(true);
    expect(error.message).toContain("30 seconds");
  });
});
