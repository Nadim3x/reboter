"""
Offline tests for uploader.py (Zernio integration).

They stub ``requests`` so no network is needed and assert the calls match the
documented Zernio flow: GET /v1/accounts -> POST /v1/media/presign -> PUT file
-> POST /v1/posts (publishNow), followed by GET /v1/posts/{postId} polling
while a platform is still "processing". They also cover the cover-image fields
(Instagram instagramThumbnail, TikTok video_cover_image_url and
mediaItems[].thumbnail for Facebook/YouTube/LinkedIn). Run with:

    python3 -m unittest discover -s tests -v
"""

import json
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uploader  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else (json.dumps(payload) if payload is not None else "")
        self.headers = headers or {}
        self.reason = "reason"

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


ACCOUNTS = {
    "accounts": [
        {"_id": "66b2e19d8c3f5a7e9d0b1c2d", "platform": "instagram", "username": "acme", "isActive": True},
        {"_id": "66b2e19d8c3f5a7e9d0b1c2e", "platform": "tiktok", "username": "acme_tt", "isActive": True},
    ]
}
ACCOUNTS_WITH_FACEBOOK = {
    "accounts": ACCOUNTS["accounts"] + [
        {"_id": "66b2e19d8c3f5a7e9d0b1c2f", "platform": "facebook", "username": "acme_fb", "isActive": True},
    ]
}
PRESIGN = {
    "uploadUrl": "https://bucket.r2.cloudflarestorage.com/temp/123_video.mp4?X-Amz-Signature=abc",
    "publicUrl": "https://media.zernio.com/temp/123_video.mp4",
    "key": "temp/123_video.mp4",
    "expiresIn": 3600,
}
THUMB_URL = "https://media.zernio.com/temp/123_default.png"
THUMB_PRESIGN = dict(PRESIGN, publicUrl=THUMB_URL,
                     uploadUrl="https://bucket.r2.cloudflarestorage.com/temp/123_default.png?X-Amz-Signature=def",
                     key="temp/123_default.png")

PUBLISHED_BOTH = {"post": {"_id": "p1", "status": "published", "platforms": [
    {"platform": "instagram", "accountId": "66b2e19d8c3f5a7e9d0b1c2d", "status": "published"},
    {"platform": "tiktok", "accountId": "66b2e19d8c3f5a7e9d0b1c2e", "status": "published"},
]}}


def base_config(**overrides):
    cfg = {
        "telegram_token": "x",
        "zernio_api_key": "sk_" + "a" * 64,
        "zernio_api_base_url": "https://zernio.com/api/v1",
        "default_thumbnail": "thumbnails/default.jpg",
        "post_to": ["instagram", "tiktok"],
        "captions": ["hi"],
    }
    cfg.update(overrides)
    return cfg


def png_bytes(width, height):
    """Minimal PNG header carrying real IHDR dimensions."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13) + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
    )


def jpeg_bytes(width, height):
    """SOI + APP0 + SOF0 — enough for image_dimensions()."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def write_thumbnail(directory, name="default.png", width=1080, height=1920):
    path = os.path.join(directory, name)
    data = jpeg_bytes(width, height) if name.lower().endswith((".jpg", ".jpeg")) else png_bytes(width, height)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


