# MatchAwards for OpenClaw

An [OpenClaw](https://openclaw.ai) plugin, published on ClawHub as `@matchawards/openclaw-plugin`, plus a ClawHub
skill. They let an agent search US government contract opportunities, grants and jobs, see who won similar work,
and size up an opportunity before you bid. Everything is read-only and needs no account and no API key.

## What's inside

```
openclaw-plugin/
├── openclaw.plugin.json   # the plugin manifest (id matchawards; contracts.tools = the nine tools)
├── package.json           # builds src/ to dist/ with tsc; no runtime dependencies
├── src/index.ts           # registers one OpenClaw tool per tools.json entry
├── src/mcp.ts             # the one network call: a JSON-RPC tools/call to the MCP server
├── tools.json             # tool definitions, generated from the server (scripts/gen_tools.py)
├── tests/                 # vitest, fetch mocked
├── skills/matchawards/    # the ClawHub skill: the public REST API with curl
│   └── SKILL.md
└── LICENSE                # MIT
```

The plugin is a thin client of the hosted [MatchAwards MCP server](../../README.md) at `https://matchawards.com/mcp`.
Each tool sends its arguments to the server as a stateless JSON-RPC `tools/call` and relays the answer: the text
goes to the model, the structured result to `details`. The tool names, descriptions and input schemas in
`tools.json` are generated from the server's own `tools/list`, and the server's test suite fails when they drift.

## Install

```bash
openclaw plugins install clawhub:@matchawards/openclaw-plugin
```

Or from a checkout of this folder: `npm ci && npm test && npm run build`, then
`openclaw plugins install --link ./integrations/openclaw-plugin`.

The skill installs on its own with `openclaw skills install @matchawards/matchawards`, for agents without the plugin.

### Config

One optional setting, `plugins.entries.matchawards.config.endpoint`: the MCP server URL. It defaults to
`https://matchawards.com/mcp`; point it at a self-hosted server (`matchawards-mcp --http`) if you run one.

## Tools

| Tool | Returns |
|---|---|
| `search_contracts` | Federal and state contract opportunities by NAICS, state, keyword, set-aside, agency, date. |
| `search_grants` | Federal grant and funding opportunities. |
| `search_jobs` | Job postings from the last 30 days. |
| `get_opportunity` | One opportunity in full: description, published contacts, AI-generated notes (labelled). |
| `lookup_naics` | NAICS codes from words or a code prefix. |
| `search_awards` | Past federal contract and grant awards: awardee, amount, date, agency. |
| `get_company` | One firm's award history by UEI. |
| `assess_opportunity` | A bid/no-bid brief: days left, set-aside and NAICS fit, similar awards and who won them. |
| `find_teaming_partners` | Firms that won similar awards, ranked, each with a reason. |

The [main README](../../README.md#tools) describes each tool and its filters in full.

## Data and limits

The data is public US federal and state contract opportunities, grants, jobs, federal contract awards and grant
awards from [matchawards.com](https://matchawards.com). Each result links to its page on matchawards.com, and to
SAM.gov or USAspending where there is one. Award records may be incomplete, federal awards especially.

The hosted server is limited per client address to 30 requests a minute and 10 per 5 seconds. Over a limit the
tool fails with the server's message (how long to wait), marked retryable.

## License

[MIT](./LICENSE). The `skills/matchawards` SKILL.md is also published on ClawHub, which licenses skills MIT-0.
