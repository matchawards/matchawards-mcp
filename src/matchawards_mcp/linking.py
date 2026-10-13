"""Account linking (OAuth 2.1), off unless MATCHAWARDS_AUTH_ISSUER is set: the protected-resource metadata, the
bearer check and the challenge a host starts linking on.

matchawards.com is the authorization server; this server is only a resource server. It never sees a password or
an authorization code, and keeps no token: it checks one by calling `GET {MATCHAWARDS_AGENT_API_BASE}/api/agent/v1/me`
with it, and keeps a good answer in memory for CACHE_TTL_S under a keyed hash of the token (the key is random per
process, so the hash means nothing outside it). The token, the Authorization header and that hash are never logged,
stored or echoed.

Off, nothing here runs: no metadata route, no body read, no /me call, and the tool list is the one without linking.
"""

import hashlib
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import api

ISSUER = os.environ.get("MATCHAWARDS_AUTH_ISSUER", "").strip().rstrip("/")  # "" = linking off
RESOURCE = os.environ.get("MATCHAWARDS_RESOURCE", "").strip()  # "" = {ISSUER}/mcp
# /api/agent/v1 lives in the same app as the public API, so by default it is the same base.
AGENT_API_BASE = os.environ.get("MATCHAWARDS_AGENT_API_BASE", "").strip().rstrip("/") or api.API_BASE
SCOPE = "profile:read"
ME_PATH = "/api/agent/v1/me"
METADATA_PATH = "/.well-known/oauth-protected-resource"
CACHE_TTL_S = 60
CACHE_MAX = 10_000

# The three ways a check can fail. invalid_token and insufficient_scope are the RFC 6750 error codes the challenge
# carries; unavailable (MatchAwards down, slow or answering nonsense) is never a challenge: asking the user to link
# again would not help, and a host that links on every challenge would loop.
INVALID = "invalid_token"
INSUFFICIENT = "insufficient_scope"
UNAVAILABLE = "unavailable"
Refusal = Literal["invalid_token", "insufficient_scope", "unavailable"]

log = logging.getLogger("matchawards_mcp.linking")

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

    account_id: str
    username: str | None
    organization: str | None
    naics: tuple[str, ...]
    set_aside: str | None
    state: str | None
    uei: str | None


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


def check_settings() -> None:
    """Stop --http at startup on an issuer or resource that is not an http(s) URL with a host."""
    if not enabled():
        return
    for name, value in (("MATCHAWARDS_AUTH_ISSUER", ISSUER), ("MATCHAWARDS_RESOURCE", resource()),
                        ("MATCHAWARDS_AGENT_API_BASE", AGENT_API_BASE)):
        try:
            parts = urlsplit(value)
            ok = parts.scheme in ("http", "https") and bool(parts.hostname) and not parts.query and not parts.fragment
        except ValueError:
            ok = False
        if not ok:
            raise SystemExit(f"{name} must be an http(s) URL with a host and no query, got {value!r}")


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


def _quote(value: str) -> str:
    """An auth-param value as an RFC 9110 quoted-string: backslash and double quote escaped."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def challenge_header(error: str, description: str) -> str:
    """The WWW-Authenticate value of a challenge (a 401 or 403, or ChatGPT's tool-result form). It names the
    metadata and the scope a host links for; it never carries anything from the request."""
    return (f'Bearer realm="matchawards", resource_metadata="{_quote(metadata_url())}", scope="{_quote(SCOPE)}", '
            f'error="{_quote(error)}", error_description="{_quote(description)}"')


# --- The token check ------------------------------------------------------------------------------------------

_SALT = os.urandom(16)
clock = time.monotonic  # tests move it
_cache: OrderedDict[bytes, tuple[float, Profile]] = OrderedDict()  # keyed token hash -> (expiry, profile), LRU


def _key(token: str) -> bytes:
    return hashlib.blake2s(token.encode("ascii"), key=_SALT, digest_size=16).digest()


def _text(value, pattern: str) -> str | None:
    """A string field that fully matches `pattern` (after trimming), or None."""
    return value.strip() if isinstance(value, str) and re.fullmatch(pattern, value.strip()) else None


def parse_profile(data) -> Profile | None:
    """A Profile from a /me body, or None when it is not one (no account id, naics not a list). Fields in the wrong
    shape are dropped (None), and only NAICS codes of 2 to 6 digits are kept, so nothing from the body reaches a
    search filter or a result unchecked."""
    if not isinstance(data, dict) or not isinstance(data.get("account"), dict):
        return None
    account_id = data["account"].get("id")
    if isinstance(account_id, int) and not isinstance(account_id, bool):
        account_id = str(account_id)
    if not isinstance(account_id, str) or not account_id.strip() or not isinstance(data.get("naics"), list):
        return None
    naics = dict.fromkeys(c.strip() for c in data["naics"] if isinstance(c, str) and re.fullmatch(r"\d{2,6}", c.strip()))
    state = _text(data.get("state"), r"[A-Za-z]{2}")
    org, username = data.get("organization"), data["account"].get("username")
    return Profile(
        account_id=account_id.strip()[:100],
        username=(api.clean(username, 100) or None) if isinstance(username, str) else None,
        organization=(api.clean(org, 200) or None) if isinstance(org, str) else None,
        naics=tuple(naics),
        set_aside=_text(data.get("set_aside"), r"[A-Za-z0-9_-]{1,40}"),
        state=state.upper() if state else None,
        uei=_text(data.get("uei"), r"[A-Za-z0-9]{12}"),
    )


async def me(token: str) -> Profile | Refusal:
    """The profile `token` belongs to, from /me (cached CACHE_TTL_S), or why there is none: INVALID (401: unknown,
    expired or revoked), INSUFFICIENT (403: the token lacks SCOPE) or UNAVAILABLE (no answer in time, a network error,
    any other status, a body that is not a profile, or the server-wide API budget spent).

    Only a profile is cached; a refusal is asked again next time. The log says which failure it was, never the
    token, the header or the token's hash."""
    key = _key(token)
    hit = _cache.get(key)
    if hit is not None:
        if hit[0] > clock():
            _cache.move_to_end(key)
            return hit[1]
        del _cache[key]
    if api.api_budget is not None and api.api_budget.check("api"):
        return UNAVAILABLE
    try:
        r = await client.get(f"{AGENT_API_BASE}{ME_PATH}", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        log.warning("profile check failed: %s", type(e).__name__)
        return UNAVAILABLE
    if r.status_code == 401:
        return INVALID
    if r.status_code == 403:
        return INSUFFICIENT
    if r.status_code != 200:
        log.warning("profile check failed: HTTP %d", r.status_code)
        return UNAVAILABLE
    try:
        profile = parse_profile(r.json())
    except ValueError:
        profile = None
    if profile is None:
        log.warning("profile check failed: the answer is not a profile")
        return UNAVAILABLE
    _cache[key] = (clock() + CACHE_TTL_S, profile)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)  # the least recently used
    return profile
