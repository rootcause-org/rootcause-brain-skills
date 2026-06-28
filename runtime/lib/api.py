"""Generic READ-only REST client — the ``lib.db`` of third-party HTTP integrations.

Most integrations need NO bespoke Python. A compact declarative manifest row (base URL, auth
strategy, pagination style, rate-limit header) plus this caller is enough: the agent runs

    python -m lib.api get <key> <path> [--query k=v ...] [--paginate] [--pick a.b,c]

and pipes the JSON through ``jq``/``rg`` so a raw API dump never floods the model context. A
dedicated thin connector (``lib.connectors.<x>``) is added ONLY when the integration trips a
force-code trigger: field pre-selection, a multi-call join, exotic auth/signing, non-standard
pagination, or a search DSL (see the integrations skill). Such a connector imports THIS module —
it never re-implements retry/pagination/rate-limiting.

Design posture (mirrors ``lib.db``): read verbs only (we never write to customer systems), raise
LOUDLY with the failing provider detail on error rather than returning a silent empty/partial, and
take the credential from ``lib.oauth.token`` so it never lands in argv, logs, or model context.

Auth is the credential VALUE injected as ``RC_CONN_<KEY>`` plus a manifest-declared *strategy* that
says where it goes (bearer header / basic / api-key header / query param / oauth2 client-credentials).
An API key always goes in a HEADER, never the query string.

``requests`` is imported lazily so ``from lib import api`` loads even where it isn't installed (the
manifest helpers and CLI ``--help`` work on a bare host).
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator
from urllib.parse import urljoin

from lib import oauth

# Both halves of the requests timeout tuple (connect, read) — never None, so a hung server can't
# wedge a run. Read is generous because we optimise for thoroughness, not latency.
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 30.0

# Retry only idempotent reads by default — this is read-only grounding, but a connector could still
# pass a write verb, and a blind retry there is a footgun.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Transient statuses worth retrying. 429 is handled separately (honours Retry-After).
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_BASE = 0.5  # seconds; first retry waits up to this, then doubles (capped)
DEFAULT_BACKOFF_CAP = 30.0
# A single 429/Retry-After wait longer than this is split into capped sleeps so a misbehaving
# server can't pin a run for an unbounded stretch in one call.
MAX_RETRY_AFTER = 120.0

# Hard ceiling on auto-paging so a runaway cursor (server bug, our bug) can't loop forever.
DEFAULT_MAX_PAGES = 1000


class ApiError(RuntimeError):
    """One normalized exception for every non-2xx — carries status + the provider error body.

    Mirrors ``lib.db``'s raise-loud posture: the caller sees the failing detail (status + the
    provider's own error JSON/text, truncated) instead of a silent fallback. ``retryable`` lets the
    retry layer decide without re-parsing.
    """

    def __init__(self, status: int, body: str, *, url: str = "", retryable: bool = False):
        self.status = status
        self.body = body
        self.url = url
        self.retryable = retryable
        detail = (body or "").strip()
        if len(detail) > 800:
            detail = detail[:800] + "…(truncated)"
        where = f" for {url}" if url else ""
        super().__init__(f"HTTP {status}{where}: {detail}" if detail else f"HTTP {status}{where}")


@dataclass(frozen=True)
class Auth:
    """How the injected credential is presented. ``strategy`` is the manifest enum; ``name`` is the
    header/param name where it varies (api-key header name, query-param name)."""

    strategy: str = "bearer"  # bearer | basic | api_key_header | query_param | oauth2_client_credentials | none
    name: str = "Authorization"  # header name (api_key_header) or query key (query_param)
    # basic: the credential value is "user:pass"; if it has no ":", it's the username with empty pass.


@dataclass(frozen=True)
class Pagination:
    """Manifest-driven paging. The framework runs the while-has-more loop, not the connector.

    - ``none``    : single page.
    - ``cursor``  : response carries an opaque next token at ``cursor_field`` (dotted path); sent back
                    as the ``cursor_param`` query arg. ``has_more_field`` (optional) gates the loop;
                    absent ⇒ loop until the cursor field is empty.
    - ``offset``  : send ``offset_param`` advancing by the page length, ``limit_param`` = ``page_size``.
    - ``link``    : follow RFC 8288 ``Link: <url>; rel="next"`` headers; opaque server URL.
    - ``body_url``: like ``link`` but the next URL lives INSIDE the JSON body at ``next_url_field``
                    (e.g. ``meta.pagination.next``, ``links.next``, ``@odata.nextLink``) instead of a
                    header. The value may be absolute (used verbatim) or relative (``_join``ed onto
                    ``base_url``). Generalises the header-based ``link`` style to the body.
    - ``page``    : numeric page-number paging — ``page_param`` starts at ``page_start`` and increments
                    by 1 (NOT by item count, unlike ``offset``); ``limit_param`` = ``page_size``. Stops
                    on a short/empty page, or on ``has_more_field``=false when the API declares one
                    (some APIs clamp/repeat the last full page past the end). ``page_start`` is 0 for
                    0-based APIs (e.g. ClickUp).
    """

    style: str = "none"  # none | cursor | offset | link | body_url | page
    cursor_field: str = ""
    cursor_param: str = "cursor"
    has_more_field: str = ""
    items_field: str = ""  # dotted path to the list inside each page (e.g. "data"); "" ⇒ page is the list
    offset_param: str = "offset"
    limit_param: str = "limit"
    page_size: int = 100
    next_url_field: str = ""  # body_url: dotted path (or whole-key literal) to the next URL/path
    page_param: str = "page"  # page: the page-number query arg
    page_start: int = 1  # page: first page number (0 for 0-based APIs like ClickUp)


@dataclass(frozen=True)
class Manifest:
    """The compact declarative row that lets one integration work with zero bespoke code.

    Mirrors the host ``integration_catalog`` columns (base_url, auth, pagination, rate_limit header)
    so a connector and the Go catalog describe the same shape.
    """

    key: str
    base_url: str
    auth: Auth = field(default_factory=Auth)
    pagination: Pagination = field(default_factory=Pagination)
    rate_limit_remaining_header: str = ""  # e.g. "X-RateLimit-Remaining"; advisory pacing only
    default_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Page:
    """One fetched page: parsed ``body``, the ``items`` list extracted per the manifest, and the
    ``next`` continuation token/URL (None when exhausted)."""

    body: Any
    items: list
    next: Any | None


# ---------------------------------------------------------------------------
# Field extraction (response shaping)
# ---------------------------------------------------------------------------


def pick(obj: Any, paths: str | list[str]) -> dict:
    """Select a few dotted paths out of an object — the token-saving field pre-selector.

    ``paths`` is a comma-string ("customer.email,subscriptions.data.0.status") or a list. Each leaf
    is keyed by its FULL path so two paths never collide. A missing path is simply absent (selection,
    not validation). List indices are numeric segments; ``*`` maps over every element of a list.

    Example: ``pick(invoice, "id,status,lines.data.*.amount")`` →
    ``{"id": "...", "status": "...", "lines.data.*.amount": [100, 250]}``.
    """
    if isinstance(paths, str):
        wanted = [p.strip() for p in paths.split(",") if p.strip()]
    else:
        wanted = [str(p).strip() for p in paths if str(p).strip()]
    out: dict[str, Any] = {}
    for p in wanted:
        found, value = _dget(obj, p.split("."))
        if found:
            out[p] = value
    return out


def _dget(obj: Any, segments: list[str]) -> tuple[bool, Any]:
    """Resolve a dotted path. Returns ``(found, value)``; ``*`` maps over a list. ``found`` is False
    for a missing key/index so ``pick`` can omit it rather than inventing ``None``."""
    if not segments:
        return True, obj
    seg, rest = segments[0], segments[1:]
    if seg == "*":
        if not isinstance(obj, list):
            return False, None
        collected = []
        any_found = False
        for el in obj:
            ok, val = _dget(el, rest)
            if ok:
                any_found = True
                collected.append(val)
        return any_found, collected
    if isinstance(obj, dict):
        if seg not in obj:
            return False, None
        return _dget(obj[seg], rest)
    if isinstance(obj, list):
        try:
            idx = int(seg)
        except ValueError:
            return False, None
        if idx < 0 or idx >= len(obj):
            return False, None
        return _dget(obj[idx], rest)
    return False, None


def _dotted(obj: Any, path: str) -> Any:
    """Single-path dotted lookup returning the value or None — for internal manifest fields
    (cursor_field, has_more_field, items_field) where 'absent' and 'null' collapse to the same."""
    if not path:
        return obj
    found, value = _dget(obj, path.split("."))
    return value if found else None


def _next_url_in_body(body: Any, field: str) -> Any:
    """Resolve the body_url next-page field. Tries the WHOLE field as a single literal key first so
    a dotted-but-not-nested key like ``@odata.nextLink`` resolves, then falls back to dotted
    traversal (``meta.pagination.next``)."""
    if not field:
        return None
    if isinstance(body, dict) and field in body:  # literal key (e.g. "@odata.nextLink")
        return body[field]
    return _dotted(body, field)


# ---------------------------------------------------------------------------
# Backoff / rate-limit timing
# ---------------------------------------------------------------------------


def _full_jitter(attempt: int, base: float, cap: float, rng: random.Random) -> float:
    """Decorrelated/full-jitter backoff: sleep is uniform in [0, min(cap, base*2**attempt)].

    Full jitter (AWS "Exponential Backoff and Jitter") avoids the thundering-herd of a fixed
    schedule. ``attempt`` is 0-based for the first retry.
    """
    ceiling = min(cap, base * (2 ** attempt))
    return rng.uniform(0, ceiling)


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Parse a ``Retry-After`` header into a delay in seconds.

    Honours BOTH forms in RFC 9110 §10.2.3: a delay in seconds ("120") AND an HTTP-date
    ("Wed, 21 Oct 2026 07:28:00 GMT") — the seconds-only bug is common and starves the
    date form. Returns None when absent/unparseable (caller falls back to jittered backoff);
    a past/negative date clamps to 0.
    """
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    # Delay-seconds form (may be a float in the wild though the RFC says integer).
    try:
        return max(0.0, float(v))
    except ValueError:
        pass
    # HTTP-date form.
    try:
        dt = parsedate_to_datetime(v)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    target = dt.timestamp()
    current = time.time() if now is None else now
    return max(0.0, target - current)


