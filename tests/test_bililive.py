from __future__ import annotations

import io
import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from PIL import Image

from qbot_bililive.service import (
    BiliLiveStore, DynamicItem, _fetch_bili_image, dispatch, fetch_dynamic,
    fetch_dynamic_card, fetch_live, fetch_user,
)
from bot_tools.storage import Identity, Store, ToolError


def response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


class BiliLiveStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.service = BiliLiveStore(self.store)
        self.admin = Identity("bot-a", "group:secret-openid-a", "owner", group_role="owner")
        self.member = Identity("bot-a", "group:secret-openid-a", "member", group_role="member")
        self.other = Identity("bot-a", "group:secret-openid-b", "owner", group_role="owner")

    def test_subscription_is_scoped_encrypted_and_admin_only(self):
        with self.assertRaises(ToolError):
            self.service.subscribe(self.member, 2, "哔哩哔哩")
        self.assertTrue(self.service.subscribe(self.admin, 2, "哔哩哔哩", 10))
        self.assertFalse(self.service.subscribe(self.admin, 2, "哔哩哔哩", 11))
        self.assertEqual(self.service.list(self.admin)[0]["uid"], 2)
        self.assertEqual(self.service.list(self.other), [])
        target = self.service.targets(2, "live")[0]
        self.assertEqual(target.target, "secret-openid-a")
        with self.store.connect() as db:
            encrypted = bytes(db.execute("SELECT target FROM bililive_targets").fetchone()[0])
        self.assertNotIn(b"secret-openid-a", encrypted)
        self.assertTrue((Path(self.temp.name) / "secrets" / "bililive-targets-master.key").exists())

    def test_modes_and_unsubscribe(self):
        self.service.subscribe(self.admin, 2, "用户")
        self.service.set_mode(self.admin, 2, "dynamic", False)
        self.assertEqual(self.service.uids("dynamic"), [])
        self.assertEqual(self.service.uids("live"), [2])
        self.assertEqual(self.service.unsubscribe(self.admin, 2), "用户")
        self.assertEqual(self.service.list(self.admin), [])
        with self.assertRaises(ToolError):
            self.service.unsubscribe(self.admin, 2)

    def test_live_first_poll_seeds_then_transition_notifies(self):
        self.service.subscribe(self.admin, 2, "用户")
        offline = {2: {"uname": "用户", "live_status": 0}}
        self.assertEqual(self.service.update_live(offline, 10), [])
        online = {2: {"uname": "用户", "live_status": 1, "room_id": 99,
                      "title": "测试直播", "area_v2_parent_name": "游戏",
                      "area_v2_name": "其他"}}
        notices = self.service.update_live(online, 20)
        self.assertEqual(len(notices), 1)
        self.assertIn("测试直播", notices[0].message)
        self.assertIn("live.bilibili.com/99", notices[0].message)
        self.assertEqual(self.service.update_live(online, 30), [])

    def test_dynamic_first_poll_does_not_backfill_and_new_item_notifies(self):
        self.service.subscribe(self.admin, 2, "用户")
        first = [DynamicItem(100, "DYNAMIC_TYPE_WORD", "用户", "旧动态")]
        self.assertEqual(self.service.update_dynamic(2, first, 10), [])
        newer = [
            DynamicItem(102, "DYNAMIC_TYPE_AV", "用户", "新视频"),
            DynamicItem(101, "DYNAMIC_TYPE_LIVE_RCMD", "用户", "直播推荐"),
        ]
        notices = self.service.update_dynamic(2, newer, 20)
        self.assertEqual(len(notices), 1)
        self.assertIn("新投稿", notices[0].message)
        self.assertIn("t.bilibili.com/102", notices[0].message)
        self.assertEqual(notices[0].dynamic_id, 102)
        self.assertEqual(self.service.update_dynamic(2, newer, 30), [])

    def test_empty_or_stale_feed_cannot_reset_cursor_or_success_time(self):
        self.service.subscribe(self.admin, 2, "用户")
        self.service.update_dynamic(2, [DynamicItem(100, "DYNAMIC_TYPE_DRAW", "用户")], 10)
        for items in ([], [DynamicItem(90, "DYNAMIC_TYPE_DRAW", "用户")]):
            with self.assertRaises(ToolError):
                self.service.update_dynamic(2, items, 20)
        self.assertEqual(self.service.dynamic_cursor(2), 100)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT last_dynamic_check FROM bililive_users").fetchone()[0], 10)
        self.assertEqual(self.service.update_dynamic(2, [DynamicItem(100, "DYNAMIC_TYPE_DRAW", "用户")], 30), [])

    def test_live_success_does_not_erase_dynamic_or_push_failure(self):
        self.service.subscribe(self.admin, 2, "用户")
        self.service.set_error(2, "dynamic", ToolError("private detail"), 10)
        self.service.update_live({2: {"live_status": 0}}, 20)
        self.assertEqual(self.service.list(self.admin)[0]["last_error"], "dynamic:ToolError")
        self.service.set_error(2, "push", ToolError("private detail"), 30)
        self.service.update_dynamic(2, [DynamicItem(100, "DYNAMIC_TYPE_DRAW", "用户")], 40)
        self.assertEqual(self.service.list(self.admin)[0]["last_error"], "push:ToolError")
        self.service.clear_error(2, "dynamic")
        self.assertEqual(self.service.list(self.admin)[0]["last_error"], "push:ToolError")
        self.service.clear_error(2, "push")
        self.assertEqual(self.service.list(self.admin)[0]["last_error"], "")

    def test_dynamic_timeline_persists_without_logging_targets_or_replaying(self):
        self.service.subscribe(self.admin, 2, "用户")
        self.service.update_dynamic(2, [DynamicItem(100, "DYNAMIC_TYPE_WORD", "用户")], 10)
        fresh = [DynamicItem(101, "DYNAMIC_TYPE_DRAW", "用户", published_at=15)]
        self.assertEqual(len(self.service.update_dynamic(2, fresh, 20)), 1)
        self.service.record_dynamic_delivery(101, sent=1, failed=1, now=25)
        self.assertEqual(self.service.update_dynamic(2, fresh, 30), [])
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM bililive_dynamic_events").fetchall()
        self.assertEqual(len(rows), 1)
        event = dict(rows[0])
        self.assertEqual((event["published_at"], event["discovered_at"], event["completed_at"]), (15, 20, 25))
        self.assertEqual(event["status"], "partial")
        self.assertNotIn("secret-openid", str(event))


