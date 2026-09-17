"""Native QQ entry point and background worker for Bilibili subscriptions."""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from contextlib import suppress
from typing import Any

from nonebot import get_driver, on_command
from nonebot.adapters.qq import Bot, Message, MessageSegment
from nonebot.adapters.qq.event import MessageEvent
from nonebot.log import logger
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from bot_tools import community
from bot_tools.bililive import (
    BiliLiveStore, Notice, close_browser, dispatch, fetch_dynamic,
    fetch_dynamic_card, fetch_live,
)
from bot_tools.storage import ToolError
from message_ui import error_panel, public_error_message
from bot_tools.plugin_runtime import bounded, command_eligible, get_store, identity


bili = on_command(
    "bili", aliases={"bililive", "B站"}, force_whitespace=True,
    rule=command_eligible, priority=10, block=True,
)
_adapter: Any = None
_worker_task: asyncio.Task | None = None


def configure_adapter(adapter: Any) -> None:
    """Provide the already configured native QQ adapter for proactive pushes."""
    global _adapter
    _adapter = adapter


def _interval(name: str, default: int, minimum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, 24 * 60 * 60))


def _plugin_enabled(store, scope: str) -> bool:
    return "bili" not in store.document(scope).get("disabled", [])


def _bot_for(app_id: str) -> Bot | None:
    if _adapter is None:
        return None
    connected = getattr(_adapter, "bots", {}).get(app_id)
    if connected is not None:
        return connected
    # Webhook accounts do not keep a gateway Bot in adapter.bots.  A Bot made
    # from the live encrypted configuration can still obtain its access token
    # and call the proactive group-message endpoint.
    bot_info = next(
        (item for item in getattr(getattr(_adapter, "qq_config", None), "qq_bots", ())
         if item.id == app_id),
        None,
    )
    return Bot(_adapter, app_id, bot_info) if bot_info is not None else None


async def _push_notice(service: BiliLiveStore, notice: Notice) -> None:
    sent = failed = skipped = 0
    try:
        targets = await asyncio.to_thread(service.targets, notice.uid, notice.kind)
    except Exception as exc:
        await asyncio.to_thread(service.set_error, notice.uid, "target", exc)
        if notice.dynamic_id is not None:
            await asyncio.to_thread(service.record_dynamic_delivery, notice.dynamic_id, failed=1)
        logger.warning("Bilibili push target lookup failed ({})", type(exc).__name__)
        return
    await asyncio.to_thread(service.clear_error, notice.uid, "target")
    eligible = []
    for target in targets:
        if target.kind != "group" or not await asyncio.to_thread(
            _plugin_enabled, service.store, target.scope,
        ):
            skipped += 1
            continue
        eligible.append(target)
    card: bytes | None = None
    if eligible and notice.kind == "dynamic" and notice.dynamic is not None:
        try:
            async with asyncio.timeout(25):
                card = await fetch_dynamic_card(notice.dynamic)
        except Exception as exc:
            # The text notification remains a reliable fallback when Bilibili
            # changes its page or temporarily presents a risk-control screen.
            logger.warning(
                "Bilibili dynamic card fallback for {} ({})",
                notice.dynamic_id, type(exc).__name__,
            )
    for target in eligible:
        bot = _bot_for(target.bot)
        if bot is None:
            skipped += 1
            logger.warning("Bilibili push skipped: QQ bot {} is not loaded", target.bot)
            continue
        try:
            content: str | Message = bounded(notice.message)
            if card is not None and notice.dynamic_id is not None:
                lines = notice.message.splitlines()
                headline = lines[0] if lines else "📰 B站发布了新动态"
                url = f"https://t.bilibili.com/{notice.dynamic_id}"
                content = Message([
                    MessageSegment.text(f"{headline}\n"),
                    MessageSegment.file_image(
                        card, f"bilibili-dynamic-{notice.dynamic_id}.jpg",
                    ),
                    MessageSegment.text(f"\n{url}"),
                ])
            # An expired send may be ambiguous; record it, never retry it with
            # another sequence number (which could duplicate a notification).
            async with asyncio.timeout(45):
                await bot.send_to_group(
                    target.target, content,
                    msg_seq=secrets.randbelow(65_535) + 1,
                )
            sent += 1
            if notice.dynamic_id is not None:
                published = notice.dynamic.published_at if notice.dynamic else 0
                logger.info("Bilibili dynamic {} sent bot={} published_at={} sent_at={} lag_seconds={}",
                            notice.dynamic_id, target.bot, published, int(time.time()),
                            int(time.time() - published) if published else -1)
        except Exception as exc:
            failed += 1
            # Never log encrypted/plain OpenIDs, response bodies or credentials.
            logger.warning(
                "Bilibili proactive group push failed for bot {} ({})",
                target.bot, type(exc).__name__,
            )
            await asyncio.to_thread(service.set_error, notice.uid, "push", exc)
    if notice.dynamic_id is not None:
        await asyncio.to_thread(service.record_dynamic_delivery, notice.dynamic_id,
                                sent=sent, failed=failed, skipped=skipped)
    if sent and not failed:
        await asyncio.to_thread(service.clear_error, notice.uid, "push")


