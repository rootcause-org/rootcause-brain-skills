"""HTML → Markdown / text grounding helper.

A thin, read-shaped wrapper over ``markdownify`` (itself over BeautifulSoup) for the recurring
job of turning an HTML blob a project hands us — an email body, a help-centre article, a rendered
page — into compact Markdown (or plain text) an agent and a draft can actually read. Raw HTML is
noise in a model's context and in a customer-facing reply; this collapses it to the words, the
structure, and the links, dropping images/scripts/styles.

Generic by design — every project hits HTML somewhere (Postmark bodies, KnowledgeOwl/Zendesk
articles, scraped pages), so this lives in the shared image rather than any one brain.

    from lib import html
    print(html.to_markdown(postmark_html_body))   # clean Markdown, images stripped, links kept
    print(html.to_text(snippet))                   # just the words

Read-only: it only transforms text in memory, never fetches or writes.
"""

from __future__ import annotations

import re

# Tags that carry no reading value in a grounding/markdown context.
_DROP_TAGS = ["img", "script", "style", "head", "meta", "link"]
# Table-structure tags — unwrapped (kept content, dropped grid) when flatten_tables=True.
_TABLE_TAGS = ["table", "thead", "tbody", "tfoot", "tr", "td", "th", "colgroup", "col"]
_BLANKS_RE = re.compile(r"\n{3,}")


def to_markdown(
    html: str,
    *,
    strip_images: bool = True,
    flatten_tables: bool = False,
    bullets: str = "-",
    heading_style: str = "ATX",
) -> str:
    """Convert an HTML fragment or document to compact Markdown.

    Scripts/styles/head are removed **subtree and all** (decomposed before conversion — passing
    them to markdownify's ``strip`` would drop the tag but keep its text, leaking raw CSS/JS);
    images are dropped too unless ``strip_images=False``. Links are kept; runs of 3+ blank lines
    collapse to one. Both deps are imported lazily so the whole ``lib`` package still loads where
    they aren't installed.

    ``flatten_tables=True`` unwraps table tags so cell text flows as plain prose instead of a
    Markdown grid — the right choice for **layout-table** HTML (most HTML emails), where the grid
    is noise. Leave it False (default) when tables carry real data (a pricing/spec table) you want
    to keep as a Markdown table.

    Returns ``""`` for falsy/empty input rather than raising — grounding code stays terse.
    """
    if not html:
        return ""
    from markdownify import markdownify

    drop = list(_DROP_TAGS) if strip_images else [t for t in _DROP_TAGS if t != "img"]
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for el in soup(drop):
            el.decompose()
        if flatten_tables:
            # Give each row a line break so flattened cells don't run together, then drop the grid.
            for tr in soup("tr"):
                tr.append(soup.new_tag("br"))
            for el in soup(_TABLE_TAGS):
                el.unwrap()
        source = str(soup)
    except Exception:
        source = html  # bs4 unavailable — markdownify still does most of the job
    md = markdownify(source, bullets=bullets, heading_style=heading_style)
    return _BLANKS_RE.sub("\n\n", md).strip()


def to_text(html: str) -> str:
    """Plain text: tags removed, entities decoded, whitespace collapsed.

    Use when you want only the words (no Markdown structure) — e.g. keyword matching or a terse
    one-line summary. Falls back to a regex strip if ``bs4`` isn't importable.
    """
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup

        text = BeautifulSoup(html, "html.parser").get_text(separator=" ")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    return " ".join(text.split())
