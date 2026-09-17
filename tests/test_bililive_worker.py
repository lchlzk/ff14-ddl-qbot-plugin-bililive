import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi+~httpx+~websockets")

from qbot_bililive.service import DynamicItem, Notice, PushTarget
from plugins import bililive


class BiliLiveWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_account_push_does_not_wait_for_slow_account(self):
        service = Mock()
        service.uids.return_value = [1, 2]
        service.dynamic_cursor.return_value = 100
        notice = Notice(1, "dynamic", "新动态", 101, DynamicItem(101, "DYNAMIC_TYPE_DRAW", "UP"))
        service.update_dynamic.side_effect = lambda uid, _: [notice] if uid == 1 else []
        release = asyncio.Event()
        pushed = asyncio.Event()

        async def fetch(uid, *, known_dynamic_id):
            self.assertEqual(known_dynamic_id, 100)
            if uid == 2:
                await release.wait()
            return []

        async def push(*_):
            pushed.set()

        with patch.object(bililive, "fetch_dynamic", new=fetch), patch.object(bililive, "_push_notice", new=push):
            task = asyncio.create_task(bililive.poll_dynamic_once(service))
            try:
                await asyncio.wait_for(pushed.wait(), 1)
                self.assertFalse(task.done())
            finally:
                release.set()
                self.assertEqual(await task, 1)

    async def test_live_and_dynamic_polling_are_independent_and_cancel_cleanly(self):
        dynamic = asyncio.Event()
        live_cancelled = asyncio.Event()

        async def slow_live(_):
            try:
                await asyncio.Event().wait()
            finally:
                live_cancelled.set()

        async def quick_dynamic(_):
            dynamic.set()

        with patch.object(bililive, "BiliLiveStore", return_value=Mock()), patch.object(bililive, "get_store"), patch.object(
            bililive, "poll_live_once", new=slow_live
        ), patch.object(bililive, "poll_dynamic_once", new=quick_dynamic):
            task = asyncio.create_task(bililive.worker())
            try:
                await asyncio.wait_for(dynamic.wait(), 1)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(live_cancelled.is_set())

    async def test_poll_duration_is_subtracted_from_interval(self):
        loop = Mock()
        loop.time.side_effect = [100, 130]
        with patch.object(bililive.asyncio, "get_running_loop", return_value=loop), patch.object(
            bililive.asyncio, "sleep", new_callable=AsyncMock, side_effect=asyncio.CancelledError
        ) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await bililive._periodic_poll(Mock(), AsyncMock(), 120)
        sleep.assert_awaited_once_with(90)

    async def test_failed_send_is_recorded_without_retry(self):
        service = Mock()
        service.targets.return_value = [PushTarget("bot-a", "scope", "group", "secret-openid")]
        bot = SimpleNamespace(send_to_group=AsyncMock(side_effect=TimeoutError))
        notice = Notice(2, "dynamic", "新动态", 101)
        with patch.object(bililive, "_plugin_enabled", return_value=True), patch.object(bililive, "_bot_for", return_value=bot):
            await bililive._push_notice(service, notice)
        bot.send_to_group.assert_awaited_once()
        service.record_dynamic_delivery.assert_called_once_with(101, sent=0, failed=1, skipped=0)

    async def test_card_timeout_still_sends_text_and_records_success(self):
        service = Mock()
        service.targets.return_value = [PushTarget("bot-a", "scope", "group", "secret-openid")]
        bot = SimpleNamespace(send_to_group=AsyncMock())
        notice = Notice(2, "dynamic", "新动态\nhttps://t.bilibili.com/101", 101,
                        DynamicItem(101, "DYNAMIC_TYPE_DRAW", "UP", published_at=100))
        with patch.object(bililive, "_plugin_enabled", return_value=True), patch.object(bililive, "_bot_for", return_value=bot), patch.object(
            bililive, "fetch_dynamic_card", new_callable=AsyncMock, side_effect=TimeoutError
        ):
            await bililive._push_notice(service, notice)
        bot.send_to_group.assert_awaited_once()
        self.assertIn("t.bilibili.com/101", bot.send_to_group.call_args.args[1])
        service.record_dynamic_delivery.assert_called_once_with(101, sent=1, failed=0, skipped=0)
