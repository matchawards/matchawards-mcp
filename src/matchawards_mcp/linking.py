"""Account linking (OAuth 2.1), off unless MATCHAWARDS_AUTH_ISSUER is set: the protected-resource metadata, the
bearer check and the challenge a host starts linking on.

matchawards.com is the authorization server; this server is only a resource server. It never sees a password or
an authorization code, and keeps no token: it checks one by calling `GET {MATCHAWARDS_AGENT_API_BASE}/api/agent/v1/me`
with it, and keeps a good answer in memory for CACHE_TTL_S (a token /me rejects for INVALID_TTL_S) under a keyed hash
of the token (the key is random per process, so the hash means nothing outside it). The token, the Authorization
header and that hash are never logged, stored or echoed.

Only a tools/call of a LINKED_TOOLS tool is checked; everything else (initialize, tools/list, the other tools) never
is. A host links on one of two challenge shapes, both naming the same metadata: an HTTP 401 (403 for a missing
scope) with WWW-Authenticate for Claude and other MCP clients (server.HttpGuard sends it before the MCP layer), and
for ChatGPT, which reads a 401 as a failed call, a tool result with isError and _meta["mcp/www_authenticate"]
(challenge_result, returned by the tool itself). An account /me refuses outright (403 forbidden) or /me not answering
is never a challenge: the call goes on and the tool says so.

Off, nothing here runs: no metadata route, no body read, no /me call, and the tool list is the one without linking.
"""

import contextvars
import hashlib
import itertools
import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp.types import CallToolResult, TextContent
from starlette.responses import JSONResponse

from . import api, links

ISSUER = os.environ.get("MATCHAWARDS_AUTH_ISSUER", "").strip().rstrip("/")  # "" = linking off
RESOURCE = os.environ.get("MATCHAWARDS_RESOURCE", "").strip()  # "" = {ISSUER}/mcp
# /api/agent/v1 lives in the same app as the public API, so by default it is the same base.
AGENT_API_BASE = os.environ.get("MATCHAWARDS_AGENT_API_BASE", "").strip().rstrip("/") or api.API_BASE
SCOPE = "profile:read"
ME_PATH = "/api/agent/v1/me"
METADATA_PATH = "/.well-known/oauth-protected-resource"
CACHE_TTL_S = 60
INVALID_TTL_S = 30  # a token /me rejected, so a client repeating it is not checked on every call
CACHE_MAX = 10_000  # per cache (profiles, rejected tokens)

# The four ways a check can fail. invalid_token and insufficient_scope are the RFC 6750 error codes the challenge
# carries; forbidden (an account that may not use linked tools: disabled, suspended, or the token's app is not an
# agent app) and unavailable (MatchAwards down, slow or answering nonsense) are never a challenge: asking the user to
# link again would not help, and a host that links on every challenge would loop.
INVALID = "invalid_token"
INSUFFICIENT = "insufficient_scope"
FORBIDDEN = "forbidden"
UNAVAILABLE = "unavailable"
Refusal = Literal["invalid_token", "insufficient_scope", "forbidden", "unavailable"]

log = logging.getLogger("matchawards_mcp.linking")

# The tools that need a linked account. A tools/call of one is checked (and challenged) before the MCP layer.
LINKED_TOOLS = frozenset({"my_matches"})
DESCRIPTIONS = {
    INVALID: "This tool needs a linked MatchAwards account.",
    INSUFFICIENT: f"The linked MatchAwards account has not granted {SCOPE}.",
}

# Its own client: same User-Agent and timeout as api.client, but it keeps no cookies, so nothing one user's /me
# answer sets can ride along on another user's request. Tests swap it for one on a MockTransport.
client = httpx.AsyncClient(
    headers={"User-Agent": api.USER_AGENT, "Accept": "application/json"},
    timeout=api.TIMEOUT_S,
    cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
)


@dataclass(frozen=True)
class Profile:
    """The linked account's matching profile, from /me. Every field is checked and cleaned before it is kept."""

    sub: str  # the account's opaque, stable id (32 hex)
    organization: str | None
    naics: tuple[str, ...]  # six-digit codes, in profile order
    naics_truncated: bool  # the profile has more codes than /me lists
    set_aside: str | None  # a code such as SBA
    set_aside_name: str | None
    state: str | None
    uei: str | None
    match_settings_url: str | None  # an https link to the match settings on the site


def enabled() -> bool:
    return bool(ISSUER)


def resource() -> str:
    """The resource this server is (the audience of a token issued for it): MATCHAWARDS_RESOURCE or {issuer}/mcp."""
    return RESOURCE or f"{ISSUER}/mcp"


