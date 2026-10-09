"""Offline fixture tests for brain-kb-mirror (synthetic Zendesk source, no network).

    cd skills/brain-kb-mirror && uv run --no-project --with pytest --with httpx --with beautifulsoup4 \
        --with lxml --with markdownify --with pillow pytest tests -q
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "kb_mirror.py"
spec = importlib.util.spec_from_file_location("kb_mirror", SCRIPT)
kb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kb)

BASE = "https://help.example.com"
API = f"{BASE}/api/v2/help_center/en"
NAME = "example-help"


def png(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), color).save(buf, "PNG")
    return buf.getvalue()


def img(n: int) -> str:
    return f"{BASE}/hc/article_attachments/{n}/shot.png"


def article(aid: int, sid: int, title: str, body: str, pos: int = 0) -> dict:
    return {"id": aid, "title": title, "body": body, "html_url": f"{BASE}/hc/en/articles/{aid}-x", "locale": "en",
            "created_at": "2025-01-01T00:00:00Z", "updated_at": "2025-02-01T00:00:00Z",
            "edited_at": "2025-02-01T00:00:00Z", "label_names": ["setup"], "section_id": sid, "position": pos}


class Source:
    """Fake Fetcher: a mutable Zendesk source with paginated articles and ETag'd images."""

    def __init__(self):
        self.categories = [{"id": 1, "name": "Getting started", "position": 0},
                           {"id": 2, "name": "News", "position": 1}]
        self.sections = [{"id": 10, "name": "Basics", "position": 0, "category_id": 1},
                         {"id": 20, "name": "Release notes", "position": 0, "category_id": 2}]
        self.articles = [
            article(101, 10, "Install the app", f'<p>Step one.</p><img src="{img(1)}"><img src="{img(2)}">'
                    f'<p>See <a href="/hc/en/articles/102-login#h_01">login</a> and {{{{x}}}}.</p>'),
            article(102, 10, "Log in", f'<p>Use your account.</p><img src="{img(3)}">', 1),
            article(201, 20, "Version 2.0", '<p>New: dark mode.</p><a href="ShowKnowledgeBaseEntry.do?id=7">old</a>'),
        ]
        self.images = {img(1): png("red"), img(2): png("green"), img(3): png("blue")}
        self.break_pagination = False
        self.requests: list[str] = []

    def get(self, url, headers=None, cap=None):
        self.requests.append(url)
        if url in self.images:
            data = self.images[url]
            etag = '"' + hashlib.md5(data).hexdigest() + '"'
            if (headers or {}).get("If-None-Match") == etag:
                return kb.Resp(304, {}, b"", url)
            return kb.Resp(200, {"content-type": "image/png", "etag": etag}, data, url)
        for key in ("articles", "sections", "categories"):
            items = getattr(self, key)
            if url == f"{API}/{key}.json?per_page=100":  # page 1 of 2 for articles
                page, nxt = (items[:2], f"{API}/{key}.json?page=2&per_page=100") if key == "articles" else (items, None)
            elif url == f"{API}/{key}.json?page=2&per_page=100":
                if self.break_pagination:
                    return kb.Resp(500, {}, b"", url)
                page, nxt = items[2:], None
            else:
                continue
            body = {key: page, "count": len(items), "next_page": nxt, "page_count": 2 if key == "articles" else 1}
            return kb.Resp(200, {"content-type": "application/json"}, json.dumps(body).encode(), url)
        return kb.Resp(404, {}, b"", url)


@pytest.fixture
def brain(tmp_path):
    root = tmp_path / "brain"
    (root / "_internal/kb-sources").mkdir(parents=True)
    (root / "_internal/kb-sources" / f"{NAME}.toml").write_text(
        f'adapter = "zendesk"\nbase_url = "{BASE}"\nlocale = "en"\ntarget = "knowledge/example-help"\n')
    return root


def refresh(brain, src, **kw):
    return kb.refresh(kb.Profile(brain, NAME), fetcher=src, **kw)


def tree(brain: Path) -> dict[str, bytes]:
    files = [*(brain / "knowledge").rglob("*"), *(brain / "_internal/kb-sources").glob("*.json")]
    return {str(p.relative_to(brain)): p.read_bytes() for p in files if p.is_file()}


def describe_all(brain, tmp_path, text="Login screen with 'Sign in' button."):
    p = kb.Profile(brain, NAME)
    outs = []
    for b in kb.describe_todo(p, tmp_path / "batches", 60_000):
        rows = [json.loads(x) for x in b.read_text().splitlines()]
        out = b.with_name(b.name.replace(".jsonl", ".out.jsonl"))
        out.write_text("".join(json.dumps({**r, "text": f"{text} {r['sha'][:6]}"}) + "\n" for r in rows))
        outs.append(out)
    assert kb.describe_apply(p, outs) == 0


A101 = "knowledge/example-help/getting-started/basics/101-install-the-app.md"


