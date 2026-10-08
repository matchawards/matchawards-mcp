# MatchAwards MCP server

<!-- mcp-name: io.github.matchawards/matchawards-mcp -->

*MatchAwards gives AI agents direct access to continuously updated government and business contracts, grants, jobs, awards and collaboration opportunities.*

This is the [Model Context Protocol](https://modelcontextprotocol.io) server for [MatchAwards](https://matchawards.com). It lets Claude, Cursor, VS Code and other MCP clients search US government contract opportunities, grants and jobs. It only reads data, and it needs no account and no API key.

## Tools

| Tool | What it returns | Example prompt |
|---|---|---|
| `search_contracts` | Federal and state contract opportunities. Filters: NAICS, state, keyword, set-aside, federal or state only, posted within N days, open only, paging. | "Find open roofing contracts in Virginia posted in the last week." |
| `search_grants` | Federal grant and funding opportunities. Filters: NAICS, keyword, posted within N days, open only, paging (grant notices carry no state). | "Are there open broadband grants?" |
| `search_jobs` | Job postings from the last 30 days. Filters: NAICS, state, keyword, posted within N days, paging. | "Show electrician jobs in Texas from the last 7 days." |
| `search_by_naics` | The newest opportunities for up to 10 NAICS codes, of one type: contract, federal, state, grant or job. | "What is new for NAICS 541511 and 541512 at the federal level?" |
| `get_opportunity` | The full record of one opportunity: description, deadline, agency and contacts (name and title). | "Tell me more about the second result." |
| `find_contacts` | Only the published points of contact for one opportunity. | "Who do I contact about that solicitation?" |

Every result carries a `url` on matchawards.com, and the server asks the model to show it to you as a link.

## Hosted (coming soon)

<!-- Release note: 0.2.0 ships without a server.json "remotes" entry. Add it in 0.2.1, once https://matchawards.com/mcp
     is live: "remotes": [{"type": "streamable-http", "url": "https://matchawards.com/mcp"}] -->

A hosted endpoint is planned at `https://matchawards.com/mcp`. It is **not live yet**. Once it is, you add it as a remote MCP server (connector) by URL, with nothing to install and no key:

- **ChatGPT**: add a custom connector with the URL `https://matchawards.com/mcp`.
- **Claude** (claude.ai or Claude Desktop): Settings, Connectors, add a custom connector with the same URL. Claude Code: `claude mcp add --transport http matchawards https://matchawards.com/mcp`.
- **Cursor**: in `mcp.json`, `{"mcpServers": {"matchawards": {"url": "https://matchawards.com/mcp"}}}`.

The hosted endpoint serves the same six tools. It is limited per client address (IPv6: per /64) to 30 requests per minute and 10 per 5 seconds, per IPv6 /48 to 120 per minute, and to 600 per minute for all clients together; over a limit it answers HTTP 429 with `Retry-After`.

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
- Rate limit per client, then the server-wide cap: 429 with `Retry-After`.

The client is the socket address, or the `X-Real-IP` header when the socket address is in `MATCHAWARDS_TRUSTED_PROXIES` (IPv6 is keyed per /64; all /64s inside one /48 also share `MATCHAWARDS_RATE_PER_PREFIX_MIN`). Run it behind a reverse proxy that sets `X-Real-IP`, and do not expose the port directly. The limits are kept in memory per process. A bad value in any of the variables below stops the server at startup. The log has one line per request (method, path, status, duration, a hashed client key), never headers or bodies.

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
| `MATCHAWARDS_GLOBAL_PER_MIN` | `600` | `--http` only: requests per minute for all clients together. |
| `MATCHAWARDS_GLOBAL_API_PER_MIN` | `900` | `--http` only: API calls per minute for the whole server (one search can make up to 3). Over it, a tool answers "MatchAwards is busy, retry in N seconds" without calling the API. |
| `MATCHAWARDS_TRUSTED_PROXIES` | `127.0.0.1/32,::1/128` | `--http` only: proxies (CIDRs, comma-separated) whose `X-Real-IP` header is trusted. Loopback only by default. |
| `MATCHAWARDS_ALLOWED_HOSTS` | `matchawards.com,staging.matchawards.com` | `--http` only: accepted `Host` headers, comma-separated (`127.0.0.1` and `localhost` are always accepted). |

## Data

The data is US federal and state contract opportunities, grants and jobs from [matchawards.com](https://matchawards.com). Only public posts are returned, and each result links back to its page on matchawards.com.

- Contract and grant searches return open items by default: a response deadline of today or later, US Eastern. An item with no deadline counts as closed.
- Job search covers the last 30 days.
- Results come newest first. When `has_more` is true, the model can ask for the next page with `cursor`.
- If the first request of a search fails, the tool returns an error. If a later page fails, the tool returns the rows it already has, with a `warning` that says why it stopped.
- Each API call times out after 10 seconds.
- The API is rate-limited per IP: 60 requests per minute, plus a burst limit of 20 requests per 5 seconds. One search call can use up to three requests when the filters are narrow and results are sparse. When the limit is reached, the tool returns an error that tells the model how many seconds to wait.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```

The tests make no network calls.

## License

MIT. See [LICENSE](LICENSE).
