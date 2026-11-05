"""UTM parameters on the matchawards.com links in tool results, so the site can tell which AI host a visit (and a
sign-up) came from.

Every link to matchawards.com or one of its subdomains (www, staging) gets utm_source (the AI host, a fixed label
from the caller's User-Agent family), utm_medium=ai_agent and utm_campaign=matchawards_mcp. Links to other sites
(SAM.gov, USAspending) are left as they are. The output models apply `tagged` to their matchawards.com URL fields
(schemas.SiteUrl), so every row built while a tool call runs carries the parameters.

Go-links: with MATCHAWARDS_GO_LINKS=1, a record's source link (an opportunity's sam_url, an award's source_url) is
given as https://matchawards.com/go/<utm_source>/<record id> instead, which redirects to that record's stored source
link and counts the hand-off. The site looks the target up itself, so the link carries no URL. A record without a
numeric MatchAwards id or a matchawards.com page keeps its plain link.
"""

import contextvars
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MEDIUM = "ai_agent"
CAMPAIGN = "matchawards_mcp"
DEFAULT_SOURCE = "mcp"  # stdio, an unknown or test client, or no request at all

# The site: matchawards.com and its subdomains (www, staging). Not the API base host: the server may reach the API
# through an internal address while the links in its answers name the public site.
SITE_DOMAIN = "matchawards.com"

# Off until the site serves /go/<via>/<id>.
GO_LINKS = os.environ.get("MATCHAWARDS_GO_LINKS", "0").strip() == "1"

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


def is_site(host: str | None) -> bool:
    """Whether `host` is the site: matchawards.com or one of its subdomains."""
    return bool(host) and (host == SITE_DOMAIN or host.endswith("." + SITE_DOMAIN))


def plain_netloc(netloc: str) -> bool:
    """Whether a URL's netloc is a plain host (letters, digits, dots, hyphens) with an optional port: nothing a browser
    and urlsplit could read as different hosts (no backslash, userinfo, percent-encoding or brackets)."""
    return bool(re.fullmatch(r"[A-Za-z0-9.-]+(:[0-9]{1,5})?", netloc))


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
    if parts.scheme not in ("http", "https") or "\\" in url or not plain_netloc(parts.netloc) or not is_site(host):
        return url
    if any(k.lower().startswith("utm_") for k, _ in parse_qsl(parts.query, keep_blank_values=True)):
        return url
    utm = urlencode({"utm_source": source.get(), "utm_medium": MEDIUM, "utm_campaign": CAMPAIGN})
    return urlunsplit(parts._replace(query=f"{parts.query}&{utm}" if parts.query else utm))


def go(record_id: str | None, page_url: str | None, url: str | None) -> str | None:
    """`url`, a record's third-party source link, as its go-link on the site when go-links are on: the scheme and host
    of `page_url` (the record's own matchawards.com page, so a staging record gets a staging go-link), then
    /go/<utm_source>/<record_id>, tagged like any site link. The plain `url` when go-links are off, the id is not a
    number, the page is not on the site, or `url` already points to the site. None or "" gives None."""
    if not url:
        return None
    if not GO_LINKS or not isinstance(record_id, str) or not re.fullmatch(r"[0-9]{1,19}", record_id) \
            or int(record_id) >= 2**63:  # the site's status ids: below 2**63
        return url
    try:
        page, target = urlsplit(page_url or ""), urlsplit(url)
        page_host = page.hostname
        if is_site(target.hostname):
            return url
    except ValueError:
        return url
    if page.scheme != "https" or "\\" in (page_url or "") or not plain_netloc(page.netloc) or not is_site(page_host):
        return url
    return tagged(f"https://{page.netloc}/go/{source.get()}/{record_id}")
