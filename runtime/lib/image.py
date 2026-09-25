"""Image generation for a run — the two-step cheap-preview → refine ladder over the broker.

The delta versus "just call an image API": every fresh generation gets a NEW seed, so a text-only
re-prompt at higher quality drifts away from the picture the human approved. We therefore always
land a cheap small ``preview`` (quality low, fewer pixels) first and produce the ``final`` as an
*edit of that preview* — the approved image is sent back as the base, so composition, palette and
lighting carry over. This lib owns the whole ladder (aspect → pixel sizes, quality per step); the
host is dumb and only forwards to the provider. ``edit``/``refine`` keep the base image's own aspect
unless ``aspect`` overrides it.

No provider key ever enters the container: the call goes to the broker mount
``POST http://rc-broker.internal/image/generate``, which is simply absent when the project's image
flag is off — that connection error becomes one clear sentence (``ImageUnavailable``).

    from lib import image
    p = image.generate("a red bicycle in the rain", style="flat-illustration", aspect="4:5")
    image.refine(p)                      # /tmp/outbox/...-final.png, same picture, more pixels
    image.edit(p, "make the sky darker")

CLI (prints one markdown line with the saved path). ``--aspect`` is any W:H from 1:3 to 3:1
(1:1, 3:4, 2:3, 16:9, 21:9 …); edit/refine default to the base image's own ratio:
    python3 -m lib.image generate "<prompt>" [--style ID] [--aspect 1:1] [--step preview|final]
                                  [--ref PATH ...] [--out PATH]
    python3 -m lib.image refine /tmp/outbox/x-preview.png [--prompt "..."] [--aspect W:H] [--ref PATH ...]
    python3 -m lib.image edit /tmp/outbox/x-final.png "make the sky darker" [--aspect W:H]
                              [--step final|preview]
    python3 -m lib.image styles
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import struct
from pathlib import Path

from lib import _http_audit

BROKER_URL = "http://rc-broker.internal/image/generate"

# Image calls are slow (tens of seconds); we optimise for a good picture, not latency.
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 180.0

MAX_REFS = 8  # host cap: 8 `image` parts per request
_RETRY_STATUS = frozenset({502, 503, 504})

# Sizing rule (provider: edges %16, aspect 1:3..3:1, ≥655,360 px, ≤3840 edge, ≤8.3 MP). Final =
# short edge 1024; preview = the smallest %16 short edge whose size clears the pixel floor (+1%).
# Within 1:3..3:1 a 1024 short edge caps the long edge at 3072, so the upper bounds can't bind.
# Cost is set by the quality tier, barely by pixels — see docs/image-generation.md.
MAX_RATIO = 3.0
FINAL_SHORT_EDGE = 1024
PREVIEW_MIN_PIXELS = 662_000  # provider floor 655,360 + ~1%
STEPS = ("preview", "final")
_QUALITY = {"preview": "low", "final": "medium"}
_ASPECT = re.compile(r"\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*")

_RECREATE = (
    "Recreate this exact image at higher resolution and detail. Keep the composition, subjects, "
    "palette, lighting and style unchanged."
)
_MODERATION_HINT = " Rephrase: avoid named people, children's faces and brand names."

# `x-preview.png` → stem `x`, so refine/edit name their output next to the base instead of nesting
# suffixes (`x-preview-final-edit-1.png`).
_STEP_SUFFIX = re.compile(r"-(?:preview|final|edit-\d+)$")


class ImageError(RuntimeError):
    """The host refused or the provider failed — carries the host's own sentence."""


class ImageUnavailable(ImageError):
    """The broker's image mount is absent (feature not enabled for this project)."""


def outbox() -> Path:
    path = Path(os.environ.get("RC_OUTBOX_DIR") or "/tmp/outbox")
    path.mkdir(parents=True, exist_ok=True)
    return path


def styles() -> dict:
    """Parsed ``/skills/image/styles.json``; empty dict when the skill isn't mounted."""
    root = os.environ.get("RC_SKILLS_DIR") or "/skills"
    path = Path(root) / "image" / "styles.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def generate(prompt, *, style=None, aspect="1:1", step="preview", refs=(), out=None) -> str:
    """Generate a fresh image. Start at ``preview``, then ``refine()`` what the human approved."""
    ratio = parse_aspect(aspect)
    text = _scaffold(prompt, style)
    path = Path(out) if out else outbox() / f"{_slug(prompt)}-{step}.png"
    return _render(text, ratio=ratio, step=step, images=list(refs), out=path)


