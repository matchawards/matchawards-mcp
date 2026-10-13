/**
 * The gateway seam: tools built from tools.json, config read from the
 * gateway's snapshots, and the entry's identity taken from the manifest.
 * `fetch` is mocked throughout.
 */

import { readFileSync } from "node:fs";

import type { OpenClawPluginApi, OpenClawPluginToolContext } from "openclaw/plugin-sdk/plugin-entry";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import entry, {
  buildTools,
  register,
  settingsFromConfig,
  toolFactory,
  toolLabel,
  toolNames,
  type ToolDefinition,
} from "../src/index.js";
import { DEFAULT_ENDPOINT } from "../src/mcp.js";

const MANIFEST = JSON.parse(readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8")) as {
  id: string;
  name: string;
  description: string;
};
const FIXTURE = JSON.parse(readFileSync(new URL("../tools.json", import.meta.url), "utf8")) as ToolDefinition[];

function ok(): Response {
  return new Response(JSON.stringify({ jsonrpc: "2.0", id: 1, result: { content: [], structuredContent: {} } }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(async () => ok());
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function lastUrl(): string {
  return fetchMock.mock.calls.at(-1)?.[0] as string;
}

const configWith = (endpoint: string) => ({ plugins: { entries: { matchawards: { config: { endpoint } } } } });

describe("tool construction", () => {
  const tools = buildTools(() => ({}));

  it("builds one tool per tools.json entry, with its name, description and schema", () => {
    expect(tools.map((tool) => tool.name)).toEqual(FIXTURE.map((tool) => tool.name));
    for (const [i, tool] of tools.entries()) {
      expect(tool.description).toBe(FIXTURE[i]!.description);
      expect(tool.parameters).toEqual(FIXTURE[i]!.inputSchema);
    }
  });

  it("derives labels from the names", () => {
    expect(toolLabel("search_contracts")).toBe("Search contracts");
    for (const name of toolNames()) expect(toolLabel(name)).not.toContain("_");
  });

  it("forwards a call to the server under the tool's own name", async () => {
    const search = tools.find((tool) => tool.name === "search_awards");
    await search!.execute("call-1", { naics: "541512" });
    const body = JSON.parse(fetchMock.mock.calls.at(-1)?.[1].body as string) as {
      params: { name: string; arguments: unknown };
    };
    expect(body.params).toEqual({ name: "search_awards", arguments: { naics: "541512" } });
    expect(lastUrl()).toBe(DEFAULT_ENDPOINT);
  });

  it("reads settings on every call, so a config edit needs no reload", async () => {
    let endpoint: string | undefined;
    const live = buildTools(() => ({ endpoint }));
    const tool = live[0]!;
    await tool.execute("call-a", {});
    expect(lastUrl()).toBe(DEFAULT_ENDPOINT);
    endpoint = "http://127.0.0.1:9/mcp";
    await tool.execute("call-b", {});
    expect(lastUrl()).toBe("http://127.0.0.1:9/mcp");
  });
});

describe("reading config out of a gateway snapshot", () => {
  it("finds plugins.entries.matchawards.config.endpoint", () => {
    expect(settingsFromConfig(configWith("http://x.invalid/mcp"))).toEqual({ endpoint: "http://x.invalid/mcp" });
  });

  it("falls back to the defaults on any shape it does not recognize", () => {
    for (const config of [undefined, null, {}, { plugins: {} }, "nonsense", { plugins: 3 }]) {
      expect(settingsFromConfig(config)).toEqual({});
    }
    expect(settingsFromConfig({ plugins: { entries: { matchawards: { config: { endpoint: 42 } } } } })).toEqual({});
  });
});

describe("the gateway seam", () => {
  async function urlVia(ctx: unknown): Promise<string> {
    const tool = toolFactory(ctx as OpenClawPluginToolContext)[0]!;
    await tool.execute("call", {});
    return lastUrl();
  }

  it("builds every tool from the context it is handed", () => {
    expect(toolFactory({} as OpenClawPluginToolContext).map((tool) => tool.name)).toEqual(toolNames());
  });

  it("calls the hosted server when nothing is configured", async () => {
    expect(await urlVia({})).toBe(DEFAULT_ENDPOINT);
  });

  it("overrides the endpoint from getRuntimeConfig, runtimeConfig or config", async () => {
    expect(await urlVia({ getRuntimeConfig: () => configWith("http://a.invalid/mcp") })).toBe("http://a.invalid/mcp");
    expect(await urlVia({ runtimeConfig: configWith("http://b.invalid/mcp") })).toBe("http://b.invalid/mcp");
    expect(await urlVia({ config: configWith("http://c.invalid/mcp") })).toBe("http://c.invalid/mcp");
  });

  it("prefers the freshest source and falls through one without this plugin's entry", async () => {
    expect(
      await urlVia({
        getRuntimeConfig: () => configWith("http://fresh.invalid/mcp"),
        runtimeConfig: configWith("http://stale.invalid/mcp"),
      }),
    ).toBe("http://fresh.invalid/mcp");
    const foreign = { plugins: { entries: { "some-other-plugin": { config: {} } } } };
    expect(await urlVia({ getRuntimeConfig: () => foreign, config: configWith("http://c.invalid/mcp") })).toBe(
      "http://c.invalid/mcp",
    );
  });

  it("threads the gateway's abort signal down to fetch", async () => {
    const controller = new AbortController();
    const tool = toolFactory({} as OpenClawPluginToolContext)[0]!;
    await tool.execute("call", {}, controller.signal);
    const signal = fetchMock.mock.calls.at(-1)?.[1].signal as AbortSignal;
    controller.abort();
    expect(signal.aborted).toBe(true);
  });

  it("registers the factory itself", () => {
    const registerTool = vi.fn();
    register({ registerTool } as unknown as OpenClawPluginApi);
    expect(registerTool).toHaveBeenCalledWith(toolFactory);
  });

  it("takes its identity from the manifest", () => {
    expect(entry.id).toBe(MANIFEST.id);
    expect(entry.name).toBe(MANIFEST.name);
    expect(entry.description).toBe(MANIFEST.description);
  });
});
