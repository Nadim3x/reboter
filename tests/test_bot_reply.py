"""
Offline tests for the Telegram reply formatting in bot.py.

The reply is what the user actually sees, so these tests pin down the wording of
the three per-platform states (published / still processing / failed) and of the
cover (thumbnail) line. They need ``python-telegram-bot`` and ``yt-dlp``
(``pip install -r requirements.txt``) and are skipped when those are missing:

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:  # the bot imports python-telegram-bot (and yt-dlp through downloader)
    import bot  # noqa: E402
except Exception as exc:  # pragma: no cover - only in a bare environment
    bot = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


def result(**overrides):
    base = {
        "success": True,
        "message": "Posted to 2/2 platforms (Zernio post 6aaeaefbdab9264168639dc8, status published)",
        "instagram": "posted — https://www.instagram.com/reel/XYZ/",
        "tiktok": "posted",
        "platforms": ["instagram", "tiktok"],
        "posted": ["instagram", "tiktok"],
        "pending": [],
        "failed": [],
        "post_id": "6aaeaefbdab9264168639dc8",
        "post_status": "published",
        "thumbnail": {"name": "default.png", "platforms": ["instagram", "tiktok"],
                      "url": "https://media.zernio.com/temp/1_default.png", "note": None},
    }
    base.update(overrides)
    return base


@unittest.skipUnless(bot is not None, f"bot.py is not importable: {IMPORT_ERROR}")
class ReplyFormattingTests(unittest.TestCase):
    def test_all_published_reply(self):
        text, log_state = bot.format_upload_reply(result(), "Follow for more 🔥")
        self.assertEqual(log_state, "SUCCESS")
        self.assertTrue(text.startswith("✅ Posted to Instagram & TikTok!"))
        self.assertIn("Instagram: posted — https://www.instagram.com/reel/XYZ/", text)
        self.assertIn("TikTok: posted", text)
        self.assertIn("🖼️ Cover: default.png → Instagram, TikTok", text)   # cover is reported
        self.assertNotIn("Cover: none", text)

    def test_still_processing_is_not_reported_as_a_failure(self):
        """The bug report: TikTok posted, Instagram still processing."""
        text, log_state = bot.format_upload_reply(result(
            message="Posted to 1/2 platforms — Instagram still processing (Zernio post 6aaeaefbdab9264168639dc8, status publishing)",
            instagram="processing — check the Zernio dashboard",
            posted=["tiktok"],
            pending=["instagram"],
            post_status="publishing",
        ), "Follow for more 🔥")

        self.assertEqual(log_state, "PARTIAL")
        self.assertIn("⚠️ Posted to TikTok (still publishing: Instagram)", text)
        self.assertIn("Instagram: processing", text)
        self.assertNotIn("❌", text)
        self.assertIn("/poststatus 6aaeaefbdab9264168639dc8", text)       # how to re-check later

    def test_nothing_published_yet_but_still_processing(self):
        text, log_state = bot.format_upload_reply(result(
            success=False,
            message="Still processing: Instagram & TikTok (Zernio post p1, status publishing)",
            instagram="processing — check the Zernio dashboard",
            tiktok="publishing — check the Zernio dashboard",
            posted=[],
            pending=["instagram", "tiktok"],
            thumbnail={"name": "default.png", "platforms": ["instagram"], "url": None, "note": None},
        ), "Caption")

        self.assertEqual(log_state, "PENDING")
        self.assertTrue(text.startswith("⏳ Still processing: Instagram, TikTok"))

    def test_partial_failure_reply(self):
        text, log_state = bot.format_upload_reply(result(
            success=True,
            message="Partially posted: 1/2 platforms succeeded",
            tiktok="failed: TikTok direct posting is at capacity right now [platform_capacity]",
            posted=["instagram"],
            failed=["tiktok"],
            thumbnail={"name": None, "platforms": [], "url": None, "note": "covers disabled (use_thumbnail: false)"},
        ), "Caption")

        self.assertEqual(log_state, "PARTIAL")
        self.assertIn("⚠️ Posted to Instagram (failed: TikTok)", text)
        self.assertIn("🖼️ Cover: none — covers disabled (use_thumbnail: false)", text)

    def test_total_failure_reply(self):
        text, log_state = bot.format_upload_reply(result(
            success=False,
            message="Zernio rejected the post: [HTTP 401 invalid_credentials] ...",
            instagram="failed: [HTTP 401 invalid_credentials] ...",
            tiktok="failed: [HTTP 401 invalid_credentials] ...",
            posted=[], failed=["instagram", "tiktok"],
            thumbnail={"name": "default.png", "platforms": [], "url": None, "note": "image not found (thumbnails/default.png)"},
        ), "Caption")

        self.assertEqual(log_state, "FAIL")
        self.assertTrue(text.startswith("❌ Upload failed"))
        self.assertIn("📝 Caption was: Caption", text)
        self.assertIn("image not found", text)

    def test_long_replies_are_truncated_for_telegram(self):
        text, _ = bot.format_upload_reply(result(), "x" * 5000)
        self.assertLessEqual(len(text), 4000)
        self.assertTrue(text.endswith("…"))


if __name__ == "__main__":
    unittest.main()
