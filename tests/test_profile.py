import contextlib
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import call, patch
import urllib.error

from PIL import Image


dc = types.ModuleType("dynamic_credentials")
dc.add_surrogate_to_request = lambda *args, **kwargs: None
dc.read_response_body = lambda response: response.read()
dc.DynamicCredentialError = type("DynamicCredentialError", (Exception,), {})
loader = SourceFileLoader(
    "filament_profile_test", str(Path(__file__).resolve().parents[1] / "skill/bin/filament")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
filament = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, dynamic_credentials=dc):
    original_path = sys.path[:]
    try:
        loader.exec_module(filament)
    finally:
        sys.path[:] = original_path


class FakeClient:
    def __init__(self):
        self.calls = []

    def handshake(self):
        pass

    def tool_call(self, name, args):
        self.calls.append((name, args))
        return {"content": [{"type": "text", "text": "{}"}]}


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.avatar_dir = self.root / "avatars"
        self.avatar_dir.mkdir()
        self.state_dir = self.root / "state"
        self.client = FakeClient()
        contexts = contextlib.ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(patch.object(filament, "AVATAR_DIR", str(self.avatar_dir)))
        contexts.enter_context(patch.object(filament, "STATE_DIR", str(self.state_dir)))
        self.sleep = contexts.enter_context(patch.object(filament.time, "sleep"))
        contexts.enter_context(contextlib.redirect_stdout(io.StringIO()))
        contexts.enter_context(contextlib.redirect_stderr(io.StringIO()))
        contexts.enter_context(patch.object(
            filament.urllib.request, "urlopen",
            side_effect=AssertionError("unexpected network request"),
        ))

    @property
    def preview(self):
        return self.state_dir / "media/avatar-preview.png"

    def files(self, *names):
        for name in names:
            (self.avatar_dir / name).write_bytes(b"avatar")

    def transparent_image(self):
        img = Image.new("RGBA", (1000, 1000), (0, 0, 0, 0))
        img.paste((0, 0, 0, 255), (300, 300, 400, 400))
        return img

    def source(self, img=None):
        path = self.root / "source.png"
        if img is None:
            img = self.transparent_image()
        img.save(path)
        return str(path)

    def profile(self, path, about=None, crop_spec="250,250,200,200", no_crop=False):
        return filament.cmd_profile(self.client, path, about, crop_spec, no_crop)

    def test_fallback_orders_by_name_digits(self):
        self.files("avatar-100-9.webp", "avatar-200-1.webp",
                   "avatar-300-1.mp4", "face-999-1.webp")
        now = time.time()
        os.utime(self.avatar_dir / "avatar-100-9.webp", (now, now))
        os.utime(self.avatar_dir / "avatar-200-1.webp", (now - 3600, now - 3600))
        self.assertEqual(filament._newest_avatar_webp(),
                         str(self.avatar_dir / "avatar-200-1.webp"))

    def test_fallback_is_numeric_not_lexical(self):
        cases = [
            (("avatar-99-2.webp", "avatar-100-1.webp"), "avatar-100-1.webp"),
            (("avatar-100-1.webp", "avatar-100-2.webp"), "avatar-100-2.webp"),
            (("avatar-100-1.webp", "avatar-0100-01.webp"), "avatar-100-1.webp"),
        ]
        for names, expected in cases:
            with self.subTest(names=names):
                for path in self.avatar_dir.iterdir():
                    path.unlink()
                self.files(*names)
                for order in (list(names), list(reversed(names))):
                    with patch.object(filament.os, "listdir", return_value=order):
                        self.assertEqual(filament._newest_avatar_webp(),
                                         str(self.avatar_dir / expected))

    def test_fallback_skips_directories(self):
        (self.avatar_dir / "avatar-500-1.webp").mkdir()
        self.files("avatar-400-1.webp")
        self.assertEqual(filament._newest_avatar_webp(),
                         str(self.avatar_dir / "avatar-400-1.webp"))

    def test_fallback_none_when_no_match(self):
        self.files("face-999-1.webp")
        self.assertIsNone(filament._newest_avatar_webp())

    def test_opaque_rgba_matches_rgb_bbox(self):
        rgb = Image.new("RGB", (1000, 1000), "white")
        rgb.paste("black", (650, 200, 850, 900))
        rgba = rgb.convert("RGBA")
        self.assertEqual(filament._subject_bbox(rgb), (650, 200, 850, 900))
        self.assertEqual(filament._subject_bbox(rgba), filament._subject_bbox(rgb))

    def test_transparent_background_uses_alpha(self):
        self.assertEqual(filament._subject_bbox(self.transparent_image()),
                         (300, 300, 400, 400))

    def test_alpha_threshold_boundary(self):
        for alpha in (16, 17):
            with self.subTest(alpha=alpha):
                img = Image.new("RGBA", (1000, 1000), (255, 255, 255, alpha))
                img.paste((0, 0, 0, 255), (650, 200, 850, 900))
                self.assertEqual(filament._subject_bbox(img), (650, 200, 850, 900))
                solid = Image.new("RGBA", (100, 100), (0, 0, 0, alpha))
                self.assertIsNone(filament._subject_bbox(solid))

    def test_la_mode_uses_alpha(self):
        img = Image.new("LA", (500, 500), (0, 0))
        img.paste((0, 255), (100, 100, 200, 200))
        self.assertEqual(filament._subject_bbox(img), (100, 100, 200, 200))

    def test_crop_keeps_transparency(self):
        with patch.object(filament, "_upload_bytes", return_value="mxc://x/y"):
            self.profile(self.source())
        with Image.open(self.preview) as preview:
            self.assertEqual(preview.mode, "RGBA")
            self.assertEqual(preview.size, (512, 512))
            self.assertEqual(preview.getpixel((0, 0))[3], 0)

    def test_rgb_source_stays_rgb(self):
        with patch.object(filament, "_upload_bytes", return_value="mxc://x/y"):
            self.profile(self.source(self.transparent_image().convert("RGB")))
        with Image.open(self.preview) as preview:
            self.assertEqual(preview.mode, "RGB")

    def test_preview_bytes_equal_uploaded_bytes(self):
        with patch.object(filament, "_upload_bytes", return_value="mxc://x/y") as upload:
            self.profile(self.source())
        upload.assert_called_once()
        body, filename, ctype = upload.call_args.args
        self.assertEqual(body, self.preview.read_bytes())
        self.assertTrue(filename.endswith("-headshot.png"))
        self.assertEqual(ctype, "image/png")

    def test_set_profile_receives_mxc(self):
        path = self.source()
        for about, expected in (
            (None, {"image": "mxc://x/y"}),
            ("hi", {"image": "mxc://x/y", "about": "hi"}),
        ):
            with self.subTest(about=about):
                self.client.calls.clear()
                with patch.object(filament, "_upload_bytes", return_value="mxc://x/y"):
                    self.profile(path, about=about)
                self.assertEqual(self.client.calls, [("set_profile", expected)])

    def test_bad_image_is_filament_error(self):
        path = self.root / "bad.png"
        path.write_bytes(b"not an image")
        with self.assertRaisesRegex(filament.FilamentError, r"^avatar image:"):
            self.profile(str(path), crop_spec=None)

    def test_preview_write_failure_is_filament_error(self):
        parent = self.root / "regular-file"
        parent.write_bytes(b"file")
        with patch.object(filament, "STATE_DIR", str(parent / "state")), \
                patch.object(filament, "_upload_bytes", return_value="mxc://x/y"):
            with self.assertRaisesRegex(filament.FilamentError, r"^avatar preview:"):
                self.profile(self.source())

    def test_main_writes_last_error_for_bad_image(self):
        path = self.root / "bad.png"
        path.write_bytes(b"not an image")
        with patch.object(sys, "argv", ["filament", "profile", "--avatar", str(path)]), \
                patch.object(filament, "Client", return_value=self.client):
            self.assertEqual(filament.main(), 1)
        self.assertIn("avatar image:", (self.state_dir / "last_error").read_text())

    def test_upload_retries_then_succeeds(self):
        path = self.source()
        failures = [urllib.error.HTTPError(filament.UPLOAD_URL, 503, "down", {}, None)
                    for _ in range(2)]
        response = io.BytesIO(b'{"mxc_url": "mxc://ok/1"}')
        with patch.object(filament.urllib.request, "urlopen",
                          side_effect=failures + [response]) as urlopen:
            self.assertEqual(self.profile(path, crop_spec=None, no_crop=True), 0)
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(2), call(4)])

    def test_upload_auth_error_not_retried(self):
        path = self.source()
        error = urllib.error.HTTPError(filament.UPLOAD_URL, 401, "unauthorised", {}, None)
        with patch.object(filament.urllib.request, "urlopen", side_effect=error) as urlopen:
            with self.assertRaises(filament.FilamentError) as raised:
                self.profile(path, crop_spec=None, no_crop=True)
        self.assertIs(raised.exception.auth, True)
        self.assertEqual(urlopen.call_count, 1)
        self.sleep.assert_not_called()

    def test_upload_read_error_is_filament_error(self):
        path = self.source()
        with patch.object(filament.urllib.request, "urlopen", return_value=io.BytesIO()) as urlopen, \
                patch.object(dc, "read_response_body", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(filament.FilamentError, r"^avatar upload: read error:"):
                self.profile(path, crop_spec=None, no_crop=True)
        self.assertEqual(urlopen.call_count, 4)
        self.assertEqual(self.sleep.call_args_list, [call(2), call(4), call(8)])

    def test_no_crop_read_failure_is_filament_error(self):
        path = self.source()
        with patch("builtins.open", side_effect=OSError("read denied")):
            with self.assertRaisesRegex(filament.FilamentError, r"^avatar file unreadable:"):
                self.profile(path, crop_spec=None, no_crop=True)

    def test_la_and_palette_crop_keep_transparency(self):
        la = self.transparent_image().convert("LA")
        palette = Image.new("P", (1000, 1000), 0)
        palette.putpalette([255, 255, 255, 0, 0, 0] + [0] * 762)
        palette.info["transparency"] = 0
        palette.paste(1, (300, 300, 400, 400))
        for img in (la, palette):
            with self.subTest(mode=img.mode):
                with patch.object(filament, "_upload_bytes", return_value="mxc://x/y"):
                    self.profile(self.source(img))
                with Image.open(self.preview) as preview:
                    self.assertEqual(preview.mode, "RGBA")
                    self.assertEqual(preview.getpixel((0, 0))[3], 0)


if __name__ == "__main__":
    unittest.main()
