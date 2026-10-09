# MatchAwards MCP server

<!-- mcp-name: io.github.matchawards/matchawards-mcp -->

*MatchAwards gives AI agents direct access to continuously updated government and business contracts, grants, jobs, awards and collaboration opportunities.*

This is the [Model Context Protocol](https://modelcontextprotocol.io) server for [MatchAwards](https://matchawards.com). It lets Claude, Cursor, VS Code and other MCP clients search US government contract opportunities, grants and jobs, and past contract and grant awards. It only reads data, and it needs no account and no API key.

## Tools

| Tool | What it returns | Example prompt |
|---|---|---|
| `search_contracts` | Federal and state contract opportunities. Filters: NAICS (up to 10 codes), state, keyword, set-aside, federal or state only, posted within N days, open only, agency (by name), 1 to 50 results, paging. | "Find open roofing contracts in Virginia posted in the last week." |
| `search_grants` | Federal grant and funding opportunities. Filters: NAICS, keyword, posted within N days, open only, 1 to 50 results, paging (grant notices carry no state). | "Are there open broadband grants?" |
| `search_jobs` | Job postings from the last 30 days. Filters: NAICS, state, keyword, posted within N days, 1 to 50 results, paging. | "Show electrician jobs in Texas from the last 7 days." |
| `get_opportunity` | The full record of one opportunity: the search row plus its description, published points of contact (name and title) and, where MatchAwards has them, AI-generated notes (`ai`: fit, reasoning, subcontracting and large-prime angles, summary), labelled as AI-generated. | "Who do I contact about the second result?" |
| `lookup_naics` | NAICS codes from words ("cybersecurity") or from a 2 to 6 digit code prefix, with each code's page on matchawards.com. 1 to 25 codes. | "What NAICS code covers cybersecurity consulting?" |
| `search_awards` | Past federal contract awards and grant awards: awardee (name, UEI, DUNS), amount, date, agency, NAICS, set-aside, place, solicitation, contract and CFDA numbers, links to matchawards.com and to SAM.gov or USAspending. Filters: type, NAICS, agency (by name), state, awardee name, UEI, keyword, awarded within N days (up to 1,095), 1 to 50 results, paging. | "Who won VA contracts under NAICS 541512 in the last year?" |
| `get_company` | One firm's award history by UEI: names, location (city, state, country), contract and grant totals, first and last award dates, top NAICS codes and agencies, the 10 most recent awards. No contact details. | "Show the award history of UEI C3KXNLTAAY98." |
| `assess_opportunity` | A qualification brief on one opportunity: days left (US Eastern), open or closed, set-aside and NAICS (and whether they match the user's `my_set_aside` and `my_naics`), the AI-generated notes, the 5 newest similar awards (same NAICS at the same agency over 3 years, or all agencies when that agency has none), the top 5 firms that won similar work (by count, then total), the quartiles of their award amounts and a coverage note. | "Should we bid on the first result? We're NAICS 336413, small business." |
| `find_teaming_partners` | Up to 10 firms that won similar awards over 3 years (same NAICS in the opportunity's state, then at its agency; the NAICS anywhere when neither has any), de-duplicated by UEI, ranked by count, then the most recent, then the total, each with a `why` line written from the records. `role` (`sub`, `prime`, `any`) puts smaller or larger typical awards first among firms with the same count. Public award data only: no contacts, no profile links. | "Who could we team with on that Air Force notice?" |

The `agency` filter takes a name. The server looks it up among MatchAwards' agencies (exact name first, then names that start with it), filters by the best match and says in the result's `note` which agency it used and what else matched; when nothing matches, it searches all agencies and says so. Lookups are cached for 10 minutes.

Award records may be incomplete (federal awards especially), so every award result says so. The two brief tools describe firms as having won similar work, never as the incumbent; they make at most 1 opportunity request and 6 award requests per call, within 6 seconds, and when either limit cuts the lookup short they answer with what they read and a `warning`. A grant award's title is its recipient's name, so the model is asked to head it with the awardee name and CFDA number instead.

Every tool is read-only and needs no account: each declares `readOnlyHint`, `idempotentHint`, a title and `securitySchemes: [{"type": "noauth"}]` (in `_meta`). Results are structured: each tool publishes an `outputSchema` and returns `structuredContent`, with the same JSON as text for clients that read only text. Every result carries a `url` on matchawards.com (and a `sam_url` for federal notices), and the server asks the model to show them as markdown links.

### Changes in 1.0

- `search_by_naics` is removed: `search_contracts`, `search_grants` and `search_jobs` take the same `naics` filter on its own.
- `find_contacts` is removed: `get_opportunity` returns the published contacts with the rest of the record.
- Results are structured output (`outputSchema` plus `structuredContent`). Fields the API adds later appear in results only once the schema declares them. `get_opportunity` returns `ai` (AI-generated notes, labelled as such) where MatchAwards has them, else null.
- Tool annotations are closed-world (`openWorldHint: false`): the tools read the MatchAwards catalog only.
- New tools: `lookup_naics`, `search_awards`, `get_company`, `assess_opportunity` and `find_teaming_partners`; `search_contracts` takes an `agency` name.

## Cards

Hosts that render [MCP Apps](https://modelcontextprotocol.io/extensions/apps) (ChatGPT, Claude) can show `search_contracts` and `search_grants` results as an inline card: up to 8 opportunities with title, agency, place, a federal/state/grant badge, the deadline (days left, closed, or no fixed deadline), set-aside and NAICS code, plus any `warning`. Clicking a row asks the model for that opportunity's details; each row's "Open on MatchAwards" link opens its page. The card is one self-contained HTML resource, `ui://matchawards/results-v1.html` (`text/html;profile=mcp-app`): no scripts or styles from elsewhere and no network requests, so its CSP allows nothing. It renders only the tool's `structuredContent`, and never calls a tool itself.

Cards cover contract and grant search for now; the other tools return their structured results without a card.

Cards are off by default. Set `MATCHAWARDS_WIDGETS=1` to list the resource and point the two search tools at it (`_meta.ui.resourceUri`); tool results are the same either way. `tests/widget_harness/` holds a minimal host page with sample results for checking the card in a browser.

## Hosted

A hosted endpoint runs at `https://matchawards.com/mcp`. Add it as a remote MCP server (connector) by URL, with nothing to install and no key:

- **ChatGPT**: add a custom connector with the URL `https://matchawards.com/mcp`.
- **Claude** (claude.ai or Claude Desktop): Settings, Connectors, add a custom connector with the same URL. Claude Code: `claude mcp add --transport http matchawards https://matchawards.com/mcp`.
- **Cursor**: in `mcp.json`, `{"mcpServers": {"matchawards": {"url": "https://matchawards.com/mcp"}}}`.

The hosted endpoint serves the same tools. It is limited per client address (IPv6: per /64) to 30 requests per minute and 10 per 5 seconds, per IPv6 /48 to 120 per minute, and to 600 per minute for all clients together; over a limit it answers HTTP 429 with `Retry-After`.

## Install

The server runs with [`uvx`](https://docs.astral.sh/uv/), which comes with uv.

### Claude Desktop

Add this to `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`, Windows: `%APPDATA%\Claude\claude_desktop_config.json`), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "matchawards": {
      "command": "uvx",
      "args": ["matchawards-mcp"]
    }
  }
}
```

### Claude Code

```bash
claude mcp add matchawards -- uvx matchawards-mcp
```

### Cursor

Add this to `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project):

```json
{
  "mcpServers": {
    "matchawards": {
      "command": "uvx",
      "args": ["matchawards-mcp"]
    }
  }
}
```

### VS Code

Add this to `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "matchawards": {
      "type": "stdio",
      "command": "uvx",
      "args": ["matchawards-mcp"]
    }
  }
}
```

### Other clients

Any MCP client that can start a stdio server works. The command is `uvx matchawards-mcp`.

### Run the HTTP server yourself

`matchawards-mcp --http` serves the same tools over stateless Streamable HTTP (JSON replies, no sessions, no SSE) at `http://127.0.0.1:8765/mcp`:

```bash
uvx matchawards-mcp --http
curl -s http://127.0.0.1:8765/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -H 'MCP-Protocol-Version: 2025-06-18' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

How requests are handled, in order:

- Repeated and trailing slashes are folded, so `/mcp/` and `//mcp` count as `/mcp`. Any path other than `/mcp` and `/healthz` gets 404.
- `GET /healthz` returns `ok` and is not rate-limited.
- A `Host` header not in `MATCHAWARDS_ALLOWED_HOSTS` (or `127.0.0.1`, `localhost`) gets 421, whatever the method.
- Only `POST /mcp` is served. `GET`, `DELETE` and `OPTIONS` get 405 (no SSE stream, no sessions, no CORS).
- A POST needs `Content-Length` (411 without it), and bodies over 64 KB get 413.
- Rate limit per client (per ChatGPT user from OpenAI's addresses), then the server-wide cap: 429 with `Retry-After`.

The client is the socket address, or the `X-Real-IP` header when the socket address is in `MATCHAWARDS_TRUSTED_PROXIES` (IPv6 is keyed per /64; all /64s inside one /48 also share `MATCHAWARDS_RATE_PER_PREFIX_MIN`). Run it behind a reverse proxy that sets `X-Real-IP`, and do not expose the port directly. The limits are kept in memory per process. A bad value in any of the variables below stops the server at startup. The log has one line per request (method, path, status, duration, a hashed client key), never headers or bodies.

ChatGPT calls come from OpenAI's shared egress addresses ([published list](https://openai.com/chatgpt-connectors.json), bundled with the package and refreshed daily; a failed refresh keeps the last good list), so one address carries many users and a per-IP limit would throttle them together. From those addresses only, a request is limited per `x-openai-subject` (ChatGPT's per-user id, kept only as a keyed hash) and all ChatGPT traffic shares `MATCHAWARDS_EGRESS_PER_MIN`; a request without the header counts in that pool only. The header from any other address is ignored, and the User-Agent is never used.

In Docker, nginx on the host reaches the container from the compose network's gateway, not from loopback, so name that address exactly. Read it with `docker network inspect <network> --format '{{(index .IPAM.Config 0).Gateway}}'` and set, for example:

```bash
MATCHAWARDS_HTTP_HOST=0.0.0.0
MATCHAWARDS_TRUSTED_PROXIES=127.0.0.1/32,::1/128,172.18.0.1/32
```

Do not trust a broad private range such as `172.16.0.0/12`: it may overlap your internal network, and any host in it could then pick its own rate-limit key.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MATCHAWARDS_API_BASE` | `https://matchawards.com` | API base URL. Change it only for development. |
| `MATCHAWARDS_HTTP_HOST` | `127.0.0.1` | `--http` only: address to listen on. |
| `MATCHAWARDS_HTTP_PORT` | `8765` | `--http` only: port to listen on. |
| `MATCHAWARDS_RATE_PER_MIN` | `30` | `--http` only: requests per minute per client. |
| `MATCHAWARDS_RATE_BURST` | `10` | `--http` only: requests per 5 seconds per client. |
| `MATCHAWARDS_RATE_PER_PREFIX_MIN` | `120` | `--http` only: requests per minute for all IPv6 /64s inside one /48 together. |
| `MATCHAWARDS_GLOBAL_PER_MIN` | `1200` | `--http` only: requests per minute for all clients together. Must be above `MATCHAWARDS_EGRESS_PER_MIN` (otherwise the server will not start). |
| `MATCHAWARDS_RATE_PER_SUBJECT_MIN` | `30` | `--http` only: requests per minute per ChatGPT user (`x-openai-subject`, from OpenAI's egress addresses only). |
| `MATCHAWARDS_RATE_SUBJECT_BURST` | `10` | `--http` only: requests per 5 seconds per ChatGPT user. |
| `MATCHAWARDS_EGRESS_PER_MIN` | `600` | `--http` only: requests per minute for all ChatGPT traffic together. |
| `MATCHAWARDS_GLOBAL_API_PER_MIN` | `900` | `--http` only: API calls per minute for the whole server (one search can make up to 3). Over it, a tool answers "MatchAwards is busy, retry in N seconds" without calling the API. |
| `MATCHAWARDS_TRUSTED_PROXIES` | `127.0.0.1/32,::1/128` | `--http` only: proxies (CIDRs, comma-separated) whose `X-Real-IP` header is trusted. Loopback only by default. |
| `MATCHAWARDS_ALLOWED_HOSTS` | `matchawards.com,staging.matchawards.com` | `--http` only: accepted `Host` headers, comma-separated (`127.0.0.1` and `localhost` are always accepted). |
| `MATCHAWARDS_USAGE_DB` | `/data/usage.db` | `--http` only: SQLite file for the usage stats. If its directory is missing or not writable, the server logs one warning and records nothing. |
| `MATCHAWARDS_USAGE_SALT` | unset | `--http` only: secret for the ChatGPT caller fingerprint. Unset: no fingerprint is stored. |
| `MATCHAWARDS_METRICS_TOKEN` | unset | `--http` only: Bearer token for `/metrics`. Unset: the metrics listener does not start. |
| `MATCHAWARDS_METRICS_HOST` | `127.0.0.1` | `--http` only: address of the metrics listener. |
| `MATCHAWARDS_METRICS_PORT` | `9765` | `--http` only: port of the metrics listener. |
| `MATCHAWARDS_WIDGETS` | unset | `1`: serve the result card (see Cards). Unset: no card resource, no `_meta.ui` on the tools. |

## Usage stats (hosted mode)

With `--http` the server keeps usage stats in a SQLite file (`MATCHAWARDS_USAGE_DB`, WAL mode). stdio mode records nothing. Writes run on two background threads and are best-effort: a failed write is counted in `mcp_ledger_errors_total` and never fails a request. Rows older than 365 days are deleted, at most once a day.

What is recorded:

- **Tool calls**: one row per call that reached a tool: time, tool name, outcome (`ok`, `invalid_input`, `upstream_error`, `bad_response` (a reply the tools cannot read), `upstream_timeout` (the API ran out of time; retry), `partial` (`assess_opportunity` or `find_teaming_partners` answered, but its request or time limit cut the award lookup short), `rate_limited_upstream`, `busy`, `internal_error`), the client network (IPv4 /24 or IPv6 /48), the User-Agent (first 200 characters), its family (`openai-mcp`, `claude-user`, `claude`, `cursor`, ..., `prober` or `other`) and the latency. Calls stopped by the rate limit are counted in a metric, with no row. Arguments the SDK rejects before the tool runs leave no row either; they still count in the transport table as `tools/call`.
- **Transport, per day**: one counter per client network, JSON-RPC method (known MCP methods only, anything else is `_other`) and `clientInfo` name and version, with the last User-Agent seen. `clientInfo` is read from the first 8 KiB of an `initialize` body, or from `params._meta` (2026-07-28 clients), as the body streams past; the body is not buffered. At most 50 counters per client network and day; past that they fold into `_other`.
- **ChatGPT callers**: when `MATCHAWARDS_USAGE_SALT` is set and a request carries `x-openai-subject` (ChatGPT's anonymous per-user id), the tool-call row stores a 16-hex HMAC-SHA256 of it, to count distinct ChatGPT callers (`openai-mcp` family only in the stats). `x-openai-session` is not used: it changes per conversation.

Never recorded: tool arguments (search terms, ids), request or response bodies, full client addresses, raw header values.

Read the stats with:

```bash
matchawards-mcp stats --days 7          # --db PATH to read another file
```

It prints tool calls per tool and outcome, calls per family, real calls vs probers vs our own tests (`matchawards-test/` UA), distinct client networks (probers and own tests left out), distinct ChatGPT fingerprints, transport requests per method and the top `clientInfo` names. In Docker: `docker exec <container> matchawards-mcp stats --days 1`.

### Metrics

When `MATCHAWARDS_METRICS_TOKEN` is set, a second listener in the same process (`MATCHAWARDS_METRICS_HOST:MATCHAWARDS_METRICS_PORT`, default `127.0.0.1:9765`) serves `GET /metrics` in the Prometheus text format, with `Authorization: Bearer <token>` (401 without it). The `/mcp` port never serves `/metrics` (404): it trusts `X-Real-IP` from the proxy, so publish only the metrics port to the network Prometheus scrapes from. Counters (in memory, reset on restart): `mcp_tool_calls_total{tool,outcome}`, `mcp_requests_total{method}`, `mcp_rejected_total{reason}` (`rate_limit_client`, `rate_limit_prefix`, `rate_limit_subject`, `rate_limit_egress`, `rate_limit_global`, `size`, `host`, `path`, `method`), `mcp_agent_requests_total{family}`, `mcp_ledger_errors_total`.

### Ops notes

- Mount a volume at `/data` (or set `MATCHAWARDS_USAGE_DB`) so the file survives a container rebuild.
- For ChatGPT fingerprints, nginx must pass `x-openai-subject` through to the container (it does by default unless headers are cleared), and `MATCHAWARDS_USAGE_SALT` must be set and kept stable: a new salt starts new fingerprints.
- For Docker, set `MATCHAWARDS_METRICS_HOST=0.0.0.0` and publish the metrics port only on the internal network.

## Data

The data is US federal and state contract opportunities, grants, jobs, federal contract awards and grant awards from [matchawards.com](https://matchawards.com). Only public posts are returned, and each result links back to its page on matchawards.com.

- Contract and grant searches return open items by default: a response deadline of today or later, US Eastern. An item with no deadline counts as closed.
- Job search covers the last 30 days.
- Results come newest first. When `has_more` is true, the model can ask for the next page with `cursor`.
- A narrow filter (an agency, a rare name) can scan the newest records without a match while more exist further back; the result's `warning` then says so and gives the `cursor` to continue with.
- If the first request of a search fails, the tool returns an error. If a later page fails, the tool returns the rows it already has, with a `warning` that says why it stopped.
- A result row in an unexpected format is left out and counted in `warning`; the rest of the search is returned.
- Each API call times out after 10 seconds.
- The API is rate-limited per IP: 60 requests per minute, plus a burst limit of 20 requests per 5 seconds. One search call can use up to three requests when the filters are narrow and results are sparse, plus one to look up an agency name (cached for 10 minutes). When the limit is reached, the tool returns an error that tells the model how many seconds to wait.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```

The tests make no network calls.

`tests/golden/prompts.json` lists prompts with the tool a model should pick (or none), for manual checks of tool selection in a client.

## License

MIT. See [LICENSE](LICENSE).