def test_render_shape(brain):
    refresh(brain, Source())
    text = (brain / A101).read_text()
    assert "zendesk_id: 101" in text and "content_hash: " in text and "fetched_at" not in text
    assert f"Source: {BASE}/hc/en/articles/101-x" in text
    assert "](102-log-in.md)" in text and "{ {x} }" in text and "{{" not in text
    assert "![image 1](../../_assets/101/1.png)" in text
    rn = (brain / "knowledge/example-help/news/release-notes/201-version-2-0.md").read_text()
    assert "kind: release-note" in rn and "ShowKnowledgeBaseEntry" not in rn


def test_rerun_is_byte_identical_noop(brain, tmp_path):
    src = Source()
    refresh(brain, src)
    describe_all(brain, tmp_path)
    before = tree(brain)
    refresh(brain, src)
    assert tree(brain) == before
    assert any(r in src.requests for r in src.images)  # images were revalidated, not skipped


def test_empty_cache_rebuild(brain, tmp_path):
    refresh(brain, Source())
    describe_all(brain, tmp_path)
    before = tree(brain)
    shutil.rmtree(brain / "_internal/kb-sources/.cache")
    refresh(brain, Source())
    assert tree(brain) == before


def test_changed_bytes_at_same_url_need_reannotation(brain, tmp_path):
    src = Source()
    refresh(brain, src)
    describe_all(brain, tmp_path)
    md = brain / "knowledge/example-help/_assets/101/1.md"
    assert md.exists()
    src.images[img(1)] = png("yellow")
    refresh(brain, src)
    assert not md.exists()
    assert "![image 1](" in (brain / A101).read_text()
    todo = kb.describe_todo(kb.Profile(brain, NAME), tmp_path / "again", 60_000)
    rows = [json.loads(x) for b in todo for x in b.read_text().splitlines()]
    assert [r["sha"] for r in rows] == [hashlib.sha256(png("yellow")).hexdigest()]


def test_interrupted_pagination_leaves_live_corpus_untouched(brain):
    src = Source()
    refresh(brain, src)
    before = tree(brain)
    src.articles.append(article(103, 10, "New article", "<p>new</p>", 2))
    src.break_pagination = True
    with pytest.raises(kb.MirrorError, match="HTTP 500"):
        refresh(brain, src)
    assert tree(brain) == before


def test_inventory_collapse_stops(brain):
    src = Source()
    refresh(brain, src)
    del src.articles[1:]
    with pytest.raises(kb.MirrorError, match="inventory collapse"):
        refresh(brain, src)


def test_article_and_image_deletion_pruned(brain, tmp_path):
    src = Source()
    refresh(brain, src)
    del src.articles[1]  # 102 and its image
    src.articles[0]["body"] = src.articles[0]["body"].replace(f'<img src="{img(2)}">', "")
    (brain / "knowledge/example-help/handwritten.md").write_text("not owned\n")
    refresh(brain, src, allow_shrink=True)
    k = brain / "knowledge/example-help"
    assert not (k / "getting-started/basics/102-log-in.md").exists()
    assert not (k / "_assets/102").exists() and not (k / "_assets/101/2.png").exists()
    assert (k / "handwritten.md").exists()  # only manifest-owned paths are pruned


def test_reordered_images_keep_descriptions(brain, tmp_path):
    src = Source()
    refresh(brain, src)
    describe_all(brain, tmp_path)
    k = brain / "knowledge/example-help/_assets/101"
    first, second = (k / "1.md").read_text(), (k / "2.md").read_text()
    src.articles[0]["body"] = f'<img src="{img(2)}"><img src="{img(1)}"><p>Swapped.</p>'
    refresh(brain, src)
    assert (k / "1.md").read_text() == second and (k / "2.md").read_text() == first


def test_check_offline_on_fresh_copy(brain, tmp_path):
    src = Source()
    refresh(brain, src)
    assert kb.main(["--brain", str(brain), "check", NAME]) == 1  # undescribed images block publishing
    describe_all(brain, tmp_path)
    fresh = tmp_path / "clone"
    shutil.copytree(brain, fresh, ignore=shutil.ignore_patterns(".cache"))
    assert kb.main(["--brain", str(fresh), "check", NAME]) == 0
    (fresh / A101).write_text((fresh / A101).read_text() + "\n[broken](nope.md)\n")
    assert kb.main(["--brain", str(fresh), "check", NAME]) == 1


def test_describe_apply_rejects_incomplete_batches(brain, tmp_path):
    refresh(brain, Source())
    p = kb.Profile(brain, NAME)
    (batch,) = kb.describe_todo(p, tmp_path / "b", 60_000)
    rows = [json.loads(x) for x in batch.read_text().splitlines()]
    out = batch.with_name("batch-001.out.jsonl")
    out.write_text(json.dumps({**rows[0], "text": "A screen."}) + "\n")  # others missing
    assert kb.describe_apply(p, [out]) == 1
    assert not p.descriptions.exists()


def test_fetcher_blocks_unlisted_and_private_hosts():
    f = kb.Fetcher(["help.example.com", "localhost"], 2.0)
    with pytest.raises(kb.Blocked, match="not allowed"):
        f.check("https://evil.example.net/x.png")
    with pytest.raises(kb.Blocked, match="not allowed"):
        f.check("http://help.example.com/x")
    with pytest.raises(kb.Blocked, match="non-public"):
        f.check("https://localhost/x")
