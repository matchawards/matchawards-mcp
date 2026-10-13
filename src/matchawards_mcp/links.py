"""UTM parameters on the matchawards.com links in tool results, so the site can tell which AI host a visit (and a
sign-up) came from.

Every link to the site the API points at gets utm_source (the AI host, a fixed label from the caller's User-Agent
family), utm_medium=ai_agent and utm_campaign=matchawards_mcp. Links to other sites (SAM.gov, USAspending) are left
as they are. The output models apply `tagged` to their matchawards.com URL fields (schemas.SiteUrl), so every row
built while a tool call runs carries the parameters.
"""

import contextvars
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MEDIUM = "ai_agent"
CAMPAIGN = "matchawards_mcp"
DEFAULT_SOURCE = "mcp"  # stdio, an unknown or test client, or no request at all

# The site's own hosts: matchawards.com (with www) and the host of the API base (a staging API returns staging
# links). Read from the same setting as api.API_BASE (api.py imports the output models, so it cannot be imported
# from here).
_API_HOST = urlsplit(os.environ.get("MATCHAWARDS_API_BASE", "https://matchawards.com")).hostname
SITE_HOSTS = frozenset(h for h in ("matchawards.com", "www.matchawards.com", _API_HOST) if h)

# utm_source for the request being served (HTTP mode sets it per request, see server.HttpGuard).
source: contextvars.ContextVar[str] = contextvars.ContextVar("matchawards_utm_source", default=DEFAULT_SOURCE)


def utm_source(family: str | None) -> str:
    """The utm_source label for a User-Agent family (usage.classify_ua). A fixed set, so a caller cannot mint its
    own value: chatgpt, claude, cursor, vscode, n8n or mcp."""
    family = family or ""
    if family == "openai-mcp":
        return "chatgpt"
    if family.startswith("claude"):
        return "claude"
    if family in ("cursor", "vscode"):
        return family
    if family.startswith("n8n"):
        return "n8n"
    return DEFAULT_SOURCE


def tagged(url: str | None) -> str | None:
    """`url` with the UTM parameters added when it is a link to the site; any other URL unchanged. Its query and
    fragment are kept, and a URL that already has a utm_* parameter is left as it is. None or "" gives None."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:  # e.g. a malformed IPv6 host
        return url
    if parts.scheme not in ("http", "https") or host not in SITE_HOSTS:
        return url
    if any(k.lower().startswith("utm_") for k, _ in parse_qsl(parts.query, keep_blank_values=True)):
        return url
    utm = urlencode({"utm_source": source.get(), "utm_medium": MEDIUM, "utm_campaign": CAMPAIGN})
    return urlunsplit(parts._replace(query=f"{parts.query}&{utm}" if parts.query else utm))
