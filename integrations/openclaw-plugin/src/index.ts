/**
 * The MatchAwards OpenClaw plugin: the read-only tools of the MatchAwards MCP
 * server, registered as OpenClaw agent tools.
 *
 * Nothing in `src/` authors a tool name, description or schema. They are read
 * from `tools.json`, which is generated from the server's own `tools/list`
 * (`scripts/gen_tools.py`) and checked against it by the server's test suite;
 * the plugin's identity and config schema come from `openclaw.plugin.json`.
 * Each tool forwards its arguments to the server (`src/mcp.ts`) and relays the
 * answer.
 */

import { readFileSync } from "node:fs";

import {
  buildJsonPluginConfigSchema,
  definePluginEntry,
  type AnyAgentTool,
  type OpenClawPluginApi,
  type OpenClawPluginToolContext,
} from "openclaw/plugin-sdk/plugin-entry";

import { callTool, type PluginSettings } from "./mcp.js";

/** One entry of `tools.json`: the server's own tool definition. */
export interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  annotations?: Record<string, unknown>;
}

interface PluginManifest {
  id: string;
  name: string;
  description: string;
  configSchema: Record<string, unknown>;
}

/**
 * Both files sit at the package root, one level above `src/` (tests) and
 * `dist/` (the built entry). A missing one throws: a plugin that silently
 * registers no tools looks healthy and is not.
 */
function readJson<T>(name: string): T {
  return JSON.parse(readFileSync(new URL(`../${name}`, import.meta.url), "utf8")) as T;
}

const MANIFEST: PluginManifest = readJson<PluginManifest>("openclaw.plugin.json");

const TOOLS: ToolDefinition[] = (() => {
  const parsed = readJson<unknown>("tools.json");
  if (!Array.isArray(parsed) || parsed.length === 0) {
    throw new Error("tools.json is not a non-empty array of tool definitions");
  }
  return parsed as ToolDefinition[];
})();

export function toolNames(): string[] {
  return TOOLS.map((tool) => tool.name);
}

/** The UI label, derived from the name (`search_contracts` -> "Search contracts"). */
export function toolLabel(name: string): string {
  const words = name.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * This plugin's settings out of a gateway config snapshot
 * (`plugins.entries.matchawards.config`). Walked as `unknown`: an unfamiliar
 * shape falls back to the defaults instead of throwing.
 */
export function settingsFromConfig(config: unknown): PluginSettings {
  const entries = (config as { plugins?: { entries?: Record<string, unknown> } } | undefined)
    ?.plugins?.entries;
  const entry = entries?.[MANIFEST.id] as { config?: Record<string, unknown> } | undefined;
  const endpoint = entry?.config?.["endpoint"];
  return typeof endpoint === "string" ? { endpoint } : {};
}

function asArgs(params: unknown): Record<string, unknown> {
  return typeof params === "object" && params !== null ? (params as Record<string, unknown>) : {};
}

/**
 * One AgentTool per `tools.json` entry. `parameters` is the server's input
 * schema as-is (TypeBox schemas are plain JSON Schema). `resolveSettings` is
 * read on every call, so a config edit applies without a reload.
 */
export function buildTools(resolveSettings: () => PluginSettings): AnyAgentTool[] {
  return TOOLS.map((tool) => ({
    name: tool.name,
    description: tool.description,
    parameters: tool.inputSchema as AnyAgentTool["parameters"],
    label: toolLabel(tool.name),
    // Results carry text from government notices and award records.
    resultContentSource: "network" as const,
    execute: async (_toolCallId: string, params: unknown, signal?: AbortSignal) =>
      callTool(resolveSettings(), tool.name, asArgs(params), signal),
  }));
}

/**
 * The tool factory the gateway calls. Config sources are consulted freshest
 * first: `getRuntimeConfig()`, then the `runtimeConfig` snapshot, then
 * `config`; the first that carries this plugin's settings wins.
 */
export function toolFactory(ctx: OpenClawPluginToolContext): AnyAgentTool[] {
  return buildTools(() => {
    for (const source of [ctx.getRuntimeConfig?.(), ctx.runtimeConfig, ctx.config]) {
      const settings = settingsFromConfig(source);
      if (settings.endpoint !== undefined) return settings;
    }
    return {};
  });
}

export function register(api: OpenClawPluginApi): void {
  api.registerTool(toolFactory);
}

// The annotation is required: the inferred type names openclaw's internal chunk
// files, which `tsc --declaration` cannot emit portably (TS2742).
const entry: ReturnType<typeof definePluginEntry> = definePluginEntry({
  id: MANIFEST.id,
  name: MANIFEST.name,
  description: MANIFEST.description,
  configSchema: () => buildJsonPluginConfigSchema(MANIFEST.configSchema as never),
  register,
});

export default entry;