def _sleep(seconds: float, sleeper: Callable[[float], None]) -> None:
    """Sleep, but never in one unbounded chunk: a hostile ``Retry-After`` is split into
    ``MAX_RETRY_AFTER`` slices so a run can't be pinned arbitrarily by one response."""
    remaining = seconds
    while remaining > 0:
        chunk = min(remaining, MAX_RETRY_AFTER)
        sleeper(chunk)
        remaining -= chunk


# ---------------------------------------------------------------------------
# Core request with retry at ONE layer
# ---------------------------------------------------------------------------


@dataclass
class Client:
    """A configured caller for one integration. Holds the manifest + resolved credential; methods do
    the read. Retry/backoff/rate-limit live HERE and nowhere else (retry at one layer only)."""

    manifest: Manifest
    credential: str = ""
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_cap: float = DEFAULT_BACKOFF_CAP
    max_pages: int = DEFAULT_MAX_PAGES
    # Injected only so tests can pin timing/jitter; production uses the real clock + RNG.
    _sleeper: Callable[[float], None] = time.sleep
    _rng: random.Random = field(default_factory=lambda: random.Random())

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Make one HTTP call (following retries/rate-limits) and return parsed JSON.

        ``path`` is joined onto the manifest ``base_url`` (an absolute URL overrides). Raises
        ``ApiError`` on a non-2xx after exhausting retries — never a silent empty.
        """
        resp = self._send(method, path, query=query, headers=headers)
        return _parse_json(resp)

    def get(self, path: str, **kw) -> Any:
        return self.request("GET", path, **kw)

    def _send(self, method, path, *, query=None, headers=None):
        import requests

        verb = method.upper()
        url = _join(self.manifest.base_url, path)
        req_headers = dict(self.manifest.default_headers)
        req_headers.update(headers or {})
        req_query = dict(query or {})
        self._apply_auth(req_headers, req_query)
        idempotent = verb in _IDEMPOTENT_METHODS

        attempt = 0
        while True:
            resp = requests.request(
                verb,
                url,
                params=req_query,
                headers=req_headers,
                timeout=(self.connect_timeout, self.read_timeout),
            )
            if 200 <= resp.status_code < 300:
                return resp
            retryable = resp.status_code in _RETRYABLE_STATUS and idempotent
            if not retryable or attempt >= self.max_retries:
                raise ApiError(
                    resp.status_code,
                    _body_text(resp),
                    url=url,
                    retryable=resp.status_code in _RETRYABLE_STATUS,
                )
            delay = self._retry_delay(resp, attempt)
            _sleep(delay, self._sleeper)
            attempt += 1

    def _retry_delay(self, resp, attempt: int) -> float:
        """Wait before the next retry: honour ``Retry-After`` on a 429 (capped), else jittered
        exponential backoff."""
        if resp.status_code == 429:
            ra = parse_retry_after(resp.headers.get("Retry-After"))
            if ra is not None:
                return min(ra, MAX_RETRY_AFTER)
        return _full_jitter(attempt, self.backoff_base, self.backoff_cap, self._rng)

    def _apply_auth(self, headers: dict, query: dict) -> None:
        """Place the credential per the manifest auth strategy. An api-key always goes in a HEADER;
        ``query_param`` exists only for APIs that genuinely require it and is used sparingly."""
        cred = self.credential
        a = self.manifest.auth
        strat = a.strategy
        if strat == "none" or not cred:
            if strat not in ("none", ""):
                raise RuntimeError(
                    f"integration {self.manifest.key!r} needs a credential for auth strategy "
                    f"{strat!r} but none is set"
                )
            return
        if strat == "bearer":
            headers.setdefault("Authorization", f"Bearer {cred}")
        elif strat == "basic":
            import base64

            user, _, pw = cred.partition(":")
            token = base64.b64encode(f"{user}:{pw}".encode()).decode()
            headers.setdefault("Authorization", f"Basic {token}")
        elif strat == "api_key_header":
            headers.setdefault(a.name or "Authorization", cred)
        elif strat == "query_param":
            query.setdefault(a.name or "api_key", cred)
        elif strat == "oauth2_client_credentials":
            # The host injects an already-minted bearer for the client-credentials grant; we present
            # it as a bearer. Minting/refresh is a host concern, not the runtime's.
            headers.setdefault("Authorization", f"Bearer {cred}")
        else:
            raise RuntimeError(f"unknown auth strategy {strat!r} for integration {self.manifest.key!r}")

    # -- pagination -------------------------------------------------------

    def fetch_page(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Page:
        """Fetch one page and extract its items + next token per the manifest pagination style."""
        resp = self._send("GET", path, query=query, headers=headers)
        body = _parse_json(resp)
        items = self._extract_items(body)
        nxt = self._next_token(body, resp)
        return Page(body=body, items=items, next=nxt)

    def paginate(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_pages: int | None = None,
    ) -> Iterator[Page]:
        """Auto-page: yield ``Page`` objects following the server's opaque next token until exhausted.

        Fail-safe partial results: if a page mid-stream errors, the ``ApiError`` propagates — but a
        caller that wants what-it-has-so-far should use ``collect`` (which sets an ``incomplete``
        flag) rather than swallowing the error here. The framework owns the loop, so a connector
        never hand-rolls a ``while has_more``.
        """
        p = self.manifest.pagination
        cap = self.max_pages if max_pages is None else max_pages
        base_query = dict(query or {})
        seen = 0
        if p.style == "none":
            yield self.fetch_page(path, query=base_query, headers=headers)
            return
        if p.style == "offset":
            base_query.setdefault(p.limit_param, p.page_size)
            offset = int(base_query.get(p.offset_param, 0) or 0)
            while seen < cap:
                q = dict(base_query, **{p.offset_param: offset})
                page = self.fetch_page(path, query=q, headers=headers)
                yield page
                seen += 1
                if not page.items or len(page.items) < p.page_size:
                    return
                offset += len(page.items)
            return
        if p.style == "page":
            # Page-NUMBER paging: advance page_param by 1 from page_start (not by item count).
            base_query.setdefault(p.limit_param, p.page_size)
            page_num = int(base_query.get(p.page_param, p.page_start) or p.page_start)
            while seen < cap:
                q = dict(base_query, **{p.page_param: page_num})
                page = self.fetch_page(path, query=q, headers=headers)
                yield page
                seen += 1
                # Stop on a short/empty page; or, when the API declares one, an explicit
                # has_more_field=false — some page-number APIs clamp/repeat the last FULL page past
                # the end instead of returning a short page, so the size check alone wouldn't fire.
                if not page.items or len(page.items) < p.page_size:
                    return
                if p.has_more_field and not _truthy(_dotted(page.body, p.has_more_field)):
                    return
                page_num += 1
            return
        # cursor / link / body_url all drive off page.next (an opaque token or URL). For link and
        # body_url, page.next is a URL/path we follow directly (a relative path is _joined to base_url
        # inside _send_url); for cursor it's a token re-sent as a query param.
        next_token: Any | None = None
        next_url: str | None = None
        while seen < cap:
            if next_url is not None:  # link / body_url: follow the URL (absolute verbatim, relative joined)
                resp = self._send_url("GET", next_url, headers=headers)
                body = _parse_json(resp)
                page = Page(body=body, items=self._extract_items(body), next=self._next_token(body, resp))
            else:
                q = dict(base_query)
                if next_token is not None:
                    q[p.cursor_param] = next_token
                page = self.fetch_page(path, query=q, headers=headers)
            yield page
            seen += 1
            if page.next is None:
                return
            if p.style in ("link", "body_url"):
                next_url = page.next
            else:
                next_token = page.next

    def collect(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_items: int | None = None,
        max_pages: int | None = None,
    ) -> dict:
        """Auto-page and gather every item into one list with an explicit completeness flag.

        Returns ``{"items": [...], "incomplete": bool, "reason": str}``. Fail-safe partial results:
        if a page errors mid-stream we return what we already have with ``incomplete=True`` and the
        error in ``reason`` — partial NEVER masquerades as a complete (or empty) result. Hitting
        ``max_items``/``max_pages`` is also ``incomplete`` (more may exist on the server).
        """
        items: list = []
        incomplete = False
        reason = ""
        pages = 0
        try:
            for page in self.paginate(path, query=query, headers=headers, max_pages=max_pages):
                pages += 1
                items.extend(page.items)
                if max_items is not None and len(items) >= max_items:
                    items = items[:max_items]
                    incomplete = True
                    reason = f"reached max_items={max_items}"
                    break
            else:
                if max_pages is not None and pages >= max_pages:
                    incomplete = True
                    reason = f"reached max_pages={max_pages}"
        except ApiError as e:
            incomplete = True
            reason = f"page fetch failed after {len(items)} item(s): {e}"
        return {"items": items, "incomplete": incomplete, "reason": reason}

    def _send_url(self, method: str, url: str, *, headers=None):
        """Follow a next-page URL from a Link header or JSON body (link / body_url styles).

        An absolute URL is used verbatim; a relative path (e.g. recurly ``/sites/.../accounts?cursor=…``,
        twilio ``/2010-04-01/…``) is ``_join``ed onto ``base_url`` so the host/scheme survive the follow.
        """
        import requests

        url = _join(self.manifest.base_url, url)
        req_headers = dict(self.manifest.default_headers)
        req_headers.update(headers or {})
        empty: dict[str, Any] = {}
        self._apply_auth(req_headers, empty)
        # Auth that lands in the query string must survive on a verbatim follow-URL too.
        params = empty or None
        verb = method.upper()
        idempotent = verb in _IDEMPOTENT_METHODS
        attempt = 0
        while True:
            resp = requests.request(
                verb, url, params=params, headers=req_headers,
                timeout=(self.connect_timeout, self.read_timeout),
            )
            if 200 <= resp.status_code < 300:
                return resp
            retryable = resp.status_code in _RETRYABLE_STATUS and idempotent
            if not retryable or attempt >= self.max_retries:
                raise ApiError(resp.status_code, _body_text(resp), url=url,
                               retryable=resp.status_code in _RETRYABLE_STATUS)
            _sleep(self._retry_delay(resp, attempt), self._sleeper)
            attempt += 1

    def _extract_items(self, body: Any) -> list:
        p = self.manifest.pagination
        target = _dotted(body, p.items_field) if p.items_field else body
        if isinstance(target, list):
            return list(target)
        return []

    def _next_token(self, body: Any, resp) -> Any | None:
        p = self.manifest.pagination
        if p.style == "cursor":
            if p.has_more_field:
                if not _truthy(_dotted(body, p.has_more_field)):
                    return None
            token = _dotted(body, p.cursor_field)
            return token if token else None
        if p.style == "link":
            return _parse_link_next(resp.headers.get("Link"))
        if p.style == "body_url":
            return _next_url_in_body(body, p.next_url_field) or None
        return None


# ---------------------------------------------------------------------------
# Module-level convenience: build a client from an injected credential
# ---------------------------------------------------------------------------


def client(manifest: Manifest, *, token_key: str | None = None, **kw) -> Client:
    """Build a ``Client``, resolving the credential from ``lib.oauth.token`` (``RC_CONN_<KEY>``).

    Raises loudly (via ``oauth.token``) when the connection isn't configured, so a script fails with
    the exact missing ``RC_CONN_*`` instead of making anonymous calls. ``auth.strategy == "none"``
    skips credential resolution.
    """
    cred = ""
    if manifest.auth.strategy not in ("none", ""):
        cred = oauth.token(token_key or manifest.key)
    return Client(manifest=manifest, credential=cred, **kw)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _join(base: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not base:
        return path
    # urljoin drops the base path unless it ends in "/"; we want base_url to be a prefix, so join
    # against base_url + "/" and strip the leading "/" off path.
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _parse_json(resp) -> Any:
    try:
        return resp.json()
    except ValueError:
        text = _body_text(resp)
        raise ApiError(resp.status_code, f"non-JSON response body: {text[:200]}", url=getattr(resp, "url", ""))


def _body_text(resp) -> str:
    try:
        return resp.text
    except Exception:  # noqa: BLE001 — never let error-rendering itself raise
        return ""


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "0", "no", "null")
    return bool(v)


def _parse_link_next(header: str | None) -> str | None:
    """Extract ``rel="next"`` from an RFC 8288 ``Link`` header. Returns the URL or None."""
    if not header:
        return None
    for part in header.split(","):
        seg = part.strip()
        if "<" not in seg or ">" not in seg:
            continue
        url = seg[seg.index("<") + 1 : seg.index(">")]
        params = seg[seg.index(">") + 1 :]
        for attr in params.split(";"):
            attr = attr.strip()
            if attr.lower().replace(" ", "") in ('rel="next"', "rel=next"):
                return url
    return None


# ---------------------------------------------------------------------------
# CLI: python -m lib.api get <key> <path>  (manifest-driven, no bespoke code)
# ---------------------------------------------------------------------------

# Manifests the generic CLI can drive directly. A connector with its own CLI registers here too, so
# `python -m lib.api get <key> ...` works for any catalogued integration without a per-key script.
MANIFESTS: dict[str, Manifest] = {}

# Keys populated from a connectors/*/manifest.yaml. Tracked so loading is idempotent (re-running
# load_manifests never duplicates) and so an explicit register() always WINS: the YAML loader fills
# only keys not already present, and a later register() overwrites the dict entry outright.
_YAML_LOADED_KEYS: set[str] = set()


def register(manifest: Manifest) -> Manifest:
    MANIFESTS[manifest.key] = manifest
    _YAML_LOADED_KEYS.discard(manifest.key)  # an explicit registration is no longer YAML-owned
    return manifest


def load_manifests(*, force: bool = False) -> dict[str, Manifest]:
    """Discover and register every ``lib/connectors/*/manifest.yaml`` into ``MANIFESTS``.

    This is what makes "a manifest row IS the integration" true at runtime: a catalogued connector
    with NO bespoke Python is still drivable via ``python -m lib.api get <key> ...``. Idempotent —
    only fills keys not already present, so an explicit ``register()`` (e.g. a Python connector that
    needs a richer Manifest than the YAML expresses) is the source of truth and is never clobbered.

    Discovery walks the package's ``connectors`` directory (works from the baked/installed wheel, not
    only the source tree). A single malformed manifest raises ``ManifestError`` naming the file rather
    than silently skipping — a broken catalog row must fail loudly, not vanish.
    """
    from pathlib import Path

    connectors_dir = Path(__file__).resolve().parent / "connectors"
    for manifest_path in sorted(connectors_dir.glob("*/manifest.yaml")):
        key_hint = manifest_path.parent.name
        if not force and key_hint in MANIFESTS and key_hint not in _YAML_LOADED_KEYS:
            continue  # an explicit register() owns this key — leave it
        mani = _parse_manifest_file(manifest_path)
        MANIFESTS[mani.key] = mani
        _YAML_LOADED_KEYS.add(mani.key)
    return MANIFESTS