class NormalizeBaseUrlTests(unittest.TestCase):
    def test_default_and_documented_values(self):
        for value in (None, "", "  ", "https://zernio.com/api/v1", "https://zernio.com/api/v1/",
                      "https://zernio.com/api", "https://zernio.com", "http://www.zernio.com/api/"):
            self.assertEqual(uploader.normalize_api_base_url(value), "https://zernio.com/api/v1", value)

    def test_legacy_values_from_older_releases_are_remapped(self):
        for value in ("https://api.zernio.com", "https://api.zernio.com/v1", "https://api.zernio.com/v1/upload",
                      "https://zernio.com/api/v1/upload"):
            self.assertEqual(uploader.normalize_api_base_url(value), "https://zernio.com/api/v1", value)

    def test_custom_host_gets_version_segment_once(self):
        self.assertEqual(uploader.normalize_api_base_url("https://proxy.example.com"), "https://proxy.example.com/v1")
        self.assertEqual(uploader.normalize_api_base_url("https://proxy.example.com/v1/"), "https://proxy.example.com/v1")
        self.assertEqual(uploader.normalize_api_base_url("http://localhost:8080/api/v1/upload"), "http://localhost:8080/api/v1")

    def test_rejects_garbage(self):
        for value in ("zernio.com", "ftp://zernio.com", "https://user:pw@zernio.com", "https://zernio.com/?x=1",
                      "https://zernio.com/#frag", "https://zer nio.com", "https://zernio.com:abc"):
            with self.assertRaises(ValueError, msg=value):
                uploader.normalize_api_base_url(value)

    def test_api_url_join(self):
        self.assertEqual(uploader.api_url("https://zernio.com/api/v1", "/posts"), "https://zernio.com/api/v1/posts")
        self.assertEqual(uploader.api_url("https://zernio.com/api/v1/", "media/presign"),
                         "https://zernio.com/api/v1/media/presign")


class ImageDimensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_png_and_jpeg_sizes(self):
        png = write_thumbnail(self.tmp.name, "a.png", 1080, 1920)
        jpg = write_thumbnail(self.tmp.name, "b.jpg", 1920, 1080)
        self.assertEqual(uploader.image_dimensions(png), (1080, 1920))
        self.assertEqual(uploader.image_dimensions(jpg), (1920, 1080))

    def test_unreadable_file_returns_none(self):
        broken = os.path.join(self.tmp.name, "broken.png")
        with open(broken, "wb") as fh:
            fh.write(b"not an image at all")
        self.assertIsNone(uploader.image_dimensions(broken))
        self.assertIsNone(uploader.image_dimensions(os.path.join(self.tmp.name, "missing.png")))

    def test_cover_note_only_for_non_vertical_images(self):
        vertical = write_thumbnail(self.tmp.name, "v.png", 1080, 1920)
        square = write_thumbnail(self.tmp.name, "s.png", 1080, 1080)
        self.assertIsNone(uploader._cover_note(vertical, ["instagram", "tiktok"]))
        self.assertIn("9:16", uploader._cover_note(square, ["instagram", "tiktok"]))
        # No Instagram target -> nothing to warn about
        self.assertIsNone(uploader._cover_note(square, ["tiktok"]))


class UploadVideoFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.video = os.path.join(self.tmp.name, "abc123.mp4")
        with open(self.video, "wb") as fh:
            fh.write(b"\x00" * 2048)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, config, responses, put_status=200, thumbnail=None, monotonic=None):
        """Drive upload_video with scripted API responses; record every call.

        ``thumbnail`` is the explicit path passed to upload_video (bot.py always
        passes the configured one); ``monotonic`` overrides time.monotonic so a
        still-processing post can be tested without real waiting.
        """
        queue = list(responses)

        def fake_request(method, url, headers=None, timeout=None, **kw):
            self.calls.append({"method": method, "url": url, "headers": headers, "json": kw.get("json"), "timeout": timeout})
            if not queue:
                raise AssertionError(f"unexpected call {method} {url}")
            return queue.pop(0)

        def fake_put(url, data=None, headers=None, timeout=None):
            body = data.read() if hasattr(data, "read") else data
            self.calls.append({"method": "PUT", "url": url, "headers": headers, "size": len(body), "timeout": timeout})
            return FakeResponse(put_status, None, text="")

        patches = [
            mock.patch.object(uploader, "load_config", return_value=config),
            mock.patch.object(uploader.requests, "request", side_effect=fake_request),
            mock.patch.object(uploader.requests, "put", side_effect=fake_put),
            mock.patch("time.sleep"),   # the status polling loop must not really wait
        ]
        if monotonic is not None:
            patches.append(mock.patch.object(uploader.time, "monotonic", side_effect=monotonic))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return uploader.upload_video(self.video, "Follow for more 🔥", thumbnail)

    def test_happy_path_uses_documented_flow(self):
        created = {
            "message": "Post published successfully",
            "post": {
                "_id": "65f1c0a9e2b5af0012ab34cd",
                "status": "published",
                "platforms": [
                    {"platform": "instagram", "accountId": "66b2e19d8c3f5a7e9d0b1c2d", "status": "published",
                     "platformPostUrl": "https://www.instagram.com/p/DGx7Yk2ScAb/"},
                    {"platform": "tiktok", "accountId": "66b2e19d8c3f5a7e9d0b1c2e", "status": "published",
                     "platformPostUrl": None},
                ],
            },
        }
        result = self._run(base_config(), [
            FakeResponse(200, ACCOUNTS),
            FakeResponse(200, PRESIGN),
            FakeResponse(201, created),
        ])

        # 1. accounts  2. presign  3. PUT  4. posts
        self.assertEqual([c["method"] for c in self.calls], ["GET", "POST", "PUT", "POST"])
        self.assertEqual(self.calls[0]["url"], "https://zernio.com/api/v1/accounts")
        self.assertEqual(self.calls[1]["url"], "https://zernio.com/api/v1/media/presign")
        self.assertEqual(self.calls[1]["json"], {"filename": "abc123.mp4", "contentType": "video/mp4", "size": 2048})
        self.assertEqual(self.calls[2]["url"], PRESIGN["uploadUrl"])
        self.assertEqual(self.calls[2]["headers"], {"Content-Type": "video/mp4"})  # no Authorization on PUT
        self.assertEqual(self.calls[2]["size"], 2048)
        self.assertEqual(self.calls[3]["url"], "https://zernio.com/api/v1/posts")
        for call in (self.calls[0], self.calls[1], self.calls[3]):
            self.assertEqual(call["headers"]["Authorization"], "Bearer " + base_config()["zernio_api_key"])
        self.assertIn("x-request-id", self.calls[3]["headers"])

        body = self.calls[3]["json"]
        self.assertEqual(body["content"], "Follow for more 🔥")
        self.assertEqual(body["mediaItems"], [{"url": PRESIGN["publicUrl"], "type": "video"}])
        self.assertTrue(body["publishNow"])
        self.assertEqual(body["platforms"], [
            {"platform": "instagram", "accountId": "66b2e19d8c3f5a7e9d0b1c2d"},
            {"platform": "tiktok", "accountId": "66b2e19d8c3f5a7e9d0b1c2e"},
        ])
        # TikTok legal/consent + privacy fields are mandatory on every post
        for key in ("privacy_level", "content_preview_confirmed", "express_consent_given"):
            self.assertIn(key, body["tiktokSettings"])

        self.assertTrue(result["success"])
        self.assertEqual(result["instagram"], "posted — https://www.instagram.com/p/DGx7Yk2ScAb/")
        self.assertEqual(result["tiktok"], "posted")
        self.assertEqual(result["posted"], ["instagram", "tiktok"])
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["post_id"], "65f1c0a9e2b5af0012ab34cd")
        self.assertTrue(result["message"].startswith("Posted to 2/2 platforms"))

    def test_config_overrides_are_honoured(self):
        config = base_config(
            zernio_api_base_url="https://api.zernio.com",           # legacy value -> remapped
            zernio_account_ids={"instagram": "aaaaaaaaaaaaaaaaaaaaaaaa", "tiktok": "bbbbbbbbbbbbbbbbbbbbbbbb"},
            tiktok_settings={"privacy_level": "SELF_ONLY", "is_aigc": True},
            instagram_settings={"shareToFeed": False},
        )
        created = {"post": {"_id": "p1", "status": "published", "platforms": [
            {"platform": "instagram", "status": "published"}, {"platform": "tiktok", "status": "published"}]}}
        result = self._run(config, [FakeResponse(200, PRESIGN), FakeResponse(201, created)])

        # No GET /accounts when both ids are pinned in config
        self.assertEqual([c["method"] for c in self.calls], ["POST", "PUT", "POST"])
        self.assertTrue(all(c["url"].startswith("https://zernio.com/api/v1/") for c in self.calls if c["method"] != "PUT"))
        body = self.calls[2]["json"]
        self.assertEqual(body["platforms"][0], {"platform": "instagram", "accountId": "aaaaaaaaaaaaaaaaaaaaaaaa",
                                                 "platformSpecificData": {"shareToFeed": False}})
        self.assertEqual(body["tiktokSettings"]["privacy_level"], "SELF_ONLY")
        self.assertTrue(body["tiktokSettings"]["is_aigc"])
        self.assertTrue(body["tiktokSettings"]["express_consent_given"])
        self.assertTrue(result["success"])

    def test_partial_207_reports_per_platform(self):
        partial = {"post": {"_id": "p2", "status": "partial", "platforms": [
            {"platform": "instagram", "status": "published", "platformPostUrl": "https://www.instagram.com/p/x/"},
            {"platform": "tiktok", "status": "failed", "errorMessage": "TikTok direct posting is at capacity right now",
             "errorCategory": "platform_capacity"},
        ]}}
        result = self._run(base_config(), [FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN), FakeResponse(207, partial)])
        self.assertTrue(result["success"])
        self.assertTrue(result["instagram"].startswith("posted"))
        self.assertEqual(result["tiktok"], "failed: TikTok direct posting is at capacity right now [platform_capacity]")
        self.assertEqual(result["failed"], ["tiktok"])
        self.assertTrue(result["message"].startswith("Partially posted: 1/2"))

    def test_processing_platform_is_polled_until_published(self):
        """The bug report: Instagram answered "processing" -> it was reported as a failure."""
        created = {"post": {"_id": "p5", "status": "publishing", "platforms": [
            {"platform": "instagram", "accountId": "66b2e19d8c3f5a7e9d0b1c2d", "status": "processing"},
            {"platform": "tiktok", "accountId": "66b2e19d8c3f5a7e9d0b1c2e", "status": "published"},
        ]}}
        # GET /v1/posts/{postId} returns accountId as an object, not a string
        refreshed = {"post": {"_id": "p5", "status": "published", "platforms": [
            {"platform": "instagram", "accountId": {"_id": "66b2e19d8c3f5a7e9d0b1c2d"}, "status": "published",
             "platformPostUrl": "https://www.instagram.com/reel/abc/"},
            {"platform": "tiktok", "accountId": {"_id": "66b2e19d8c3f5a7e9d0b1c2e"}, "status": "published"},
        ]}}

        result = self._run(base_config(), [
            FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN),
            FakeResponse(201, created), FakeResponse(200, refreshed),
        ])

        self.assertEqual([c["method"] for c in self.calls], ["GET", "POST", "PUT", "POST", "GET"])
        self.assertEqual(self.calls[-1]["url"], "https://zernio.com/api/v1/posts/p5")
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["instagram"], "posted — https://www.instagram.com/reel/abc/")
        self.assertEqual(result["post_status"], "published")
        self.assertTrue(result["message"].startswith("Posted to 2/2 platforms"))

    def test_still_publishing_post_is_reported_as_processing_not_failure(self):
        created = {"post": {"_id": "p7", "status": "publishing", "platforms": [
            {"platform": "instagram", "accountId": "66b2e19d8c3f5a7e9d0b1c2d", "status": "processing"},
            {"platform": "tiktok", "accountId": "66b2e19d8c3f5a7e9d0b1c2e", "status": "published"},
        ]}}
        still = {"post": {"_id": "p7", "status": "publishing", "platforms": [
            {"platform": "instagram", "status": "processing"},
            {"platform": "tiktok", "status": "published"},
        ]}}

        # time.monotonic: first call sets the deadline, the second jumps past it
        result = self._run(
            base_config(post_status_wait_seconds=120),
            [FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN),
             FakeResponse(201, created), FakeResponse(200, still)],
            monotonic=[0.0, 1000.0],
        )

        self.assertTrue(result["success"])                      # TikTok published
        self.assertEqual(result["posted"], ["tiktok"])
        self.assertEqual(result["pending"], ["instagram"])
        self.assertEqual(result["failed"], [])                  # not a failure any more
        self.assertEqual(result["instagram"], "processing — check the Zernio dashboard")
        self.assertIn("still processing", result["message"])
        self.assertNotIn("Partially posted", result["message"])
        self.assertEqual(result["post_status"], "publishing")

    def test_status_polling_disabled_with_zero_wait(self):
        created = {"post": {"_id": "p8", "status": "publishing", "platforms": [
            {"platform": "instagram", "status": "processing"},
            {"platform": "tiktok", "status": "published"},
        ]}}
        result = self._run(base_config(post_status_wait_seconds=0),
                           [FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN), FakeResponse(201, created)])
        self.assertEqual([c["method"] for c in self.calls], ["GET", "POST", "PUT", "POST"])  # no GET /posts/{id}
        self.assertEqual(result["pending"], ["instagram"])

    def test_status_poll_failure_keeps_the_create_response(self):
        created = {"post": {"_id": "p9", "status": "publishing", "platforms": [
            {"platform": "instagram", "status": "processing"},
            {"platform": "tiktok", "status": "published"},
        ]}}
        gone = {"error": "Post not found", "type": "not_found", "code": "post_not_found"}
        result = self._run(base_config(),
                           [FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN),
                            FakeResponse(201, created), FakeResponse(404, gone)])
        self.assertEqual(result["pending"], ["instagram"])
        self.assertEqual(result["posted"], ["tiktok"])
        self.assertTrue(result["success"])

    def test_endpoint_not_found_gives_actionable_hint(self):
        """The exact failure users saw with the old code, now surfaced with a fix hint."""
        not_found = {"error": "No such API endpoint: POST /api/v1/posts", "type": "not_found", "code": "endpoint_not_found"}
        result = self._run(base_config(zernio_api_base_url="https://proxy.example.com/wrong"),
                           [FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN), FakeResponse(404, not_found)])
        self.assertFalse(result["success"])
        self.assertIn("endpoint_not_found", result["instagram"])
        self.assertIn("https://zernio.com/api/v1", result["instagram"])

    def test_invalid_api_key(self):
        unauthorized = {"error": "Invalid API key", "type": "authentication_error", "code": "invalid_credentials"}
        result = self._run(base_config(), [FakeResponse(401, unauthorized)])
        self.assertFalse(result["success"])
        self.assertEqual([c["method"] for c in self.calls], ["GET"])  # stops before uploading anything
        self.assertIn("invalid_credentials", result["instagram"])
        self.assertIn("API key", result["instagram"])

    def test_missing_platform_account(self):
        only_ig = {"accounts": [ACCOUNTS["accounts"][0]]}
        created = {"post": {"_id": "p3", "status": "published", "platforms": [{"platform": "instagram", "status": "published"}]}}
        result = self._run(base_config(), [FakeResponse(200, only_ig), FakeResponse(200, PRESIGN), FakeResponse(201, created)])
        self.assertTrue(result["success"])           # Instagram still goes out
        self.assertIn("no tiktok account is connected", result["tiktok"])
        body = self.calls[3]["json"]
        self.assertEqual([p["platform"] for p in body["platforms"]], ["instagram"])
        self.assertNotIn("tiktokSettings", body)

    def test_duplicate_409(self):
        dup = {"error": "Duplicate post", "type": "invalid_request_error", "code": "duplicate_post",
               "platform": "instagram", "accountId": "66b2e19d8c3f5a7e9d0b1c2d", "existingPostId": "old123"}
        result = self._run(base_config(), [FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN), FakeResponse(409, dup)])
        self.assertFalse(result["success"])
        self.assertIn("duplicate", result["instagram"])
        self.assertIn("old123", result["instagram"])
        self.assertEqual(result["post_id"], "old123")

    def test_storage_put_failure(self):
        result = self._run(base_config(), [FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN)], put_status=403)
        self.assertFalse(result["success"])
        self.assertEqual([c["method"] for c in self.calls], ["GET", "POST", "PUT"])  # never reaches /posts
        self.assertIn("storage", result["tiktok"])

    def test_placeholder_key_and_missing_file(self):
        with mock.patch.object(uploader, "load_config", return_value=base_config(zernio_api_key="YOUR_ZERNIO_API_KEY")):
            result = uploader.upload_video(self.video, "c")
        self.assertFalse(result["success"])
        self.assertIn("not configured", result["message"])

        with mock.patch.object(uploader, "load_config", return_value=base_config()):
            result = uploader.upload_video(os.path.join(self.tmp.name, "nope.mp4"), "c")
        self.assertFalse(result["success"])
        self.assertEqual(result["instagram"], "failed: video file missing")

    def test_unsupported_extension(self):
        mkv = os.path.join(self.tmp.name, "v.mkv")
        open(mkv, "wb").close()
        with mock.patch.object(uploader, "load_config", return_value=base_config()):
            result = uploader.upload_video(mkv, "c")
        self.assertFalse(result["success"])
        self.assertIn("Unsupported video format .mkv", result["message"])

    # -- covers -----------------------------------------------------------------

    def test_thumbnail_becomes_the_cover_for_instagram_and_tiktok(self):
        thumb = write_thumbnail(self.tmp.name)          # 1080x1920 PNG
        result = self._run(base_config(default_thumbnail=thumb),
                           [FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN),
                            FakeResponse(200, THUMB_PRESIGN), FakeResponse(201, PUBLISHED_BOTH)],
                           thumbnail=thumb)

        # video presign + PUT, cover presign + PUT, then the post itself
        self.assertEqual([c["method"] for c in self.calls], ["GET", "POST", "PUT", "POST", "PUT", "POST"])
        body = self.calls[-1]["json"]
        # Instagram and TikTok ignore mediaItems[].thumbnail …
        self.assertNotIn("thumbnail", body["mediaItems"][0])
        # … so the cover goes into the field each platform actually reads.
        self.assertEqual(body["platforms"][0]["platformSpecificData"], {"instagramThumbnail": THUMB_URL})
        self.assertEqual(body["tiktokSettings"]["video_cover_image_url"], THUMB_URL)
        self.assertEqual(result["thumbnail"]["platforms"], ["instagram", "tiktok"])
        self.assertIsNone(result["thumbnail"]["note"])   # already 9:16

    def test_incorrectly_shaped_cover_is_flagged_in_the_result(self):
        thumb = write_thumbnail(self.tmp.name, "wide.png", 1920, 1080)
        result = self._run(base_config(default_thumbnail=thumb),
                           [FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN),
                            FakeResponse(200, THUMB_PRESIGN), FakeResponse(201, PUBLISHED_BOTH)],
                           thumbnail=thumb)
        self.assertIn("9:16", result["thumbnail"]["note"])

    def test_use_thumbnail_false_posts_without_a_cover(self):
        thumb = write_thumbnail(self.tmp.name)
        result = self._run(base_config(default_thumbnail=thumb, use_thumbnail=False),
                           [FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN), FakeResponse(201, PUBLISHED_BOTH)],
                           thumbnail=thumb)
        self.assertEqual([c["method"] for c in self.calls], ["GET", "POST", "PUT", "POST"])
        body = self.calls[-1]["json"]
        self.assertNotIn("thumbnail", body["mediaItems"][0])
        self.assertNotIn("platformSpecificData", body["platforms"][0])
        self.assertNotIn("video_cover_image_url", body["tiktokSettings"])
        self.assertEqual(result["thumbnail"]["platforms"], [])
        self.assertIn("use_thumbnail", result["thumbnail"]["note"])

    def test_legacy_tiktok_cover_flag_is_ignored(self):
        """tiktok_cover_from_thumbnail is deprecated; use_thumbnail decides."""
        thumb = write_thumbnail(self.tmp.name)
        result = self._run(base_config(default_thumbnail=thumb, tiktok_cover_from_thumbnail=False),
                           [FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN),
                            FakeResponse(200, THUMB_PRESIGN), FakeResponse(201, PUBLISHED_BOTH)],
                           thumbnail=thumb)
        body = self.calls[-1]["json"]
        self.assertEqual(body["tiktokSettings"]["video_cover_image_url"], THUMB_URL)
        self.assertEqual(result["thumbnail"]["platforms"], ["instagram", "tiktok"])

    def test_webp_cover_is_used_for_tiktok_only(self):
        thumb = write_thumbnail(self.tmp.name, "cover.webp")
        result = self._run(base_config(default_thumbnail=thumb),
                           [FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN),
                            FakeResponse(200, THUMB_PRESIGN), FakeResponse(201, PUBLISHED_BOTH)],
                           thumbnail=thumb)
        body = self.calls[-1]["json"]
        # Instagram only accepts JPEG/PNG covers, so it is skipped instead of failing the post
        self.assertNotIn("platformSpecificData", body["platforms"][0])
        self.assertEqual(body["tiktokSettings"]["video_cover_image_url"], THUMB_URL)
        self.assertEqual(result["thumbnail"]["platforms"], ["tiktok"])

    def test_custom_cover_in_instagram_settings_wins(self):
        thumb = write_thumbnail(self.tmp.name)
        config = base_config(
            default_thumbnail=thumb,
            instagram_settings={"shareToFeed": False, "instagramThumbnail": "https://cdn.example.com/mine.jpg"},
        )
        self._run(config, [FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN),
                           FakeResponse(200, THUMB_PRESIGN), FakeResponse(201, PUBLISHED_BOTH)],
                  thumbnail=thumb)
        entry = self.calls[-1]["json"]["platforms"][0]
        self.assertEqual(entry["platformSpecificData"]["instagramThumbnail"], "https://cdn.example.com/mine.jpg")
        self.assertFalse(entry["platformSpecificData"]["shareToFeed"])

    def test_facebook_target_gets_the_media_item_thumbnail(self):
        thumb = write_thumbnail(self.tmp.name)
        created = {"post": {"_id": "p6", "status": "published", "platforms": [
            {"platform": "instagram", "status": "published"},
            {"platform": "facebook", "status": "published"}]}}
        result = self._run(base_config(post_to=["instagram", "facebook"], default_thumbnail=thumb),
                           [FakeResponse(200, ACCOUNTS_WITH_FACEBOOK), FakeResponse(200, PRESIGN),
                            FakeResponse(200, THUMB_PRESIGN), FakeResponse(201, created)],
                           thumbnail=thumb)
        body = self.calls[-1]["json"]
        self.assertEqual(body["mediaItems"][0]["thumbnail"], THUMB_URL)
        self.assertNotIn("tiktokSettings", body)
        self.assertIn("facebook", result["thumbnail"]["platforms"])

    def test_missing_thumbnail_is_reported_in_the_result(self):
        result = self._run(base_config(default_thumbnail=os.path.join(self.tmp.name, "nope.png")),
                           [FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN), FakeResponse(201, PUBLISHED_BOTH)])
        self.assertEqual(result["thumbnail"]["platforms"], [])
        self.assertIn("not found", result["thumbnail"]["note"])


class ListAccountsTests(unittest.TestCase):
    def test_list_accounts(self):
        with mock.patch.object(uploader.requests, "request", return_value=FakeResponse(200, ACCOUNTS)) as req:
            accounts = uploader.list_accounts("sk_x", "https://api.zernio.com")
        self.assertEqual(len(accounts), 2)
        self.assertEqual(req.call_args.args, ("GET", "https://zernio.com/api/v1/accounts"))

    def test_list_accounts_error(self):
        with mock.patch.object(uploader.requests, "request",
                               return_value=FakeResponse(404, {"error": "No such API endpoint: GET /api/v1/accounts",
                                                              "type": "not_found", "code": "endpoint_not_found"})):
            with self.assertRaises(uploader.ZernioError) as ctx:
                uploader.list_accounts("sk_x", "https://proxy.example.com")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "endpoint_not_found")


if __name__ == "__main__":
    unittest.main()
