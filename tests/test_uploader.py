"""
Offline tests for uploader.py (Zernio integration).

They stub ``requests`` so no network is needed and assert the calls match the
documented Zernio flow: GET /v1/accounts -> POST /v1/media/presign -> PUT file
-> POST /v1/posts (publishNow). Run with:

    python3 -m unittest discover -s tests -v
"""

import json
import os
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
PRESIGN = {
    "uploadUrl": "https://bucket.r2.cloudflarestorage.com/temp/123_video.mp4?X-Amz-Signature=abc",
    "publicUrl": "https://media.zernio.com/temp/123_video.mp4",
    "key": "temp/123_video.mp4",
    "expiresIn": 3600,
}


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


class UploadVideoFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.video = os.path.join(self.tmp.name, "abc123.mp4")
        with open(self.video, "wb") as fh:
            fh.write(b"\x00" * 2048)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, config, responses, put_status=200):
        """Drive upload_video with scripted API responses; record every call."""
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

        with mock.patch.object(uploader, "load_config", return_value=config), \
             mock.patch.object(uploader.requests, "request", side_effect=fake_request), \
             mock.patch.object(uploader.requests, "put", side_effect=fake_put):
            return uploader.upload_video(self.video, "Follow for more 🔥", None)

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
        self.assertTrue(result["message"].startswith("Partially posted: 1/2"))

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

    def test_thumbnail_only_uploaded_when_a_platform_can_use_it(self):
        thumb = os.path.join(self.tmp.name, "default.jpg")
        with open(thumb, "wb") as fh:
            fh.write(b"\xff\xd8" * 10)
        created = {"post": {"_id": "p4", "status": "published", "platforms": [
            {"platform": "instagram", "status": "published"}, {"platform": "tiktok", "status": "published"}]}}

        # Instagram + TikTok only: the thumbnail is not uploaded (IG/TikTok ignore it)
        with mock.patch.object(uploader, "load_config", return_value=base_config(default_thumbnail=thumb)), \
             mock.patch.object(uploader.requests, "request",
                               side_effect=[FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN), FakeResponse(201, created)]) as req, \
             mock.patch.object(uploader.requests, "put", return_value=FakeResponse(200, None, text="")) as put:
            uploader.upload_video(self.video, "c", thumb)
        self.assertEqual(put.call_count, 1)
        self.assertNotIn("thumbnail", req.call_args_list[2].kwargs["json"]["mediaItems"][0])

        # tiktok_cover_from_thumbnail: thumbnail is uploaded and used as the TikTok cover
        thumb_presign = dict(PRESIGN, publicUrl="https://media.zernio.com/temp/thumb.jpg")
        with mock.patch.object(uploader, "load_config",
                               return_value=base_config(default_thumbnail=thumb, tiktok_cover_from_thumbnail=True)), \
             mock.patch.object(uploader.requests, "request",
                               side_effect=[FakeResponse(200, ACCOUNTS), FakeResponse(200, PRESIGN),
                                            FakeResponse(200, thumb_presign), FakeResponse(201, created)]) as req, \
             mock.patch.object(uploader.requests, "put", return_value=FakeResponse(200, None, text="")) as put:
            uploader.upload_video(self.video, "c", thumb)
        self.assertEqual(put.call_count, 2)
        body = req.call_args_list[3].kwargs["json"]
        self.assertEqual(body["tiktokSettings"]["video_cover_image_url"], "https://media.zernio.com/temp/thumb.jpg")
        self.assertNotIn("thumbnail", body["mediaItems"][0])


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
