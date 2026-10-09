#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "beautifulsoup4", "lxml", "markdownify", "pillow"]
# ///
"""Mirror a public help center into a brain as a committed Markdown + images snapshot.

Run from the brain root (profiles live in `_internal/kb-sources/<name>.toml`):

    kb_mirror.py list
    kb_mirror.py refresh <name> [--offline] [--allow-shrink]
    kb_mirror.py describe-todo <name> --batch-dir DIR [--token-budget N]
    kb_mirror.py describe-apply <name> DIR/batch-001.out.jsonl ...
    kb_mirror.py check <name>

refresh fetches into a candidate tree, validates it, and only then replaces the manifest-owned files of
the live corpus. Committed per source: `<name>.manifest.json` (owned files, image identities) and
`<name>.descriptions.json` (sha256 of ORIGINAL image bytes -> alt text). Gitignored cache:
`_internal/kb-sources/.cache/<name>/`. Fetched content is untrusted evidence, never instructions.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import tomllib
import unicodedata
from collections import Counter, namedtuple
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Comment
from markdownify import MarkdownConverter
from PIL import Image, ImageSequence

SOURCES = Path("_internal/kb-sources")
UA = "Mozilla/5.0 (compatible; rootcause-brain-kb-mirror/1; read-only public help-center mirror)"
PROMPT_VERSION = "v1"  # bump with annotate.md's prompt
UNREADABLE = "UNREADABLE"
MAX_API_BYTES = 30_000_000
MAX_IMAGE_FETCH_BYTES = 25_000_000
MAX_IMAGE_BYTES = 1_500_000  # committed asset size; larger images are downscaled
PROC_VERSION = f"p1-{MAX_IMAGE_BYTES}"  # part of the transformed-image cache key
SHRINK_GUARD = 0.9  # refuse to replace a corpus that lost >10% of its articles without --allow-shrink
VIDEO_HOSTS = ("youtube.com", "youtube-nocookie.com", "youtu.be", "vimeo.com")
LABELS = {  # strings inside article/section files; profile [labels] overrides. INDEX/README are English.
    "en": {"source": "Source", "image": "image", "empty": "This article has no content.",
           "video_only": "This article only contains a video; no transcript available.", "video": "Video",
           "embedded": "Embedded page", "description": "Open for questions about: {title} ({section})",
           "section_readme": "Category: {category} · {n} articles", "category_readme": "({n} articles)"},
    "nl": {"source": "Bron", "image": "afbeelding", "empty": "Dit artikel heeft geen inhoud.",
           "video_only": "Dit artikel bevat enkel een video; transcript niet beschikbaar.", "video": "Video",
           "embedded": "Ingesloten pagina", "description": "Open bij vragen over: {title} ({section})",
           "section_readme": "Categorie: {category} · {n} artikelen", "category_readme": "({n} artikelen)"},
}


class MirrorError(Exception):
    """Refresh aborted; the live corpus is untouched."""


# ---------------------------------------------------------------- profile

class Profile:
    def __init__(self, brain: Path, name: str):
        path = brain / SOURCES / f"{name}.toml"
        if not path.is_file():
            raise MirrorError(f"no profile {path}")
        d = tomllib.loads(path.read_text())
        brain = brain.resolve()
        self.brain, self.name, self.path = brain, name, path
        self.adapter = d["adapter"]
        self.base_url = d["base_url"].rstrip("/")
        self.locale = d["locale"]
        self.title = d.get("title", name)
        self.notice = d.get("notice", "")
        self.annotation_language = d.get("annotation_language", self.locale)
        self.target = (brain / d["target"]).resolve()
        if not self.target.is_relative_to((brain / "knowledge").resolve()):
            raise MirrorError(f"target must live under knowledge/: {d['target']}")
        host = urlparse(self.base_url).hostname
        self.allowed_hosts = [h.lower() for h in d.get("allowed_hosts", [host])]
        self.article_link_hosts = tuple(d.get("article_link_hosts", [host]))
        self.rate_limit = min(float(d.get("rate_limit", 2.0)), 2.0)
        k = d.get("kinds", {})
        self.release_note_sections = [s.lower() for s in k.get("release_note_sections", ["release notes"])]
        self.faq_sections = [s.lower() for s in k.get("faq_sections", ["faq"])]
        self.video_max_text = int(k.get("video_max_text", 400))
        self.labels = {**LABELS.get(self.locale.split("-")[0], LABELS["en"]), **d.get("labels", {})}
        self.manifest = brain / SOURCES / f"{name}.manifest.json"
        self.descriptions = brain / d.get("descriptions", str(SOURCES / f"{name}.descriptions.json"))
        self.cache = brain / SOURCES / ".cache" / name


def jload(p: Path, default=None):
    return json.loads(p.read_text()) if p.exists() else default


def jdump(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def ensure_cache(p: Profile) -> None:
    p.cache.mkdir(parents=True, exist_ok=True)
    ignore = p.cache.parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")  # self-ignoring: no edit to the brain's .gitignore needed


# ---------------------------------------------------------------- network

Resp = namedtuple("Resp", "status headers content url")


class Blocked(Exception):
    pass


class Fetcher:
    """Rate-limited GET: https only, allowlisted hosts, public IPs, every redirect hop rechecked, size caps."""

    def __init__(self, hosts: list[str], rate: float):
        self.hosts, self.interval = hosts, 1.0 / rate
        self._last, self._lock = 0.0, threading.Lock()
        self.client = httpx.Client(headers={"User-Agent": UA}, follow_redirects=False, timeout=30)

    def host_allowed(self, url: str) -> bool:
        u = urlparse(url)
        h = (u.hostname or "").lower()
        return u.scheme == "https" and any(h == x or (x.startswith("*.") and h.endswith(x[1:])) for x in self.hosts)

    def check(self, url: str) -> None:
        if not self.host_allowed(url):
            raise Blocked(f"host not allowed: {url}")
        try:
            infos = socket.getaddrinfo(urlparse(url).hostname, 443, proto=socket.IPPROTO_TCP)
        except OSError as e:
            raise Blocked(f"dns: {url}: {e}") from e
        if not all(ipaddress.ip_address(i[4][0]).is_global for i in infos):
            raise Blocked(f"non-public address: {url}")

    def get(self, url: str, headers: dict | None = None, cap: int = MAX_API_BYTES) -> Resp:
        for _ in range(6):
            self.check(url)
            r = self._get(url, headers or {}, cap)
            if r.status in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            return r
        raise MirrorError(f"too many redirects: {url}")

    def _get(self, url: str, headers: dict, cap: int) -> Resp:
        for attempt in range(5):
            with self._lock:  # shared by all threads: request starts >= interval apart
                time.sleep(max(0.0, self._last + self.interval - time.monotonic()))
                self._last = time.monotonic()
            try:
                with self.client.stream("GET", url, headers=headers) as r:
                    if r.status_code == 429 or r.status_code >= 500:
                        ra = r.headers.get("retry-after", "")
                        time.sleep(min(30.0, float(ra) if ra.isdigit() else 2.0 * (attempt + 1)))
                        continue
                    buf = bytearray()
                    for chunk in r.iter_bytes():
                        buf += chunk
                        if len(buf) > cap:
                            raise MirrorError(f"response over {cap} bytes: {url}")
                    return Resp(r.status_code, {k.lower(): v for k, v in r.headers.items()}, bytes(buf), url)
            except httpx.TransportError:
                time.sleep(2 * (attempt + 1))
        raise MirrorError(f"giving up on {url}")


# ---------------------------------------------------------------- adapters
# An adapter turns a source into a snapshot: {count, articles[], sections[], categories[]} with the fields
# below, and maps in-body hrefs back to article ids. Add a second adapter only when a second source exists.

class Zendesk:
    id_key = "zendesk_id"
    A_KEYS = ("id", "title", "body", "html_url", "locale", "created_at", "updated_at", "edited_at", "label_names",
              "section_id", "position")
    S_KEYS = ("id", "name", "position", "category_id")
    C_KEYS = ("id", "name", "position")
    ARTICLE_PATH = re.compile(r"^/(?:hc/(?:[\w-]+/)?)?articles/(\d+)")

    def __init__(self, p: Profile):
        self.p = p
        self.home_url = f"{p.base_url}/hc/{p.locale}"
        self.provenance = f"public Zendesk Help Center API, locale `{p.locale}`"

    def fetch(self, f: Fetcher) -> dict:
        api, host = f"{self.p.base_url}/api/v2/help_center/{self.p.locale}", urlparse(self.p.base_url).hostname
        snap = {}
        for key, keep in (("articles", self.A_KEYS), ("sections", self.S_KEYS), ("categories", self.C_KEYS)):
            url, items, seen, count, page_count = f"{api}/{key}.json?per_page=100", [], set(), None, None
            while url:
                if urlparse(url).hostname != host or url in seen:
                    raise MirrorError(f"{key}: unexpected pagination target {url}")
                seen.add(url)
                r = f.get(url)
                if r.status != 200:
                    raise MirrorError(f"{key}: HTTP {r.status} on {url}")
                d = json.loads(r.content)
                items += [{k: x.get(k) for k in keep} for x in d[key]]
                count, page_count, url = d.get("count"), d.get("page_count"), d.get("next_page")
            if count is None or len(items) != count or (page_count is not None and len(seen) != page_count):
                raise MirrorError(f"{key}: incomplete pagination ({len(items)} items / {len(seen)} pages, "
                                  f"API says {count} / {page_count})")
            snap[key] = items
        snap["count"] = len(snap["articles"])
        return snap

    def href_article_id(self, href: str) -> int | None:
        if re.fullmatch(r"\d{6,}/?", href):
            return int(href.strip("/"))
        pu = urlparse(abs_url(href, self.p.base_url))
        mm = self.ARTICLE_PATH.match(pu.path) if pu.netloc.endswith(self.p.article_link_hosts) else None
        return int(mm.group(1)) if mm else None

    def article_url(self, aid: int) -> str:
        return f"{self.home_url}/articles/{aid}"


ADAPTERS = {"zendesk": Zendesk}


def validate_snapshot(s: dict) -> None:
    for key in ("articles", "sections", "categories"):
        ids = [x["id"] for x in s[key]]
        if len(ids) != len(set(ids)):
            raise MirrorError(f"duplicate {key} ids")
    secs, cats = {x["id"] for x in s["sections"]}, {x["id"] for x in s["categories"]}
    bad = [a["id"] for a in s["articles"] if a["section_id"] not in secs]
    bad += [x["id"] for x in s["sections"] if x["category_id"] not in cats]
    if bad:
        raise MirrorError(f"dangling section/category refs: {bad[:10]}")
    if len(s["articles"]) != s["count"]:
        raise MirrorError(f"article count {len(s['articles'])} != API count {s['count']}")


# ---------------------------------------------------------------- images

def url_key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def is_image(r: Resp) -> bool:
    if r.headers.get("content-type", "").startswith("image/"):
        return True
    try:
        Image.open(io.BytesIO(r.content)).verify()
        return True
    except Exception:  # noqa: BLE001
        return False


def refresh_image(p: Profile, f: Fetcher, url: str) -> str:
    """Revalidate one cached image (conditional GET when validators exist); retry previous failures."""
    d = p.cache / "images"
    binp, metap = d / f"{url_key(url)}.bin", d / f"{url_key(url)}.json"
    meta = jload(metap, {}) if binp.exists() else {}
    hdrs = {k: meta[m] for k, m in (("If-None-Match", "etag"), ("If-Modified-Since", "last_modified")) if meta.get(m)}
    try:
        r = f.get(url, hdrs, MAX_IMAGE_FETCH_BYTES)
    except Blocked:
        binp.unlink(missing_ok=True)
        return "blocked"
    except Exception as e:  # noqa: BLE001 - transient: keep the last good bytes
        print(f"  image error {url}: {e}")
        return "error"
    if r.status == 304 and binp.exists():
        return "unchanged"
    if r.status == 200 and is_image(r):
        old = binp.read_bytes() if binp.exists() else None
        if old != r.content:
            d.mkdir(parents=True, exist_ok=True)
            binp.write_bytes(r.content)
        jdump(metap, {"url": url, "content_type": r.headers.get("content-type", ""), "etag": r.headers.get("etag"),
                      "last_modified": r.headers.get("last-modified"), "sha256": sha256(r.content)})
        return "unchanged" if old == r.content else ("changed" if old else "new")
    if 400 <= r.status < 500 or r.status == 200:
        binp.unlink(missing_ok=True)
        print(f"  image failed {r.status}: {url}")
        return "failed"
    return "error"


EXT = {"PNG": "png", "JPEG": "jpg", "GIF": "gif", "WEBP": "webp", "BMP": "bmp", "TIFF": "tif"}
CT_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "image/svg+xml": "svg",
          "image/bmp": "bmp"}


def process_image(raw: bytes, content_type: str) -> dict:
    """-> {data, ext, width, height, downscaled}. Downscales (Pillow) until <= MAX_IMAGE_BYTES."""
    try:
        im = Image.open(io.BytesIO(raw))
        ext = EXT.get(im.format or "PNG", "png")
        w, h = im.size
    except Exception:  # noqa: BLE001 - not rasterisable (svg ...): keep bytes as-is
        return {"data": raw, "ext": CT_EXT.get(content_type.split(";")[0], "bin"), "width": None, "height": None,
                "downscaled": False}
    if len(raw) <= MAX_IMAGE_BYTES:
        return {"data": raw, "ext": ext, "width": w, "height": h, "downscaled": False}
    if getattr(im, "is_animated", False):
        return _shrink_animation(im, w, h)
    data, scale = raw, 1.0
    for _ in range(15):
        scale *= 0.8
        r = im.convert("RGB") if ext == "jpg" and im.mode not in ("RGB", "L") else im
        r = r.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        if ext == "jpg":
            r.save(buf, "JPEG", quality=85, optimize=True)
        else:
            r.save(buf, "PNG" if ext != "webp" else "WEBP", optimize=True)
        data = buf.getvalue()
        if len(data) <= MAX_IMAGE_BYTES:
            return {"data": data, "ext": ext if ext not in ("gif", "bmp", "tif") else "png", "width": r.width,
                    "height": r.height, "downscaled": True}
    return {"data": data, "ext": ext, "width": r.width, "height": r.height, "downscaled": True}


def _shrink_animation(im, w: int, h: int) -> dict:
    frames = [(f.convert("RGBA").copy(), f.info.get("duration", 100)) for f in ImageSequence.Iterator(im)]
    scale, data, size = 1.0, b"", (w, h)
    for step in range(20):
        scale *= 0.8
        if step and step % 4 == 0 and len(frames) > 4:
            frames = [(f, d * 2) for f, d in frames[::2]]
        size = (max(1, int(w * scale)), max(1, int(h * scale)))
        out = [f.resize(size, Image.LANCZOS) for f, _ in frames]
        buf = io.BytesIO()
        out[0].save(buf, "GIF", save_all=True, append_images=out[1:], duration=[d for _, d in frames],
                    loop=im.info.get("loop", 0), optimize=True, disposal=2)
        data = buf.getvalue()
        if len(data) <= MAX_IMAGE_BYTES:
            break
    return {"data": data, "ext": "gif", "width": size[0], "height": size[1], "downscaled": True}


def processed(p: Profile, raw: bytes, content_type: str) -> dict:
    """process_image, cached by (sha256 of the original bytes, processing version): new bytes = new entry."""
    base = p.cache / "out" / f"{sha256(raw)}.{PROC_VERSION}"
    if base.with_suffix(".json").exists():
        return {**jload(base.with_suffix(".json")), "data": base.with_suffix(".bin").read_bytes()}
    r = process_image(raw, content_type)
    if r["downscaled"]:
        base.parent.mkdir(parents=True, exist_ok=True)
        base.with_suffix(".bin").write_bytes(r["data"])
        jdump(base.with_suffix(".json"), {k: v for k, v in r.items() if k != "data"})
    return r


# ---------------------------------------------------------------- HTML -> Markdown

ALLOWED = set("p br li ul ol strong b em i u span a img h1 h2 h3 h4 h5 h6 figure figcaption div table thead tbody "
              "tfoot tr td th col colgroup hr sup sub blockquote code pre iframe small caption html body".split())
SENTINEL = "⁣BR⁣"


def slugify(s: str, maxlen: int = 80) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"['’`]", "", s.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if len(s) > maxlen:
        s = s[:maxlen].rsplit("-", 1)[0] if "-" in s[:maxlen] else s[:maxlen]
    return s


def esc_tpl(s: str) -> str:
    """The brain is Go-templated: no '{{' / '}}' may survive."""
    while "{{" in s or "}}" in s:
        s = s.replace("{{", "{ {").replace("}}", "} }")
    return s


def q(s) -> str:
    return json.dumps(s, ensure_ascii=False)  # JSON string == valid YAML double-quoted scalar


def one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def abs_url(href: str, base: str) -> str:
    return "https:" + href if href.startswith("//") else urljoin(base + "/", href)


def video_url(src: str) -> str | None:
    src = "https:" + src if src.startswith("//") else src
    host = urlparse(src).netloc.lower()
    return src if any(host == h or host.endswith("." + h) for h in VIDEO_HOSTS) else None


class Conv(MarkdownConverter):
    def convert_td(self, el, text, parent_tags):  # multi-line cells stay on one table row
        colspan = int(el["colspan"]) if str(el.get("colspan", "")).isdigit() else 1
        t = re.sub(r"(?:\s*\n\s*)+", SENTINEL, text.strip()).replace("|", "\\|")
        return " " + t + " |" * max(1, min(colspan, 50))

    convert_th = convert_td


def to_md(soup: BeautifulSoup) -> str:
    inline_ok = ["td", "th", "a", "li", "p", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "b", "i", "u"]
    return Conv(heading_style="ATX", bullets="-", escape_underscores=False, table_infer_header=False,
                keep_inline_images_in=inline_ok).convert_soup(soup)


def article_images(soup: BeautifulSoup, base: str) -> list[tuple]:
    """Usable <img> tags in document order with absolute URL; skips tracking pixels, data: and local-file srcs."""
    out = []
    for im in soup.find_all("img"):
        src = (im.get("src") or "").strip()
        if not src or src.startswith(("data:", "file:")) or re.match(r"^[A-Za-z]:[\\/]", src):
            continue
        if im.get("width") == "0" and im.get("height") == "0":
            continue
        out.append((im, urljoin(base + "/", src)))
    return out


def clean_soup(soup: BeautifulSoup) -> None:
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for t in soup.find_all(["style", "script", "xml", "head", "title"]):
        t.decompose()
    for t in soup.find_all(True):
        if t.name not in ALLOWED:
            t.unwrap()
    for t in soup.find_all(["span", "figure", "u", "small"]):
        t.unwrap()
    for t in soup.find_all(["strong", "b", "em", "i"]):  # nested same-style emphasis -> single
        if t.find_parent(["strong", "b"] if t.name in ("strong", "b") else ["em", "i"]):
            t.unwrap()
    for br in soup.find_all("br"):  # no leading space on the line after a break
        nxt = br.next_sibling
        if isinstance(nxt, str):
            nxt.replace_with(nxt.lstrip(" \t\n\xa0"))


class Render:
    """Renders one snapshot into a candidate tree; returns the manifest."""

    def __init__(self, p: Profile, adapter, snap: dict, store: dict, out: Path):
        self.p, self.ad, self.out, self.store = p, adapter, out, store
        self.arts = sorted(snap["articles"], key=lambda a: a["id"])
        self.cats = {c["id"]: c for c in snap["categories"]}
        self.secs = {s["id"]: s for s in snap["sections"]}
        self.count = snap["count"]
        self.files: set[str] = set()
        self.cat_slug = {c["id"]: slugify(c["name"]) or str(c["id"]) for c in snap["categories"]}
        self.sec_slug: dict[int, str] = {}
        used: dict[int, set] = {}
        for s in sorted(snap["sections"], key=lambda s: (s["position"], s["id"])):
            sl = slugify(s["name"]) or str(s["id"])
            u = used.setdefault(s["category_id"], set())
            sl = f"{sl}-{s['id']}" if sl in u else sl
            u.add(sl)
            self.sec_slug[s["id"]] = sl
        self.paths = {a["id"]: Path(self.cat_slug[self.secs[a["section_id"]]["category_id"]])
                      / self.sec_slug[a["section_id"]] / f"{a['id']}-{slugify(a['title'], 70) or 'artikel'}.md"
                      for a in self.arts}

    def write(self, rel: Path | str, data: str | bytes) -> None:
        dest = self.out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data if isinstance(data, bytes) else esc_tpl(data).encode())
        self.files.add(str(rel))

    def kind(self, sec_name: str, vids: list, structural, text_len: int) -> str:
        n = sec_name.lower()
        if any(s in n for s in self.p.release_note_sections):
            return "release-note"
        if vids and not structural and text_len <= self.p.video_max_text:
            return "video"
        if any(s in n for s in self.p.faq_sections):
            return "faq"
        return "article"

    def rewrite_links(self, soup: BeautifulSoup, here: Path) -> None:
        for a in soup.find_all("a"):
            href = (a.get("href") or "").strip()
            aid = self.ad.href_article_id(href) if href and not href.startswith(("#", "mailto:", "tel:")) else None
            if aid is not None:
                in_corpus = aid in self.paths
                a["href"] = os.path.relpath(self.paths[aid], here.parent) if in_corpus else self.ad.article_url(aid)
            elif not href or href.startswith("#") or href.lower().startswith("javascript:"):
                a.unwrap()  # dead same-page anchor
            elif href.startswith(("mailto:", "tel:", "http://", "https://")):
                continue
            elif href.startswith("/"):
                a["href"] = abs_url(href, self.p.base_url)
            elif re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", href, re.I):
                a["href"] = "https://" + href
            else:  # legacy relative link (e.g. ShowKnowledgeBaseEntry.do?id=..): unresolvable
                a.unwrap()

    def article(self, a: dict) -> tuple[str, dict]:
        L, here = self.p.labels, self.paths[a["id"]]
        sec = self.secs[a["section_id"]]
        cat = self.cats[sec["category_id"]]
        soup = BeautifulSoup(a.get("body") or "", "lxml")
        clean_soup(soup)
        problems: list[str] = []

        vids = []
        for f in soup.find_all("iframe"):
            src = (f.get("src") or "").strip()
            v = video_url(src)
            para = soup.new_tag("p")
            if v:
                vids.append(v)
                para.append(f"{L['video']}: ")
            else:
                para.append(f"{L['embedded']}: ")
                v = abs_url(src, self.p.base_url) if src else ""
            if v:
                link = soup.new_tag("a", href=v)
                link.string = v
                para.append(link)
                f.replace_with(para)
            else:
                f.decompose()

        plain = one_line(soup.get_text(" "))
        text_len = len(re.sub(r"https?://\S+", "", re.sub(rf"^{re.escape(L['video'])}:\s*", "", plain)).strip())
        structural = soup.find(["img", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"])
        kind = self.kind(sec["name"], vids, structural, text_len)

        usable = article_images(soup, self.p.base_url)
        for im in soup.find_all("img"):
            if not any(im is u for u, _ in usable):
                im.decompose()
        images, external = [], []
        for n, (im, url) in enumerate(usable, 1):
            alt0 = one_line(im.get("alt") or im.get("title") or "")
            alt0 = "" if re.search(r"\.(png|jpe?g|gif|webp)$", alt0, re.I) else alt0
            binp = self.p.cache / "images" / f"{url_key(url)}.bin"
            if not binp.exists():
                problems.append(f"image {n} not mirrored: {url}")
                external.append({"article": str(here), "url": url})
                im["src"], alt = url, alt0 or f"{L['image']} {n}"
            else:
                raw = binp.read_bytes()
                meta = jload(binp.with_suffix(".json"), {})
                r = processed(self.p, raw, meta.get("content_type", ""))
                rel = Path("_assets") / str(a["id"]) / f"{n}.{r['ext']}"
                self.write(rel, r["data"])
                orig = sha256(raw)  # description identity: ORIGINAL bytes, never position
                desc = one_line(self.store.get(orig, {}).get("text", ""))
                desc = "" if desc == UNREADABLE else desc
                if desc:
                    self.write(rel.with_suffix(".md"), desc + "\n")
                alt = desc or alt0 or f"{L['image']} {n}"
                im["src"] = os.path.relpath(rel, here.parent)
                images.append({"path": str(rel), "url": url, "sha256": orig, "bytes": len(r["data"]),
                               "width": r["width"], "height": r["height"], "downscaled": r["downscaled"]})
            im.attrs = {"src": im["src"], "alt": alt.replace("[", "(").replace("]", ")")}

        self.rewrite_links(soup, here)

        for t in soup.find_all("table"):  # layout tables (nested, single row/column, lists in cells) -> blocks
            rows, cells = t.find_all("tr"), t.find_all(["td", "th"])
            if t.find("table") or t.find(["ul", "ol"]) or len(rows) < 2 or len(cells) <= len(rows):
                for c in t.find_all(["td", "th"]):
                    c.name = "div"
                for x in t.find_all(["col", "colgroup"]):
                    x.decompose()
                for x in t.find_all(["tr", "tbody", "thead", "tfoot", "caption"]):
                    x.unwrap()
                t.unwrap()
        for cell in soup.find_all(["td", "th"]):  # line breaks inside cells
            for li in cell.find_all("li"):
                li.insert(0, "• ")
            for t in cell.find_all(["br", "p", "li", "div"]):
                if t.name == "br":
                    t.replace_with("\n")
                else:
                    t.append("\n")
            for t in cell.find_all(["ul", "ol", "p", "li", "div"]):
                t.unwrap()
        heads = soup.find_all(re.compile(r"^h[1-6]$"))  # keep one H1 (the title)
        if any(h.name == "h1" for h in heads):
            for h in heads:
                h.name = f"h{min(6, int(h.name[1]) + 1)}"
        for t in soup.find_all(["p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "div", "strong", "em", "b", "i", "ul",
                                "ol", "a", "blockquote"]):
            if not t.get_text().replace("\xa0", "").strip() and not t.find(["img", "table"]):
                t.decompose()

        body = to_md(soup).replace("\xa0", " ").replace(SENTINEL, "<br>")
        body = re.sub(r"\n[ \t]+(?=\n)", "\n", body)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        if not body:
            body = L["empty"]
            problems.append("empty body")
        if kind == "video":
            body = f"{L['video_only']}\n\n{body}"

        title = one_line(a["title"])
        d_title = a["title"] if len(a["title"]) <= 70 else a["title"][:69].rstrip() + "…"
        desc = L["description"].format(title=d_title, section=sec["name"].strip())
        desc = desc if len(desc) <= 150 else desc[:149].rstrip() + "…"
        fm = [("title", q(title)), ("description", q(one_line(desc))), ("source_url", q(a["html_url"])),
              (self.ad.id_key, a["id"]), ("section", q(sec["name"].strip())), ("category", q(cat["name"].strip())),
              ("locale", q(a["locale"])), ("created_at", q(a["created_at"])), ("updated_at", q(a["updated_at"])),
              ("edited_at", q(a.get("edited_at"))), ("labels", q(a.get("label_names") or [])), ("kind", kind),
              ("has_images", "true" if images else "false")]
        if vids:
            fm.append(("video_url", q(vids[0])))
        fm.append(("content_hash", q(sha256(f"{a['title']}\n{a.get('body') or ''}".encode())[:16])))
        text = ("---\n" + "\n".join(f"{k}: {v}" for k, v in fm)
                + f"\n---\n\n# {title}\n\n{L['source']}: {a['html_url']}\n\n{body}\n")
        info = {"kind": kind, "hash": fm[-1][1][1:-1], "images": images, "external": external, "problems": problems}
        return text, info

    def run(self) -> dict:
        kinds, per_sec, images, external, problems, articles = Counter(), {}, [], [], {}, {}
        for a in self.arts:
            text, info = self.article(a)
            self.write(self.paths[a["id"]], text)
            kinds[info["kind"]] += 1
            per_sec.setdefault(a["section_id"], []).append(a)
            images += info["images"]
            external += info["external"]
            articles[str(a["id"])] = {"path": str(self.paths[a["id"]]), "hash": info["hash"]}
            if info["problems"]:
                problems[a["id"]] = info["problems"]
        cat_secs: dict[int, list] = {}
        for sid, items in per_sec.items():
            s = self.secs[sid]
            items.sort(key=lambda a: (a["position"], a["title"].lower()))
            lines = [f"# {s['name'].strip()}", "",
                     self.p.labels["section_readme"].format(category=self.cats[s["category_id"]]["name"].strip(),
                                                           n=len(items)), ""]
            lines += [f"- [{one_line(a['title'])}]({self.paths[a['id']].name})" for a in items]
            sec_path = Path(self.cat_slug[s["category_id"]]) / self.sec_slug[sid]
            self.write(sec_path / "README.md", "\n".join(lines) + "\n")
            cat_secs.setdefault(s["category_id"], []).append(s)
        cat_order = sorted(cat_secs, key=lambda c: (self.cats[c]["position"], c))
        for cid in cat_order:
            ss = sorted(cat_secs[cid], key=lambda s: (s["position"], s["name"].lower()))
            cat_secs[cid] = ss
            lines = [f"# {self.cats[cid]['name'].strip()}", ""]
            lines += [f"- [{s['name'].strip()}]({self.sec_slug[s['id']]}/README.md) "
                      + self.p.labels["category_readme"].format(n=len(per_sec[s["id"]])) for s in ss]
            self.write(Path(self.cat_slug[cid]) / "README.md", "\n".join(lines) + "\n")
        self.index(kinds, per_sec, cat_secs, cat_order, images)
        self.readme(cat_order)
        for i, pr in problems.items():
            print(f"  {i}: {'; '.join(pr)[:200]}")
        return {"source": self.p.name, "source_count": self.count, "articles": articles, "images": images,
                "external_images": external, "files": sorted(self.files)}

    def index(self, kinds, per_sec, cat_secs, cat_order, images) -> None:
        R = f"/brain/{self.p.target.relative_to(self.p.brain)}"
        idk = self.ad.id_key
        sec_dir = {s: f"{self.cat_slug[self.secs[s]['category_id']]}/{self.sec_slug[s]}" for s in per_sec}
        rn = sorted({sec_dir[a["section_id"]] for a in self.arts
                     if any(x in self.secs[a["section_id"]]["name"].lower() for x in self.p.release_note_sections)})
        biggest = max(per_sec, key=lambda s: (sec_dir[s] not in rn, len(per_sec[s]), -s))
        label = Counter(x for a in self.arts for x in (a.get("label_names") or [])
                        if re.fullmatch(r"[\w -]+", x)).most_common(1)
        linked = Counter(m for a in self.arts for m in re.findall(r"articles/(\d{6,})", a.get("body") or "")
                         if int(m) in self.paths).most_common(1)
        aid = int(linked[0][0]) if linked else self.arts[0]["id"]
        month = max(a["updated_at"] for a in self.arts)[:7]
        L = [f"# Index: {self.p.title}", "",
             f"{len(self.arts)} articles (`{self.p.locale}`), {len(images)} images "
             f"({len({i['sha256'] for i in images})} unique). One file per article with YAML frontmatter (`title`, "
             f"`description`, `source_url`, `{idk}`, `section`, `category`, `labels`, `kind`, `updated_at`, ...). "
             "Each section folder has a `README.md` listing its articles; each category folder one listing its "
             "sections.",
             "", "Image descriptions (`_assets/<id>/<n>.md` and the article alt text) are machine-generated, "
             "derived from the screenshots: not source text.", "",
             "## Counts", "", "| kind | articles |", "|---|---|"]
        L += [f"| {k} | {kinds[k]} |" for k in ("article", "faq", "video", "release-note") if kinds[k]]
        L += ["", "| category (folder) | sections | articles |", "|---|---|---|"]
        L += [f"| `{self.cat_slug[c]}` ({self.cats[c]['name'].strip()}) | {len(cat_secs[c])} | "
              f"{sum(len(per_sec[s['id']]) for s in cat_secs[c])} |" for c in cat_order]
        L += ["", "| section (folder) | articles |", "|---|---|"]
        L += [f"| `{sec_dir[s['id']]}` | {len(per_sec[s['id']])} |" for c in cat_order for s in cat_secs[c]]
        not_rn = "".join(f" -g '!{d}/**'" for d in rn)
        L += ["", "## rg recipes", "",
              "Paths are from `/brain`. Frontmatter values are JSON-quoted on one line. Exclude `README.md`, "
              "`INDEX.md` and `_assets` from body searches.", "", "```sh",
              "# 1. by title", f"rg -i '^title:.*<term>' {R} -g '*.md'", "",
              "# 2. by category/section folder, then read the section README",
              f"rg --files {R}/{sec_dir[biggest].split('/')[0]} | head -30",
              f"cat {R}/{sec_dir[biggest]}/README.md", ""]
        if label:
            L += ["# 3. by label", f"rg -l 'labels:.*{label[0][0]}' {R} -g '*.md'", ""]
        L += ["# 4. by kind (article|faq|video|release-note)", f"rg -l '^kind: (article|faq)$' {R} -g '*.md'", "",
              "# 5. full text in articles" + (" (without release notes)" if rn else ""),
              f"rg -i -l '<term>' {R} -g '*.md' -g '!README.md' -g '!INDEX.md' -g '!_assets/**'{not_rn}", "",
              f"# 6. images by description, then back to the article (folder _assets/<id>/ = {idk})",
              f"rg -i '<term>' {R}/_assets -g '*.md'", f"rg -l '^{idk}: {aid}$' {R} -g '*.md'", "",
              "# 7. recently changed / articles with a video",
              f"rg -l '^updated_at: \"{month}' {R} -g '*.md' -g '!_assets/**'",
              f"rg -l '^video_url:' {R} -g '*.md'", "",
              "# 8. article by id, and who links to it",
              f"rg --files {R} | rg '/{aid}-'", f"rg -l '{aid}-' {R} -g '*.md' -g '!INDEX.md'", "```", ""]
        self.write("INDEX.md", "\n".join(L))

    def readme(self, cat_order) -> None:
        asof = max(max(a["updated_at"], a.get("edited_at") or "") for a in self.arts)[:10]
        L = [f"# {self.p.title}", "", f"Source: {self.ad.home_url} ({self.ad.provenance}). Content as of {asof} "
             f"(latest source update), {len(self.arts)} articles. Start at [INDEX.md](INDEX.md).", ""]
        if self.p.notice:
            L += ["> [!IMPORTANT]", f"> {self.p.notice}", ""]
        L += ["Provenance: read-only snapshot of a public help center, refreshed with the brain-dev kit skill "
              "`brain-kb-mirror`. Text and images belong to their publisher; public access or robots.txt is not "
              "permission to republish. Image descriptions are machine-generated.", "",
              "Layout: `<category>/<section>/<id>-<slug>.md`; images in `_assets/<id>/<n>.<ext>` with a derived "
              "description in `<n>.md` next to them.", "", "## Categories", ""]
        L += [f"- [{self.cats[c]['name'].strip()}]({self.cat_slug[c]}/README.md)" for c in cat_order]
        self.write("README.md", "\n".join(L) + "\n")


# ---------------------------------------------------------------- check

LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
CORE_KEYS = ["title", "description", "source_url", "locale", "kind", "content_hash", "created_at", "updated_at"]
KINDS = {"article", "release-note", "video", "faq"}


def frontmatter(text: str) -> dict | None:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    fm = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(": ")
        try:
            fm[k] = json.loads(v)
        except ValueError:
            fm[k] = v
    return fm


def check_tree(root: Path, man: dict, store: dict, id_key: str, need_descriptions: bool) -> list[str]:
    errs: list[str] = []
    missing = [f for f in man["files"] if not (root / f).is_file()]
    errs += [f"manifest file missing: {f}" for f in missing[:10]]
    arts = {v["path"] for v in man["articles"].values()}
    if len(arts) != man["source_count"]:
        errs.append(f"article count {len(arts)} != source count {man['source_count']}")
    ids = set()
    for rel in sorted(arts):
        fm = frontmatter((root / rel).read_text()) if (root / rel).is_file() else None
        if fm is None:
            errs.append(f"{rel}: no frontmatter")
            continue
        errs += [f"{rel}: missing frontmatter key {k}" for k in [*CORE_KEYS, id_key] if k not in fm]
        if fm.get("kind") not in KINDS:
            errs.append(f"{rel}: bad kind {fm.get('kind')}")
        if len(str(fm.get("description", ""))) > 150:
            errs.append(f"{rel}: description >150 chars")
        if fm.get("kind") == "video" and not fm.get("video_url"):
            errs.append(f"{rel}: kind video without video_url")
        if not Path(rel).name.startswith(f"{fm.get(id_key)}-") or fm.get(id_key) in ids:
            errs.append(f"{rel}: filename/id mismatch or duplicate id")
        ids.add(fm.get(id_key))
    n_links = n_imgs = 0
    for rel in man["files"]:
        if not rel.endswith(".md") or rel in missing:
            continue
        text = (root / rel).read_text()
        if "{{" in text or "}}" in text:
            errs.append(f"{rel}: contains a template delimiter")
        if rel.startswith("_assets/"):
            continue
        for m in LINK.finditer(text):
            target = m.group(1)
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue
            is_img = m.group(0).startswith("!")
            n_imgs += is_img
            n_links += not is_img
            dest = ((root / rel).parent / target.split("#", 1)[0]).resolve()
            if not dest.is_relative_to(root.resolve()) or not dest.exists():
                errs.append(f"{rel}: broken {'image' if is_img else 'link'} -> {target}")
    for sec in {str(Path(a).parent) for a in arts}:
        readme = root / sec / "README.md"
        listed = set(re.findall(r"\]\(([^)]+\.md)\)", readme.read_text())) if readme.is_file() else set()
        errs += [f"{sec}/README.md does not list {Path(a).name}" for a in arts
                 if str(Path(a).parent) == sec and Path(a).name not in listed]
    undescribed = {i["sha256"] for i in man["images"] if i["sha256"] not in store}
    for i in man["images"]:
        text = one_line(store.get(i["sha256"], {}).get("text", ""))
        md = root / Path(i["path"]).with_suffix(".md")
        if text and text != UNREADABLE and (not md.is_file() or md.read_text() != esc_tpl(text) + "\n"):
            errs.append(f"{i['path']}: description file out of date")
    if need_descriptions and undescribed:
        errs.append(f"{len(undescribed)} images without a description: run describe-todo")
    print(f"checked {len(arts)} articles, {n_links} relative links, {n_imgs} image refs, "
          f"{len(man['images'])} images ({len(undescribed)} undescribed)")
    return errs


# ---------------------------------------------------------------- refresh

def safe_path(root: Path, rel: str) -> Path:
    """Live path for a manifest entry: inside root, no symlinked component."""
    dest = root / rel
    if ".." in Path(rel).parts or Path(rel).is_absolute():
        raise MirrorError(f"path escape: {rel}")
    cur = root
    for part in Path(rel).parts:
        cur = cur / part
        if cur.is_symlink():
            raise MirrorError(f"symlink in target tree: {cur}")
    return dest


def swap(root: Path, cand: Path, old: dict | None, new: dict) -> None:
    if root.is_symlink():
        raise MirrorError(f"target is a symlink: {root}")
    keep = set(new["files"])
    plan = [(safe_path(root, rel), cand / rel) for rel in sorted(keep)]
    stale = [safe_path(root, rel) for rel in sorted(set(old["files"] if old else []) - keep)]
    for dest, src in plan:
        data = src.read_bytes()
        if not dest.is_file() or dest.read_bytes() != data:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
    for dest in stale:
        dest.unlink(missing_ok=True)
    for d in sorted({p.parent for p in stale}, key=lambda x: len(x.parts), reverse=True):
        while d != root and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent


def diff_summary(old: dict | None, new: dict, store: dict) -> None:
    oa, na = (old or {}).get("articles", {}), new["articles"]
    oi = {i["path"]: i["sha256"] for i in (old or {}).get("images", [])}
    ni = {i["path"]: i["sha256"] for i in new["images"]}
    added = sorted(set(na) - set(oa))
    changed = sorted(k for k in set(na) & set(oa) if na[k]["hash"] != oa[k]["hash"] or na[k]["path"] != oa[k]["path"])
    deleted = sorted(set(oa) - set(na))
    print(f"articles: {len(na)} (+{len(added)} ~{len(changed)} -{len(deleted)})")
    for tag, ids, src in (("+", added, na), ("~", changed, na), ("-", deleted, oa)):
        for k in ids[:25]:
            print(f"  {tag} {src[k]['path']}")
    print(f"images: {len(ni)} (+{len(set(ni) - set(oi))} ~{sum(oi[k] != ni[k] for k in set(ni) & set(oi))} "
          f"-{len(set(oi) - set(ni))}), not mirrored (external refs): {len(new['external_images'])}")
    print(f"undescribed unique images: {len({s for s in ni.values() if s not in store})}")


def refresh(p: Profile, offline: bool = False, allow_shrink: bool = False, fetcher=None) -> dict:
    ensure_cache(p)
    adapter = ADAPTERS[p.adapter](p)
    snap_path = p.cache / "snapshot.json"
    if offline:
        snap = jload(snap_path)
        if snap is None:
            raise MirrorError("no cached snapshot: run refresh online first")
    else:
        f = fetcher or Fetcher(p.allowed_hosts, p.rate_limit)
        snap = adapter.fetch(f)
    validate_snapshot(snap)
    if not offline:
        urls = list(dict.fromkeys(u for a in snap["articles"]
                                  for _, u in article_images(BeautifulSoup(a.get("body") or "", "lxml"), p.base_url)))
        print(f"listing: {snap['count']} articles; revalidating {len(urls)} images "
              f"(~{len(urls) / p.rate_limit / 60:.0f} min)")
        with ThreadPoolExecutor(4) as ex:  # the shared limiter keeps the request rate
            stats = Counter(ex.map(lambda u: refresh_image(p, f, u), urls))
        print(f"images: {dict(stats)}")
    old = jload(p.manifest)
    if old and snap["count"] < SHRINK_GUARD * old["source_count"] and not allow_shrink:
        raise MirrorError(f"inventory collapse: {old['source_count']} -> {snap['count']} articles; investigate "
                          "before passing --allow-shrink")
    store = jload(p.descriptions, {})
    cand = p.cache / "candidate"
    shutil.rmtree(cand, ignore_errors=True)
    new = Render(p, adapter, snap, store, cand).run()
    errs = check_tree(cand, new, store, adapter.id_key, need_descriptions=False)
    if errs:
        raise MirrorError("candidate failed validation:\n  " + "\n  ".join(errs[:20]))
    if not offline:
        jdump(snap_path, snap)
    diff_summary(old, new, store)
    swap(p.target, cand, old, new)
    jdump(p.manifest, new)
    shutil.rmtree(cand)
    if not old:
        stray = sorted(str(x.relative_to(p.target)) for x in p.target.rglob("*")
                       if x.is_file() and str(x.relative_to(p.target)) not in set(new["files"]))
        if stray:
            print(f"WARNING: {len(stray)} files under the target are not owned by this mirror, e.g. {stray[:5]}")
    return new


# ---------------------------------------------------------------- describe

def est_tokens(img: dict) -> int:
    w, h = img.get("width") or 1000, img.get("height") or 1000
    return min(1600, w * h // 750) + 150  # vision tokens are ~w*h/750, capped after the harness resize


def describe_todo(p: Profile, batch_dir: Path, budget: int) -> list[Path]:
    man, store = jload(p.manifest), jload(p.descriptions, {})
    if man is None:
        raise MirrorError("no manifest: run refresh first")
    todo = {}
    for i in man["images"]:
        if i["sha256"] not in store and i["sha256"] not in todo:
            row = {"sha": i["sha256"], "path": str((p.target / i["path"]).relative_to(p.brain))}
            orig = p.cache / "images" / f"{url_key(i['url'])}.bin"
            if i["downscaled"] and orig.exists():
                row["original"] = str(orig)
            todo[i["sha256"]] = (row, est_tokens(i))
    batch_dir.mkdir(parents=True, exist_ok=True)
    batches, cur, used = [], [], 0
    for row, tok in todo.values():
        if cur and used + tok > budget:
            batches.append(cur)
            cur, used = [], 0
        cur.append(row)
        used += tok
    batches += [cur] if cur else []
    paths = []
    for n, b in enumerate(batches, 1):
        path = batch_dir / f"batch-{n:03d}.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in b))
        paths.append(path)
    print(f"{len(todo)} undescribed unique images -> {len(paths)} batches in {batch_dir} "
          f"(language: {p.annotation_language}, prompt {PROMPT_VERSION})")
    return paths


def describe_apply(p: Profile, files: list[Path]) -> int:
    store, merged, errs = jload(p.descriptions, {}), {}, []
    for out in files:
        assigned_path = out.with_name(out.name.replace(".out.jsonl", ".jsonl"))
        if assigned_path == out or not assigned_path.exists():
            errs.append(f"{out}: no assignment file {assigned_path.name}")
            continue
        rows = map(json.loads, filter(str.strip, assigned_path.read_text().splitlines()))
        assigned = {r["sha"]: r["path"] for r in rows}
        seen = Counter()
        for ln, line in enumerate(out.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                sha, text = r["sha"], one_line(r["text"])
            except (ValueError, KeyError, TypeError):
                errs.append(f"{out.name}:{ln}: not a {{sha, path, text}} object")
                continue
            seen[sha] += 1
            if sha not in assigned or r.get("path") != assigned[sha]:
                errs.append(f"{out.name}:{ln}: sha/path not assigned in this batch")
            elif not text or "\n" in r["text"] or re.search(r"(^#|\*\*|`|^- )", text):
                errs.append(f"{out.name}:{ln}: empty, multi-line or markdown text")
            elif len(text.split()) > 50:
                errs.append(f"{out.name}:{ln}: {len(text.split())} words (prompt asks <=35, hard limit 50)")
            else:
                merged[sha] = {"text": text, "lang": p.annotation_language, "prompt_version": PROMPT_VERSION}
        errs += [f"{out.name}: {s} answered {c}x" for s, c in seen.items() if c > 1]
        errs += [f"{out.name}: {s} missing" for s in set(assigned) - set(seen)]
    if errs:
        print("REJECTED (nothing merged):\n  " + "\n  ".join(errs[:40]))
        return 1
    store.update(merged)
    jdump(p.descriptions, dict(sorted(store.items())))
    print(f"merged {len(merged)} descriptions; store has {len(store)}. Re-rendering offline:")
    refresh(p, offline=True)
    return 0


# ---------------------------------------------------------------- main

def cmd_list(brain: Path) -> None:
    profiles = sorted((brain / SOURCES).glob("*.toml"))
    if not profiles:
        print(f"no profiles in {SOURCES}/ (create <name>.toml, see the skill)")
    for path in profiles:
        p = Profile(brain, path.stem)
        man, store = jload(p.manifest, {}), jload(p.descriptions, {})
        und = len({i["sha256"] for i in man.get("images", []) if i["sha256"] not in store})
        quirks = " +quirks" if path.with_suffix(".quirks.md").exists() else ""
        print(f"{p.name}: {p.adapter} {p.base_url} -> {p.target.relative_to(p.brain)} "
              f"({man.get('source_count', 0)} articles, {und} undescribed images){quirks}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brain", type=Path, default=Path.cwd(), help="brain root (default: cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("refresh")
    r.add_argument("name")
    r.add_argument("--offline", action="store_true", help="re-render from cache + descriptions, no network")
    r.add_argument("--allow-shrink", action="store_true", help="accept a >10%% article drop (after review)")
    t = sub.add_parser("describe-todo")
    t.add_argument("name")
    t.add_argument("--batch-dir", type=Path, required=True)
    t.add_argument("--token-budget", type=int, default=60_000, help="estimated vision tokens per batch")
    a = sub.add_parser("describe-apply")
    a.add_argument("name")
    a.add_argument("files", type=Path, nargs="+")
    c = sub.add_parser("check")
    c.add_argument("name")
    args = ap.parse_args(argv)
    brain = args.brain.resolve()
    try:
        if args.cmd == "list":
            cmd_list(brain)
            return 0
        p = Profile(brain, args.name)
        if args.cmd == "refresh":
            refresh(p, args.offline, args.allow_shrink)
            return 0
        if args.cmd == "describe-todo":
            describe_todo(p, args.batch_dir, args.token_budget)
            return 0
        if args.cmd == "describe-apply":
            return describe_apply(p, args.files)
        man = jload(p.manifest)
        if man is None:
            raise MirrorError("no manifest: run refresh first")
        errs = check_tree(p.target, man, jload(p.descriptions, {}), ADAPTERS[p.adapter].id_key, True)
        print("\n".join(["FAIL:", *errs[:30]]) if errs else "OK")
        return 1 if errs else 0
    except MirrorError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
