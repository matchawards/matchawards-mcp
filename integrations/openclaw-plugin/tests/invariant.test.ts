/**
 * Structural guards: a tool's definition lives only in tools.json (generated
 * from the server), the package ships what the plugin reads at runtime, and the
 * hosted endpoint is named in one place.
 */

import { readFileSync, readdirSync } from "node:fs";
import { describe, expect, it } from "vitest";

import type { ToolDefinition } from "../src/index.js";

const SRC_DIR = new URL("../src/", import.meta.url);

function sourceFiles(): { name: string; text: string }[] {
  return readdirSync(SRC_DIR)
    .filter((name) => name.endsWith(".ts"))
    .map((name) => ({ name, text: readFileSync(new URL(name, SRC_DIR), "utf8") }));
}

function readJson<T>(name: string): T {
  return JSON.parse(readFileSync(new URL(`../${name}`, import.meta.url), "utf8")) as T;
}

const fixture = readJson<ToolDefinition[]>("tools.json");
const manifest = readJson<{
  id: string;
  version?: unknown;
  contracts: { tools: string[] };
  configSchema: { additionalProperties: boolean; properties: Record<string, { default?: unknown }> };
}>("openclaw.plugin.json");
const pkg = readJson<{
  name: string;
  version: string;
  files: string[];
  dependencies?: Record<string, string>;
  openclaw: { extensions: string[] };
}>("package.json");

describe("tools.json", () => {
  it("holds the nine read-only data tools and no card tool", () => {
    expect(fixture.map((tool) => tool.name).sort()).toEqual([
      "assess_opportunity",
      "find_teaming_partners",
      "get_company",
      "get_opportunity",
      "lookup_naics",
      "search_awards",
      "search_contracts",
      "search_grants",
      "search_jobs",
    ]);
    for (const tool of fixture) {
      expect(tool.description.length).toBeGreaterThan(0);
      expect(tool.inputSchema["type"]).toBe("object");
      expect(tool.annotations?.["readOnlyHint"]).toBe(true);
    }
  });

  it("matches the manifest's contracts.tools, sorted", () => {
    expect(manifest.contracts.tools).toEqual(fixture.map((tool) => tool.name).sort());
  });
});

describe("no tool definitions in src/", () => {
  const sources = sourceFiles();
  const joined = sources.map((file) => file.text).join("\n");

  it("finds source files to check", () => {
    expect(sources.length).toBeGreaterThan(0);
  });

  it("names no tool and copies no description", () => {
    for (const tool of fixture) {
      expect(joined).not.toContain(`"${tool.name}"`);
      expect(joined).not.toContain(tool.description.slice(0, 40));
    }
  });

  it("writes no schema properties by hand", () => {
    expect(sources.filter((file) => /\bproperties\s*:\s*\{/.test(file.text)).map((file) => file.name)).toEqual([]);
  });

  it("names the hosted endpoint only in mcp.ts", () => {
    const offenders = sources
      .filter((file) => file.name !== "mcp.ts" && file.text.includes("matchawards.com"))
      .map((file) => file.name);
    expect(offenders).toEqual([]);
  });
});

describe("the manifest and the package", () => {
  it("keeps the version in package.json only", () => {
    expect(manifest.version).toBeUndefined();
    expect(pkg.name).toBe("@matchawards/openclaw-plugin");
    expect(pkg.version).toMatch(/^\d+\.\d+\.\d+$/);
  });

  it("configures only the endpoint, defaulting to the hosted server", () => {
    expect(manifest.configSchema.additionalProperties).toBe(false);
    expect(Object.keys(manifest.configSchema.properties)).toEqual(["endpoint"]);
    expect(manifest.configSchema.properties["endpoint"]?.default).toBe("https://matchawards.com/mcp");
  });

  it("ships tools.json and the manifest with the build", () => {
    expect(pkg.files).toContain("dist");
    expect(pkg.files).toContain("tools.json");
    expect(pkg.files).toContain("openclaw.plugin.json");
  });

  it("points the gateway at the built entry", () => {
    expect(pkg.openclaw.extensions).toEqual(["./dist/index.js"]);
  });

  it("has no runtime dependencies", () => {
    expect(pkg.dependencies ?? {}).toEqual({});
  });
});