def refine(base, prompt=None, *, aspect=None, step="final", refs=(), out=None) -> str:
    """Re-render an approved image bigger, from the image itself so it stays the same picture."""
    ratio = _ratio_for(base, aspect)
    text = f"{_RECREATE} {prompt}".strip() if prompt else _RECREATE
    path = Path(out) if out else _derive(base, step)
    return _render(text, ratio=ratio, step=step, images=[base, *refs], out=path)


def edit(base, instruction, *, aspect=None, step="final", refs=(), out=None) -> str:
    """Apply a change to an existing image, keeping the rest of it (and its aspect, unless given)."""
    ratio = _ratio_for(base, aspect)
    path = Path(out) if out else _derive(base, "edit")
    return _render(instruction, ratio=ratio, step=step, images=[base, *refs], out=path)


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------


def parse_aspect(aspect: str) -> float:
    """``"3:4"`` → 0.75 (width / height); outside 1:3..3:1 is an ImageError."""
    m = _ASPECT.fullmatch(str(aspect))
    w, h = (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)
    if not (w and h):
        raise ImageError(f"aspect must look like W:H (e.g. 1:1, 3:4, 16:9), got {aspect!r}")
    ratio = w / h
    if not 1 / MAX_RATIO <= ratio <= MAX_RATIO:
        raise ImageError(f"aspect {aspect} is too extreme; the image model accepts 1:3 (tall) to 3:1 (wide)")
    return ratio


def size_for(ratio: float, step: str) -> tuple[int, int]:
    """(width, height) for a width/height ratio at a ladder step."""
    stretch = max(ratio, 1 / ratio)

    def dims(short: int) -> tuple[int, int]:
        long = 16 * round(short * stretch / 16)
        return (long, short) if ratio >= 1 else (short, long)

    if step == "final":
        return dims(FINAL_SHORT_EDGE)
    short = 16
    while math.prod(dims(short)) < PREVIEW_MIN_PIXELS:
        short += 16
    return dims(short)


def _ratio_for(base, aspect) -> float:
    if aspect:
        return parse_aspect(aspect)
    width, height = dimensions(base)
    return min(max(width / height, 1 / MAX_RATIO), MAX_RATIO)


# ---------------------------------------------------------------------------
# Prompt / naming
# ---------------------------------------------------------------------------


def _style_index() -> dict:
    """styles.json keyed by id (an object today; a list of entries is tolerated)."""
    raw = styles()
    entries = raw.get("styles", raw) if isinstance(raw, dict) else raw
    if isinstance(entries, dict):
        return {k: v for k, v in entries.items() if isinstance(v, dict)}
    return {s.get("id"): s for s in entries if isinstance(s, dict) and s.get("id")}


def _scaffold(prompt: str, style: str | None) -> str:
    if not style:
        return prompt
    known = _style_index()
    spec = known.get(style)
    if spec is None:
        raise ImageError(f"unknown style {style!r}; known styles: {', '.join(sorted(known)) or '(none)'}")
    text = str(spec.get("scaffold", "{subject}")).replace("{subject}", prompt)
    negative = str(spec.get("negative", "")).strip()
    return f"{text}\n{negative}" if negative else text


def _slug(prompt: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(prompt).lower()).strip("-")[:40].strip("-")
    return slug or "image"


def _derive(base, step: str) -> Path:
    """`/tmp/outbox/x-preview.png` → `x-final.png`, or the next free `x-edit-N.png`."""
    stem = _STEP_SUFFIX.sub("", Path(base).stem) or "image"
    directory = outbox()
    if step != "edit":
        return directory / f"{stem}-{step}.png"
    n = 1
    while (directory / f"{stem}-edit-{n}.png").exists():
        n += 1
    return directory / f"{stem}-edit-{n}.png"


# ---------------------------------------------------------------------------
# Wire
# ---------------------------------------------------------------------------


def _render(prompt: str, *, ratio: float, step: str, images: list, out: Path) -> str:
    if step not in STEPS:
        raise ImageError(f"unknown step {step!r}; known: {', '.join(STEPS)}")
    if len(images) > MAX_REFS:
        raise ImageError(f"at most {MAX_REFS} reference images per call, got {len(images)}")
    width, height = size_for(ratio, step)
    form = {
        "prompt": prompt,
        "size": f"{width}x{height}",
        "quality": _QUALITY[step],
        "output_format": "png",
    }
    parts = [("image", (Path(p).name, _read_image(p), "application/octet-stream")) for p in images]
    body = _post(form, parts)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    return str(out)


def _read_image(path) -> bytes:
    p = Path(path)
    if not p.is_file():
        raise ImageError(f"reference image not found: {p}")
    return p.read_bytes()