class BiliLiveAPITests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def feed(identifier: int = 456) -> dict:
        return {"code": 0, "data": {"items": [{
            "id_str": str(identifier), "type": "DYNAMIC_TYPE_DRAW",
            "modules": {"module_author": {"name": "测试UP"},
                        "module_dynamic": {"desc": {"text": "图文正文"}}},
        }]}}

    async def test_all_dynamic_requests_explicitly_include_all_types(self):
        def handler(request):
            self.assertEqual(request.url.params["type"], "all")
            if "/desktop/" not in request.url.path:
                return response({}, 412)
            return response(self.feed())
        self.assertEqual((await fetch_dynamic(2, httpx.MockTransport(handler)))[0].dynamic_type, "DYNAMIC_TYPE_DRAW")

    async def test_successful_but_empty_primary_continues_to_desktop(self):
        def handler(request):
            return response({"code": 0, "data": {"items": []}} if "/desktop/" not in request.url.path else self.feed())
        self.assertEqual((await fetch_dynamic(2, httpx.MockTransport(handler), known_dynamic_id=400))[0].dynamic_id, 456)

    async def test_all_empty_sources_with_history_are_failure(self):
        empty = httpx.MockTransport(lambda _: response({"code": 0, "data": {"items": []}}))
        with self.assertRaises(ToolError):
            await fetch_dynamic(2, empty, known_dynamic_id=400)
        self.assertEqual(await fetch_dynamic(2, empty), [])

    async def test_stale_or_malformed_primary_continues_to_fallback(self):
        for bad in (self.feed(100), {"code": 0, "data": {}}, self.feed(100) | {"data": {"items": [{}]}}):
            with self.subTest(bad=bad):
                transport = httpx.MockTransport(lambda r: response(self.feed() if "/desktop/" in r.url.path else bad))
                self.assertEqual((await fetch_dynamic(2, transport, known_dynamic_id=400))[0].dynamic_id, 456)

    async def test_compatibility_query_recovers_empty_desktop_feed(self):
        def handler(request):
            if "/desktop/" not in request.url.path:
                return response({}, 503)
            return response(self.feed() if request.url.params.get("platform") == "web" else {"code": 0, "data": {"items": []}})
        self.assertEqual((await fetch_dynamic(2, httpx.MockTransport(handler), known_dynamic_id=400))[0].dynamic_id, 456)

    async def test_frontend_metadata_is_used_before_basic_requests(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.url.params["platform"], "web")
            self.assertEqual(request.url.params["timezone_offset"], "-480")
            self.assertEqual(request.url.params["web_location"], "333.1387")
            return response({}, 412) if "/desktop/" not in request.url.path else response(self.feed())
        self.assertEqual((await fetch_dynamic(2, httpx.MockTransport(handler)))[0].dynamic_id, 456)
        self.assertEqual(len(requests), 2)

    async def test_basic_desktop_request_remains_a_fallback(self):
        requests = []
        def handler(request):
            requests.append(request)
            return response({"code": 0, "data": {"items": []}}) if request.url.params.get("platform") else response(self.feed())
        self.assertEqual((await fetch_dynamic(2, httpx.MockTransport(handler), known_dynamic_id=400))[0].dynamic_id, 456)
        self.assertEqual(len(requests), 3)
        self.assertNotIn("platform", requests[-1].url.params)

    async def test_browser_fallback_is_attempted_after_empty_http_sources(self):
        empty = {"code": 0, "data": {"items": []}}
        with patch("qbot_bililive.service._fetch_dynamic_api", new_callable=AsyncMock, return_value=empty), patch(
            "qbot_bililive.service._fetch_dynamic_in_browser", new_callable=AsyncMock, return_value=self.feed()
        ) as browser:
            self.assertEqual((await fetch_dynamic(2, known_dynamic_id=400))[0].dynamic_id, 456)
            browser.assert_awaited_once_with(2, known_dynamic_id=400)

    async def test_feed_deadline_is_safe_error(self):
        with patch("qbot_bililive.service._fetch_dynamic_feed", new_callable=AsyncMock, side_effect=TimeoutError):
            with self.assertRaises(ToolError):
                await fetch_dynamic(2)

    async def test_fetch_cancellation_propagates(self):
        with patch("qbot_bililive.service._fetch_dynamic_feed", new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await fetch_dynamic(2)

    async def test_browser_tries_desktop_after_web_risk_and_closes_page(self):
        from qbot_bililive.service import _fetch_dynamic_in_browser
        page = AsyncMock()
        page.evaluate.side_effect = [{"code": -412}, self.feed()]
        context = AsyncMock()
        context.new_page.return_value = page
        with patch("qbot_bililive.service._browser", new_callable=AsyncMock, return_value=context):
            payload = await _fetch_dynamic_in_browser(2, known_dynamic_id=400)
        self.assertEqual(payload, self.feed())
        self.assertEqual(page.evaluate.await_count, 2)
        self.assertIn("type: 'all'", page.evaluate.await_args.args[0])
        self.assertIn("AbortController", page.evaluate.await_args.args[0])
        page.close.assert_awaited_once()

    async def test_browser_ignores_stale_or_unparseable_first_source(self):
        from qbot_bililive.service import _fetch_dynamic_in_browser
        for bad in (self.feed(100), {"code": 0, "data": {}}):
            page = AsyncMock()
            page.evaluate.side_effect = [bad, self.feed()]
            context = AsyncMock()
            context.new_page.return_value = page
            with patch("qbot_bililive.service._browser", new_callable=AsyncMock, return_value=context):
                self.assertEqual(await _fetch_dynamic_in_browser(2, known_dynamic_id=400), self.feed())
            self.assertEqual(page.evaluate.await_count, 2)

    async def test_dynamic_card_renders_api_content_without_webpage(self):
        image = await fetch_dynamic_card(DynamicItem(
            123, "DYNAMIC_TYPE_DRAW", "测试UP",
            "第一段正文\n\n● 第二段正文", published_at=1_789_125_210,
        ))
        self.assertTrue(image.startswith(b"\xff\xd8\xff"))
        with Image.open(io.BytesIO(image)) as card:
            self.assertEqual(card.width, 920)
            self.assertGreater(card.height, 300)

    async def test_large_dynamic_gif_uses_first_frame_in_card(self):
        stream = io.BytesIO()
        frames = [Image.new("RGB", (32, 24), color) for color in ("#ff0000", "#0000ff")]
        frames[0].save(
            stream, "GIF", save_all=True, append_images=frames[1:],
            duration=100, loop=0,
        )
        # GIF decoders permit trailing data.  This reproduces a valid animated
        # Bilibili asset whose complete download exceeds the old 4 MiB limit.
        large_gif = stream.getvalue() + b"\0" * (4 * 1024 * 1024)
        transport = httpx.MockTransport(lambda _: httpx.Response(
            200, content=large_gif, headers={"content-type": "image/gif"},
        ))

        image = await _fetch_bili_image(
            "https://i0.hdslb.com/bfs/new_dyn/example.gif", transport,
        )

        self.assertTrue(image.startswith(b"\xff\xd8\xff"))
        self.assertLess(len(image), len(large_gif))
        with Image.open(io.BytesIO(image)) as first_frame:
            self.assertEqual(first_frame.size, (32, 24))
            red, green, blue = first_frame.getpixel((16, 12))
            self.assertGreater(red, 240)
            self.assertLess(green, 20)
            self.assertLess(blue, 20)

    async def test_dynamic_risk_control_uses_desktop_api_shape(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/web-dynamic/v1/" in request.url.path:
                return response({}, status=412)
            self.assertIn("/web-dynamic/desktop/v1/", request.url.path)
            return response({"code": 0, "data": {"items": [{
                "id_str": "456", "type": "DYNAMIC_TYPE_AV",
                "modules": [
                    {"module_author": {"pub_ts": 123, "user": {
                        "name": "桌面UP", "face": "https://i0.hdslb.com/avatar.jpg",
                        "official": {"role": 3, "title": "官方账号"},
                    }}},
                    {"module_desc": {"text": "完整正文第一段\n\n完整正文第二段"}},
                    {"module_dynamic": {
                        "dyn_archive": {
                            "title": "桌面投稿",
                            "cover": "https://i0.hdslb.com/cover.webp",
                        },
                    }},
                ],
            }]}})

        items = await fetch_dynamic(2, httpx.MockTransport(handler))
        self.assertEqual(items[0].author, "桌面UP")
        self.assertEqual(items[0].summary, "完整正文第一段\n\n完整正文第二段")
        self.assertEqual(items[0].published_at, 123)
        self.assertEqual(items[0].avatar_url, "https://i0.hdslb.com/avatar.jpg")
        self.assertEqual(items[0].image_urls, ("https://i0.hdslb.com/cover.webp",))
        self.assertTrue(items[0].verified)

    async def test_user_card_risk_control_falls_back_to_live_api(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/card"):
                return response({"code": -352, "message": "risk"})
            self.assertTrue(request.url.path.endswith("get_status_info_by_uids"))
            return response({"code": 0, "data": {"2": {
                "uname": "测试UP", "live_status": 0,
            }}})

        transport = httpx.MockTransport(handler)
        self.assertEqual(await fetch_user(2, transport), "测试UP")

    async def test_account_without_live_room_is_verified_from_space_feed(self):
        uid = 3537118097836039
        def handler(request):
            if request.url.path.endswith("/card"):
                self.assertEqual(request.url.params["mid"], str(uid))
                return response({"code": -352})
            if request.url.path.endswith("get_status_info_by_uids"):
                self.assertEqual(parse_qs(request.content.decode()), {"uids[]": [str(uid)]})
                return response({"code": 0, "data": []})
            self.assertEqual(request.url.params["host_mid"], str(uid))
            return response({"code": 0, "data": {"items": [{
                "id_str": "1247136871449886724", "type": "DYNAMIC_TYPE_FORWARD",
                "modules": [{"module_author": {"user": {"name": "Piki피키"}}}],
                "orig": {"modules": {"module_author": {"name": "另一位UP"}}},
            }]}})
        self.assertEqual(await fetch_user(uid, httpx.MockTransport(handler)), "Piki피키")
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            admin = Identity("bot", "group:openid", "owner", group_role="owner")
            reply = await dispatch(store, admin, f"关注 {uid}", httpx.MockTransport(handler))
            self.assertIn("Piki피키", reply)
            self.assertEqual(BiliLiveStore(store).uids("dynamic"), [uid])

    async def test_user_http_failure_and_live_failure_do_not_block_feed_fallback(self):
        def handler(request):
            if request.url.path.endswith("/card"):
                return response({}, 503)
            if request.url.path.endswith("get_status_info_by_uids"):
                return response({}, 502)
            return response(self.feed())
        self.assertEqual(await fetch_user(2, httpx.MockTransport(handler)), "测试UP")

    async def test_empty_card_also_tries_live_profile(self):
        def handler(request):
            if request.url.path.endswith("/card"):
                return response({"code": 0, "data": {"card": {}}})
            return response({"code": 0, "data": {"2": {"uname": "测试UP"}}})
        self.assertEqual(await fetch_user(2, httpx.MockTransport(handler)), "测试UP")

    async def test_unavailable_user_sources_do_not_claim_account_is_missing(self):
        for risk in (False, True):
            def handler(request):
                if request.url.path.endswith("/card"):
                    return response({"code": -352} if risk else {"code": 0, "data": {"card": {}}})
                if request.url.path.endswith("get_status_info_by_uids"):
                    return response({"code": 0, "data": []})
                return response({"code": 0, "data": {"items": []}})
            with self.subTest(risk=risk), self.assertRaises(ToolError) as caught:
                await fetch_user(2, httpx.MockTransport(handler))
            self.assertNotIn("没有找到", str(caught.exception))
            self.assertNotIn("不存在", str(caught.exception))
            self.assertIn("稍后重试", str(caught.exception))

    async def test_user_lookup_cancellation_propagates(self):
        with patch("qbot_bililive.service._fetch_user", new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await fetch_user(2)

    async def test_user_lookup_timeout_is_safe_error(self):
        with patch("qbot_bililive.service._fetch_user", new_callable=AsyncMock, side_effect=TimeoutError):
            with self.assertRaises(ToolError) as caught:
                await fetch_user(2)
            self.assertIn("查询超时", str(caught.exception))

    async def test_fetch_user_live_and_dynamic(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/card"):
                return response({"code": 0, "data": {"card": {"name": "测试UP"}}})
            if request.url.path.endswith("get_status_info_by_uids"):
                self.assertEqual(parse_qs(request.content.decode()), {"uids[]": ["2"]})
                return response({"code": 0, "data": {"2": {
                    "uname": "测试UP", "live_status": 1, "room_id": 3,
                }}})
            return response({"code": 0, "data": {"items": [{
                "id_str": "123", "type": "DYNAMIC_TYPE_WORD",
                "modules": {
                    "module_author": {"name": "测试UP"},
                    "module_dynamic": {"desc": {"text": "动态正文"}},
                },
            }]}})

        transport = httpx.MockTransport(handler)
        self.assertEqual(await fetch_user(2, transport), "测试UP")
        self.assertEqual((await fetch_live([2], transport))[2]["room_id"], 3)
        items = await fetch_dynamic(2, transport)
        self.assertEqual(items[0].summary, "动态正文")

    async def test_dispatch_and_safe_api_errors(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(temp.name)
        admin = Identity("bot", "group:openid", "owner", group_role="owner")

        transport = httpx.MockTransport(lambda _: response({
            "code": 0, "data": {"card": {"name": "测试UP"}},
        }))
        result = await dispatch(store, admin, "关注 2", transport)
        self.assertIn("测试UP", result)
        self.assertIn("直播开", await dispatch(store, admin, "列表"))
        self.assertIn("已关闭", await dispatch(store, admin, "关闭动态 2"))

        denied = httpx.MockTransport(lambda _: response({"code": -412, "message": "secret"}))
        with self.assertRaises(ToolError) as caught:
            await fetch_user(2, denied)
        self.assertNotIn("secret", str(caught.exception))

        denied_http = httpx.MockTransport(lambda _: response({}, status=412))
        with self.assertRaises(ToolError):
            await fetch_dynamic(2, denied_http)

    async def test_private_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            private = Identity("bot", "private:user", "user", True)
            with self.assertRaises(ToolError):
                await dispatch(Store(folder), private, "列表")


if __name__ == "__main__":
    unittest.main()