async def poll_live_once(service: BiliLiveStore) -> int:
    uids = await asyncio.to_thread(service.uids, "live")
    if not uids:
        return 0
    try:
        snapshots = await fetch_live(uids)
        notices = await asyncio.to_thread(service.update_live, snapshots)
    except Exception as exc:
        for uid in uids:
            await asyncio.to_thread(service.set_error, uid, "live", exc)
        logger.warning("Bilibili live polling failed ({})", type(exc).__name__)
        return 0
    for notice in notices:
        await _push_notice(service, notice)
    return len(notices)


async def poll_dynamic_once(service: BiliLiveStore) -> int:
    uids = await asyncio.to_thread(service.uids, "dynamic")
    if not uids:
        return 0
    semaphore = asyncio.Semaphore(3)
    total = 0

    async def one(uid: int) -> None:
        nonlocal total
        async with semaphore:
            try:
                cursor = await asyncio.to_thread(service.dynamic_cursor, uid)
                items = await fetch_dynamic(uid, known_dynamic_id=cursor)
                notices = await asyncio.to_thread(service.update_dynamic, uid, items)
            except Exception as exc:
                await asyncio.to_thread(service.set_error, uid, "dynamic", exc)
                logger.warning(
                    "Bilibili dynamic polling failed for UID {} ({})", uid, type(exc).__name__,
                )
                return
        # Do not hold already discovered notices behind a slow account's API
        # fallback or card rendering. Limit API concurrency, not notifications.
        for notice in notices:
            published = notice.dynamic.published_at if notice.dynamic else 0
            logger.info("Bilibili dynamic {} discovered uid={} published_at={} discovered_at={} lag_seconds={}",
                        notice.dynamic_id, uid, published, int(time.time()),
                        int(time.time() - published) if published else -1)
            await _push_notice(service, notice)
        total += len(notices)

    await asyncio.gather(*(one(uid) for uid in uids))
    return total


async def _periodic_poll(service: BiliLiveStore, poll, interval: int) -> None:
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        try:
            await poll(service)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Bilibili {} cycle failed ({})", poll.__name__, type(exc).__name__)
        # Fixed start-to-start cadence, not interval plus API/card/send time.
        await asyncio.sleep(max(1.0, interval - (loop.time() - started)))


async def worker() -> None:
    service = BiliLiveStore(get_store())
    live_interval = _interval("BILILIVE_LIVE_INTERVAL", 60, 30)
    dynamic_interval = _interval("BILILIVE_DYNAMIC_INTERVAL", 120, 60)
    logger.info("Bilibili polling intervals: live={}s dynamic={}s", live_interval, dynamic_interval)
    tasks = [asyncio.create_task(_periodic_poll(service, poll_live_once, live_interval)),
             asyncio.create_task(_periodic_poll(service, poll_dynamic_once, dynamic_interval))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@bili.handle()
async def handle_bili(bot: Bot, event: MessageEvent, matcher: Matcher,
                      args: Message = CommandArg()) -> None:
    raw = args.extract_plain_text().strip()
    if len(raw) > 200:
        await matcher.finish(error_panel("输入过长，请控制在 200 字以内。"))
    try:
        store, who = get_store(), identity(bot, event)
        await asyncio.to_thread(store.remember_scope, who)
        await asyncio.to_thread(community.gate, store, who, "bili")
        reply = await dispatch(store, who, raw)
    except ToolError as exc:
        detail = str(exc).strip()
        visible = public_error_message(exc, "B站推送服务暂时不可用，请稍后重试或联系管理员。")
        if visible != detail:
            logger.error("Suppressed internal Bilibili error: {}", detail)
        reply = error_panel(visible)
    except Exception as exc:
        logger.error("Bilibili command failed ({})", type(exc).__name__)
        reply = error_panel("B站推送命令暂时未完成，请稍后重试。")
    await matcher.send(bounded(reply))


@get_driver().on_startup
async def start_bililive_worker() -> None:
    global _worker_task
    await asyncio.to_thread(BiliLiveStore, get_store())
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(worker(), name="bililive-poller")
    logger.info("Bilibili live/dynamic subscription worker ready")


@get_driver().on_shutdown
async def stop_bililive_worker() -> None:
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await _worker_task
        _worker_task = None
    await close_browser()
