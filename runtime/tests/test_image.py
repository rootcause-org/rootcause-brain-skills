"""Unit tests for lib.image — the preview→refine ladder over the broker. No network: the broker is
faked with the `responses` library, exactly like tests/test_api.py.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_image.py -q
"""

import io
import json
import struct
import sys
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from urllib.parse import unquote_plus
from pathlib import Path
from unittest import mock

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import image  # noqa: E402

STYLES = {
    "flat-illustration": {
        "id": "flat-illustration",
        "label": "Flat illustration",
        "looks_like": "Simple drawn shapes in a few flat colours.",
        "scaffold": "Flat vector illustration of {subject}. Bold simple shapes.",
        "negative": "no photorealism, no text",
        "default_aspect": "4:5",
    }
}


def _png(width: int, height: int) -> bytes:
    """A minimal valid PNG carrying real IHDR dimensions (the only part lib.image reads)."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


class ImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        self.outbox = self.tmp / "outbox"
        skills = self.tmp / "skills" / "image"
        skills.mkdir(parents=True)
        (skills / "styles.json").write_text(json.dumps(STYLES), encoding="utf-8")
        self.enterContext(
            mock.patch.dict(
                "os.environ",
                {"RC_OUTBOX_DIR": str(self.outbox), "RC_SKILLS_DIR": str(self.tmp / "skills")},
            )
        )

    def _broker(self, body=None, status=200, content_type="image/png"):
        responses.add(
            responses.POST,
            image.BROKER_URL,
            body=body if body is not None else _png(8, 8),
            status=status,
            content_type=content_type,
        )

    def _form(self, call=0) -> str:
        """Request body as text — urlencoded when there are no files, multipart when there are."""
        body = responses.calls[call].request.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        return unquote_plus(body)

    def _base(self, name="poster-preview.png", size=(736, 928)) -> Path:
        path = self.tmp / name
        path.write_bytes(_png(*size))
        return path


class SizeLadder(ImageTest):
    @responses.activate
    def test_every_aspect_and_step_sends_its_size_and_quality(self):
        for aspect, steps in image.LADDER.items():
            for step, (w, h) in steps.items():
                responses.reset()
                self._broker()
                image.generate("a red bicycle", aspect=aspect, step=step)
                form = self._form()
                self.assertIn(f"{w}x{h}", form, f"{aspect}/{step}")
                self.assertIn("low" if step == "preview" else "medium", form)

    @responses.activate
    def test_default_out_path_is_slugified_prompt(self):
        self._broker()
        path = image.generate("A Red Bicycle, in the Rain!")
        self.assertEqual(path, str(self.outbox / "a-red-bicycle-in-the-rain-preview.png"))
        self.assertTrue(Path(path).is_file())

    @responses.activate
    def test_long_prompt_slug_is_capped(self):
        self._broker()
        path = image.generate("word " * 40)
        self.assertLessEqual(len(Path(path).stem.rsplit("-preview", 1)[0]), 40)


class Styles(ImageTest):
    @responses.activate
    def test_style_scaffold_and_negative(self):
        self._broker()
        image.generate("a red bicycle", style="flat-illustration")
        form = self._form()
        self.assertIn("Flat vector illustration of a red bicycle", form)
        self.assertIn("no photorealism", form)

    def test_unknown_style_lists_known_ids(self):
        with self.assertRaises(image.ImageError) as ctx:
            image.generate("x", style="nope")
        self.assertIn("flat-illustration", str(ctx.exception))

    def test_styles_empty_when_skill_absent(self):
        with mock.patch.dict("os.environ", {"RC_SKILLS_DIR": str(self.tmp / "missing")}):
            self.assertEqual(image.styles(), {})


class Refine(ImageTest):
    @responses.activate
    def test_derives_aspect_sends_base_first_at_medium(self):
        self._broker()
        base = self._base(size=(736, 928))  # 4:5 preview
        out = image.refine(str(base))
        form = self._form()
        self.assertIn("1024x1280", form)  # 4:5 final
        self.assertIn("medium", form)
        self.assertIn("Recreate this exact image", form)
        self.assertIn(b'name="image"; filename="poster-preview.png"', responses.calls[0].request.body)
        self.assertEqual(out, str(self.outbox / "poster-final.png"))

    @responses.activate
    def test_base_first_before_refs(self):
        self._broker()
        base = self._base()
        ref = self.tmp / "ref.png"
        ref.write_bytes(_png(4, 4))
        image.refine(str(base), refs=[str(ref)])
        body = responses.calls[0].request.body
        self.assertLess(body.index(b'filename="poster-preview.png"'), body.index(b'filename="ref.png"'))

    @responses.activate
    def test_aspect_from_landscape_base(self):
        self._broker()
        base = self._base(name="banner-preview.png", size=(1136, 640))  # 16:9
        image.refine(str(base))
        self.assertIn("1824x1024", self._form())

    @responses.activate
    def test_edit_names_next_free_slot(self):
        self._broker()
        self._broker()
        base = self._base()
        first = image.edit(str(base), "make the sky darker")
        second = image.edit(str(base), "and add a bird")
        self.assertEqual(first, str(self.outbox / "poster-edit-1.png"))
        self.assertEqual(second, str(self.outbox / "poster-edit-2.png"))


class Failures(ImageTest):
    @responses.activate
    def test_moderation_error_appends_rephrase_hint(self):
        self._broker(body=json.dumps({"error": "The prompt was rejected.", "kind": "moderation"}), status=400,
                     content_type="application/json")
        with self.assertRaises(image.ImageError) as ctx:
            image.generate("a famous person")
        self.assertIn("The prompt was rejected.", str(ctx.exception))
        self.assertIn("avoid named people", str(ctx.exception))

    @responses.activate
    def test_provider_error_sentence_passes_through_without_retry(self):
        self._broker(body=json.dumps({"error": "Provider is out of capacity.", "kind": "provider"}), status=400,
                     content_type="application/json")
        with self.assertRaises(image.ImageError) as ctx:
            image.generate("x")
        self.assertEqual(str(ctx.exception), "Provider is out of capacity.")
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_retries_once_on_503(self):
        self._broker(body=json.dumps({"error": "busy"}), status=503, content_type="application/json")
        self._broker()
        image.generate("x")
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_no_mount_is_one_clear_sentence(self):
        # Nothing registered ⇒ responses raises requests ConnectionError, like an absent mount.
        with self.assertRaises(image.ImageUnavailable) as ctx:
            image.generate("x")
        self.assertEqual(str(ctx.exception), "Image generation is not enabled for this project")

    def test_refs_limit(self):
        refs = []
        for i in range(9):
            p = self.tmp / f"r{i}.png"
            p.write_bytes(_png(4, 4))
            refs.append(str(p))
        with self.assertRaises(image.ImageError) as ctx:
            image.generate("x", refs=refs)
        self.assertIn("at most 8", str(ctx.exception))

    def test_missing_ref_names_the_path(self):
        with self.assertRaises(image.ImageError) as ctx:
            image.generate("x", refs=["/tmp/nope-does-not-exist.png"])
        self.assertIn("nope-does-not-exist.png", str(ctx.exception))


class Audit(ImageTest):
    @responses.activate
    def test_prompt_never_reaches_the_stderr_audit_line(self):
        self._broker()
        err = io.StringIO()
        with redirect_stderr(err):
            image.generate("a secret internal codename bicycle")
        self.assertIn("RC_HTTP_AUDIT", err.getvalue())
        self.assertNotIn("codename", err.getvalue())


class Cli(ImageTest):
    @responses.activate
    def test_generate_prints_saved_line(self):
        self._broker(body=_png(816, 816))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(image.main(["generate", "a red bicycle"]), 0)
        self.assertEqual(
            buf.getvalue().strip(),
            f"Saved {self.outbox / 'a-red-bicycle-preview.png'} (816×816, preview)",
        )

    def test_styles_prints_markdown_table(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            image.main(["styles"])
        out = buf.getvalue()
        self.assertIn("| `flat-illustration` | Flat illustration |", out)
        self.assertIn("Simple drawn shapes", out)


if __name__ == "__main__":
    unittest.main()