def metadata_url() -> str:
    """Where the resource's metadata is, per RFC 9728 §3.1: the well-known path inserted before the resource's path
    ('https://matchawards.com/mcp' -> 'https://matchawards.com/.well-known/oauth-protected-resource/mcp')."""
    parts = urlsplit(resource())
    return urlunsplit((parts.scheme, parts.netloc, METADATA_PATH + parts.path.rstrip("/"), "", ""))


def metadata_paths() -> set[str]:
    """The paths the metadata is served at: the bare well-known path and the one metadata_url() names."""
    return {METADATA_PATH, urlsplit(metadata_url()).path}


def metadata() -> dict:
    """The protected-resource metadata document (RFC 9728). matchawards.com serves the same one; they must match."""
    return {
        "resource": resource(),
        "authorization_servers": [ISSUER],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "MatchAwards",
    }


LOOPBACK = ("localhost", "127.0.0.1", "::1")


def check_settings() -> None:
    """Stop --http at startup on a bad URL: the issuer and the resource are public URLs a host fetches, so https
    (http only on a loopback host, for a local run); the agent API base is usually an internal address, so http or
    https. All need a host and a valid port, and no query or user:password@."""
    if not enabled():
        return
    for name, value, public in (("MATCHAWARDS_AUTH_ISSUER", ISSUER, True), ("MATCHAWARDS_RESOURCE", resource(), True),
                                ("MATCHAWARDS_AGENT_API_BASE", AGENT_API_BASE, False)):
        try:
            parts = urlsplit(value)
            http_ok = not public or parts.hostname in LOOPBACK
            parts.port  # raises ValueError when not a number from 0 to 65535
            ok = (parts.scheme == "https" or (parts.scheme == "http" and http_ok)) and bool(parts.hostname) \
                and not parts.query and not parts.fragment and "@" not in parts.netloc
        except ValueError:
            ok = False
        if not ok:
            kind = "an https URL (http only on localhost)" if public else "an http(s) URL"
            raise SystemExit(f"{name} must be {kind} with a host and no query or user, got {value!r}")


# RFC 6750 b64token. Anything else in the header is not a token we would send on.
_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*")
TOKEN_MAX_LEN = 4096


def bearer(scope) -> str | None:
    """The bearer token of an ASGI request (the first Authorization header), or None when there is none or it is
    not a well-formed bearer token."""
    raw = next((v for k, v in scope["headers"] if k == b"authorization"), b"").decode("latin-1")
    kind, _, token = raw.strip().partition(" ")
    token = token.strip()
    if kind.lower() != "bearer" or len(token) > TOKEN_MAX_LEN or not _TOKEN.fullmatch(token):
        return None
    return token


def is_openai_host(user_agent: str | None) -> bool:
    """ChatGPT and Codex (User-Agent openai-mcp/...): they link on the tool-result challenge, not on a 401."""
    return (user_agent or "").lower().startswith("openai-mcp")


def linked_tool(body: bytes) -> str | None:
    """The tool name when a JSON-RPC body is a tools/call of a LINKED_TOOLS tool, else None (any other method or
    tool, a batch, or a body that is not JSON)."""
    try:
        msg = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    params = msg.get("params") if isinstance(msg, dict) and msg.get("method") == "tools/call" else None
    name = params.get("name") if isinstance(params, dict) else None
    return name if isinstance(name, str) and name in LINKED_TOOLS else None