class ManifestError(RuntimeError):
    """A manifest.yaml could not be parsed into a Manifest — names the offending file."""


def _parse_manifest_file(path) -> Manifest:
    """Parse one connector ``manifest.yaml`` into a ``Manifest``.

    Maps only the fields that DRIVE a REST call (base_url, auth, pagination, rate_limit,
    default_headers). The catalog-only blocks (``oauth:``, ``mcp_url_template``, ``kinds``,
    ``help_md``, …) are read leniently and ignored here — they belong to the host catalog, not the
    runtime caller — so an unknown key never breaks loading (forward-compatible)."""
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ManifestError(f"failed to read/parse manifest {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest {path} is not a YAML mapping")
    try:
        return _manifest_from_dict(raw)
    except (KeyError, TypeError, ValueError) as e:
        raise ManifestError(f"invalid manifest {path}: {e}") from e


def _manifest_from_dict(raw: dict) -> Manifest:
    key = raw.get("key")
    if not key or not isinstance(key, str):
        raise ValueError("missing required 'key'")
    base_url = raw.get("base_url") or ""
    if not isinstance(base_url, str):
        raise ValueError("'base_url' must be a string")

    auth_raw = raw.get("auth") or {}
    if not isinstance(auth_raw, dict):
        raise ValueError("'auth' must be a mapping")
    auth = Auth(
        strategy=str(auth_raw.get("strategy", "bearer")),
        name=str(auth_raw.get("name", "Authorization")),
    )

    pg_raw = raw.get("pagination") or {}
    if not isinstance(pg_raw, dict):
        raise ValueError("'pagination' must be a mapping")
    defaults = Pagination()
    pagination = Pagination(
        style=str(pg_raw.get("style", defaults.style)),
        cursor_field=str(pg_raw.get("cursor_field", defaults.cursor_field)),
        cursor_param=str(pg_raw.get("cursor_param", defaults.cursor_param)),
        has_more_field=str(pg_raw.get("has_more_field", defaults.has_more_field)),
        items_field=str(pg_raw.get("items_field", defaults.items_field)),
        offset_param=str(pg_raw.get("offset_param", defaults.offset_param)),
        limit_param=str(pg_raw.get("limit_param", defaults.limit_param)),
        page_size=int(pg_raw.get("page_size", defaults.page_size)),
        next_url_field=str(pg_raw.get("next_url_field", defaults.next_url_field)),
        page_param=str(pg_raw.get("page_param", defaults.page_param)),
        page_start=int(pg_raw.get("page_start", defaults.page_start)),
    )

    rl_raw = raw.get("rate_limit") or {}
    if not isinstance(rl_raw, dict):
        raise ValueError("'rate_limit' must be a mapping")
    rate_limit_remaining_header = str(rl_raw.get("remaining_header", "") or "")

    dh_raw = raw.get("default_headers") or {}
    if not isinstance(dh_raw, dict):
        raise ValueError("'default_headers' must be a mapping")
    default_headers = {str(k): str(v) for k, v in dh_raw.items()}

    return Manifest(
        key=key,
        base_url=base_url,
        auth=auth,
        pagination=pagination,
        rate_limit_remaining_header=rate_limit_remaining_header,
        default_headers=default_headers,
    )


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    p = argparse.ArgumentParser(prog="python -m lib.api", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get", help="GET a path on a registered integration and print JSON")
    g.add_argument("key", help="integration key (RC_CONN_<KEY> must be injected)")
    g.add_argument("path", help="API path under the manifest base_url, or an absolute URL")
    g.add_argument("--query", action="append", default=[], metavar="K=V", help="query param (repeatable)")
    g.add_argument("--paginate", action="store_true", help="auto-page and collect all items")
    g.add_argument("--max-items", type=int, default=None)
    g.add_argument("--max-pages", type=int, default=None)
    g.add_argument("--pick", default="", help="comma-separated dotted paths to select from each object")
    args = p.parse_args(argv)

    if args.cmd != "get":
        p.error("unknown command")
    load_manifests()  # discover catalogued manifests so any zero-Python integration is drivable
    mani = MANIFESTS.get(args.key)
    if mani is None:
        p.error(f"no manifest registered for {args.key!r}; known: {sorted(MANIFESTS) or '(none)'}")
    query = dict(kv.split("=", 1) for kv in args.query if "=" in kv)
    c = client(mani)
    if args.paginate:
        result = c.collect(args.path, query=query, max_items=args.max_items, max_pages=args.max_pages)
        if args.pick:
            result["items"] = [pick(it, args.pick) for it in result["items"]]
        print(json.dumps(result, indent=2, default=str))
    else:
        body = c.get(args.path, query=query)
        print(json.dumps(pick(body, args.pick) if args.pick else body, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