def _post(form: dict, parts: list) -> bytes:
    import requests

    attempt = 1
    reason = "initial"
    while True:
        try:
            # Text fields ride as multipart parts too: with an empty ``files`` list requests would
            # urlencode the body and the host (multipart-only) rejects it.
            resp = _http_audit.request(
                "POST",
                BROKER_URL,
                files=[(k, (None, str(v))) for k, v in form.items()] + parts,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                attempt=attempt,
                reason=reason,
                endpoint_template="/image/generate",
                # The prompt is user content: keep it out of the stderr audit line entirely.
                known_secrets=(form.get("prompt", ""),),
            )
        except requests.exceptions.ConnectionError as exc:
            raise ImageUnavailable("Image generation is not enabled for this project") from exc
        if 200 <= resp.status_code < 300:
            return resp.content
        if resp.status_code in _RETRY_STATUS and attempt == 1:
            attempt, reason = 2, f"retry_status_{resp.status_code}"
            continue
        raise _failure(resp)


def _failure(resp) -> ImageError:
    sentence, kind = "", ""
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            sentence = str(payload.get("error") or "").strip()
            kind = str(payload.get("kind") or "").strip()
    except ValueError:
        pass
    if not sentence:
        sentence = f"Image generation failed (HTTP {resp.status_code})."
    if kind == "moderation":
        sentence += _MODERATION_HINT
    return ImageError(sentence)


# ---------------------------------------------------------------------------
# Pixel geometry (stdlib header parsing — Pillow is not a runtime dependency)
# ---------------------------------------------------------------------------


def dimensions(path) -> tuple[int, int]:
    """(width, height) of a PNG or JPEG, read from its header."""
    data = Path(path).read_bytes()
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker, length = data[i + 1], struct.unpack(">H", data[i + 2 : i + 4])[0]
            # SOF0..SOF15 carry the frame size; DHT/DAC/RSTn/SOS never do.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[i + 5 : i + 9])
                return width, height
            i += 2 + length
    raise ImageError(f"cannot read image dimensions from {path} (expected PNG or JPEG)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _saved_line(path: str, step: str) -> str:
    width, height = dimensions(path)
    return f"Saved {path} ({width}×{height}, {step})"


def _styles_table() -> str:
    rows = list(_style_index().values())
    if not rows:
        return "No styles available (the image skill is not mounted)."
    lines = ["| id | style | looks like |", "|---|---|---|"]
    for s in rows:
        lines.append(f"| `{s.get('id', '')}` | {s.get('label', '')} | {s.get('looks_like', '')} |")
    return "\n".join(lines)


_KEEP_BASE = "W:H override (e.g. 1:1 to make it square); default keeps the base image's own ratio"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.image", description="Generate, refine and edit images — saved to /tmp/outbox."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_gen = sub.add_parser("generate", help="Generate a fresh image from a prompt")
    p_gen.add_argument("prompt")
    p_gen.add_argument("--style", default=None, help="style id from `python -m lib.image styles`")
    p_gen.add_argument("--aspect", default="1:1", help="W:H from 1:3 to 3:1, e.g. 1:1, 3:4, 16:9")
    p_gen.add_argument("--step", default="preview", choices=list(STEPS))
    p_gen.add_argument("--ref", action="append", default=[], help="reference image path (repeatable)")
    p_gen.add_argument("--out", default=None)

    p_ref = sub.add_parser("refine", help="Re-render an approved preview at final quality")
    p_ref.add_argument("base")
    p_ref.add_argument("--prompt", default=None)
    p_ref.add_argument("--aspect", default=None, help=_KEEP_BASE)
    p_ref.add_argument("--step", default="final", choices=list(STEPS))
    p_ref.add_argument("--ref", action="append", default=[])
    p_ref.add_argument("--out", default=None)

    p_edit = sub.add_parser("edit", help="Change one thing about an existing image")
    p_edit.add_argument("base")
    p_edit.add_argument("instruction")
    p_edit.add_argument("--aspect", default=None, help=_KEEP_BASE)
    p_edit.add_argument("--step", default="final", choices=list(STEPS), help="preview = cheap low-quality trial")
    p_edit.add_argument("--ref", action="append", default=[])
    p_edit.add_argument("--out", default=None)

    sub.add_parser("styles", help="List the available styles")

    args = parser.parse_args(argv)
    if args.cmd == "styles":
        print(_styles_table())
        return 0
    if args.cmd == "generate":
        path = generate(
            args.prompt, style=args.style, aspect=args.aspect, step=args.step, refs=args.ref, out=args.out
        )
    elif args.cmd == "refine":
        path = refine(args.base, args.prompt, aspect=args.aspect, step=args.step, refs=args.ref, out=args.out)
    else:
        path = edit(
            args.base, args.instruction, aspect=args.aspect, step=args.step, refs=args.ref, out=args.out
        )
    print(_saved_line(path, args.step))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