def _quote(value: str) -> str:
    """An auth-param value as an RFC 9110 quoted-string: backslash and double quote escaped."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def challenge_header(error: str, description: str) -> str:
    """The WWW-Authenticate value of a challenge (a 401 or 403, or ChatGPT's tool-result form). It names the
    metadata and the scope a host links for; it never carries anything from the request."""
    return (f'Bearer realm="matchawards", resource_metadata="{_quote(metadata_url())}", scope="{_quote(SCOPE)}", '
            f'error="{_quote(error)}", error_description="{_quote(description)}"')


def challenge_response(error: str) -> JSONResponse:
    """The HTTP challenge: 401 for no or a bad token (INVALID), 403 for a token without SCOPE (INSUFFICIENT)."""
    return JSONResponse(
        {"error": error, "error_description": DESCRIPTIONS[error]},
        status_code=403 if error == INSUFFICIENT else 401,
        headers={"WWW-Authenticate": challenge_header(error, DESCRIPTIONS[error]), "Cache-Control": "no-store"},
    )


def challenge_result(error: str, text: str) -> CallToolResult:
    """The challenge as a tool result, for ChatGPT: isError, `text` for the model (say what linking gives the
    user), and the WWW-Authenticate value in _meta["mcp/www_authenticate"].

    A linked tool returns it when auth.get() is INVALID or INSUFFICIENT. To keep its outputSchema, the tool's return
    annotation is Annotated[CallToolResult, <its output model>]: the SDK takes the schema from the model, validates a
    returned model as before, and passes an error result through unvalidated."""
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        is_error=True,
        _meta={"mcp/www_authenticate": [challenge_header(error, DESCRIPTIONS[error])]},
    )


# --- The token check ------------------------------------------------------------------------------------------

_SALT = os.urandom(16)
clock = time.monotonic  # tests move it
# keyed token hash -> (expiry, answer), least recently used first. Two caches, so a flood of bad tokens cannot push
# out the good ones.
_cache: OrderedDict[bytes, tuple[float, Profile]] = OrderedDict()
_invalid: OrderedDict[bytes, tuple[float, str]] = OrderedDict()
# keyed token hash -> when /me last refused it (a _checks number), so an answer to an older check never caches a
# profile over a newer refusal. Bounded like the caches.
_refused: OrderedDict[bytes, int] = OrderedDict()
_checks = itertools.count()  # orders the /me checks and refusals of this process
_refused_floor = -1  # the newest refusal _refused dropped for room: a check started before it is not cached


def _key(token: str) -> bytes:
    return hashlib.blake2s(token.encode("ascii"), key=_SALT, digest_size=16).digest()


def _cached(cache: OrderedDict, key: bytes):
    """The answer kept under `key` while it has not expired, else None."""
    hit = cache.get(key)
    if hit is not None:
        if hit[0] > clock():
            cache.move_to_end(key)
            return hit[1]
        del cache[key]
    return None


def _keep(cache: OrderedDict, key: bytes, ttl: float, answer) -> None:
    cache[key] = (clock() + ttl, answer)
    cache.move_to_end(key)
    while len(cache) > CACHE_MAX:
        cache.popitem(last=False)  # the least recently used


def _text(value, pattern: str) -> str | None:
    """A string field that fully matches `pattern` (after trimming), or None."""
    return value.strip() if isinstance(value, str) and re.fullmatch(pattern, value.strip()) else None


def _site_url(value) -> str | None:
    """An https link to the site (matchawards.com or a subdomain, as links.tagged knows it) with no user:password@,
    no port other than 443 and no space or control character, at most 500 characters; else None."""
    if not isinstance(value, str) or len(value) > 500 or any(c <= " " or c == "\x7f" for c in value):
        return None
    try:
        parts = urlsplit(value)
        ok = parts.scheme == "https" and links.is_site(parts.hostname) and parts.port in (None, 443) \
            and "@" not in parts.netloc
    except ValueError:  # e.g. a port that is not a number
        return None
    return value if ok else None


def parse_profile(data) -> Profile | None:
    """A Profile from a /me body, or None when it is not one (sub not 32 lowercase hex, naics not a list); me()
    checks its resource and expiry. Fields in the wrong shape are dropped (None), and only six-digit NAICS codes are
    kept, so nothing from the body reaches a search filter or a result unchecked."""
    if not isinstance(data, dict):
        return None
    sub = data.get("sub")
    if not isinstance(sub, str) or not re.fullmatch(r"[0-9a-f]{32}", sub) or not isinstance(data.get("naics"), list):
        return None
    codes = (n.get("code") for n in data["naics"] if isinstance(n, dict))
    naics = dict.fromkeys(c.strip() for c in codes if isinstance(c, str) and re.fullmatch(r"[0-9]{6}", c.strip()))
    set_aside = data.get("set_aside") if isinstance(data.get("set_aside"), dict) else {}
    code = _text(set_aside.get("code"), r"[A-Za-z0-9_-]{1,40}")
    name = set_aside.get("name") if code else None
    state = _text(data.get("state"), r"[A-Za-z]{2}")
    org = data.get("organization_name")
    return Profile(
        sub=sub,
        organization=(api.clean(org, 200) or None) if isinstance(org, str) else None,
        naics=tuple(naics),
        naics_truncated=data.get("naics_truncated") is True,
        set_aside=code,
        set_aside_name=(api.clean(name, 100) or None) if isinstance(name, str) else None,
        state=state.upper() if state else None,
        uei=_text(data.get("uei"), r"[A-Za-z0-9]{12}"),
        match_settings_url=_site_url(data.get("match_settings_url")),
    )


def _expired(value) -> bool:
    """Whether a /me token_expires_at is a time (ISO 8601; no zone is UTC) already past. Anything else is not."""
    if not isinstance(value, str):
        return False
    value = value.strip()
    if value.endswith(("Z", "z")):  # Python before 3.11 does not read the Z
        value = value[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return False
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)) <= datetime.now(timezone.utc)


# The check of the request being served: a Profile, a Refusal, or None when it was not checked (stdio, linking off,
# or not a call of a LINKED_TOOLS tool). server.HttpGuard sets it per request, so concurrent requests never see each
# other's; a linked tool reads it.
auth: contextvars.ContextVar[Profile | Refusal | None] = contextvars.ContextVar("matchawards_auth", default=None)


async def check(scope) -> Profile | Refusal:
    """The check of an ASGI request: INVALID without a well-formed bearer token (no call made), else me(token)."""
    token = bearer(scope)
    return INVALID if token is None else await me(token)


def _resource_key(url: str) -> tuple:
    """A resource URL as compared: scheme and host case-insensitive, port, query and path as they are (a trailing
    slash ignored)."""
    parts = urlsplit(url.strip())
    if parts.username is not None or parts.password is not None:
        raise ValueError("userinfo")  # a resource never names a user: no match
    return parts.scheme.lower(), parts.hostname, parts.port, parts.path.rstrip("/"), parts.query, parts.fragment


def _same_resource(value) -> bool:
    """Whether a /me body's resource names this server."""
    try:
        return isinstance(value, str) and _resource_key(value) == _resource_key(resource())
    except ValueError:  # e.g. a port that is not a number, or user:pass@ in it
        return False


def _error(r: httpx.Response) -> str | None:
    """The `error` of a JSON error body, or None."""
    try:
        body = r.json()
    except ValueError:
        return None
    return body.get("error") if isinstance(body, dict) else None


def _refuse(key: bytes, answer: str) -> str:
    """Record that /me refused the token under `key`: its profile, if cached, is dropped, and INVALID is remembered
    for INVALID_TTL_S (INSUFFICIENT and FORBIDDEN are not remembered)."""
    _cache.pop(key, None)
    _refused[key] = next(_checks)
    _refused.move_to_end(key)
    global _refused_floor
    while len(_refused) > CACHE_MAX:
        _refused_floor = max(_refused_floor, _refused.popitem(last=False)[1])
    if answer == INVALID:
        _keep(_invalid, key, INVALID_TTL_S, INVALID)
    return answer


async def me(token: str) -> Profile | Refusal:
    """The profile `token` belongs to, from /me (cached CACHE_TTL_S), or why there is none: INVALID (401: unknown,
    expired or revoked; or a profile for a token issued for another resource, with no resource named, or whose
    token_expires_at is past), INSUFFICIENT (403 insufficient_scope: the token lacks SCOPE), FORBIDDEN (any other
    403: forbidden, an error /me does not name, or none) or UNAVAILABLE (no answer in time, a network error, any
    other status, a body that is not a profile, or the server-wide API budget spent).

    /me names the resource (RFC 8707) the token was issued for. Only a token issued for this server's resource()
    is taken: one a client got for another service is not a key to this one.

    A profile is cached for CACHE_TTL_S and INVALID for INVALID_TTL_S; INSUFFICIENT, FORBIDDEN and UNAVAILABLE are
    asked again next time. A refusal drops the token's cached profile, and a profile is not cached when a check that
    started later was refused first (two calls in flight with a token revoked between them). The log says which
    failure it was, never the token, the header or the token's hash."""
    key = _key(token)
    hit = _cached(_cache, key) or _cached(_invalid, key)
    if hit is not None:
        return hit
    if api.api_budget is not None and api.api_budget.check("api"):
        return UNAVAILABLE
    started = next(_checks)
    try:
        r = await client.get(f"{AGENT_API_BASE}{ME_PATH}", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        log.warning("profile check failed: %s", type(e).__name__)
        return UNAVAILABLE
    if r.status_code == 401:
        return _refuse(key, INVALID)
    if r.status_code == 403:
        return _refuse(key, INSUFFICIENT if _error(r) == INSUFFICIENT else FORBIDDEN)
    if r.status_code != 200:
        log.warning("profile check failed: HTTP %d", r.status_code)
        return UNAVAILABLE
    try:
        data = r.json()
    except ValueError:
        data = None
    profile = parse_profile(data)
    if profile is None:
        log.warning("profile check failed: the answer is not a profile")
        return UNAVAILABLE
    if not _same_resource(data.get("resource")):
        log.warning("profile check refused: the token was not issued for this resource")
        return _refuse(key, INVALID)
    if _expired(data.get("token_expires_at")):
        log.warning("profile check refused: the token has expired")
        return _refuse(key, INVALID)
    # Else a newer check was refused while this one waited (or a refusal that may have been one was dropped for
    # room): the 200 answers this call only.
    if max(_refused.get(key, -1), _refused_floor) < started:
        _invalid.pop(key, None)
        _keep(_cache, key, CACHE_TTL_S, profile)
    return profile
