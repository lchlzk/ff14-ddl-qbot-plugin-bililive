"""Bilibili live/dynamic subscriptions for the native QQ adapter.

The polling model and first-run offset behaviour are adapted from
``Akiyy-dev/nonebot-plugin-bililive`` (AGPL-3.0-or-later).  Message delivery,
storage and permissions are implemented locally for nonebot-adapter-qq.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import io
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from nonebot.log import logger
from PIL import Image, UnidentifiedImageError

from message_ui import help_panel, panel
from .media import render_dynamic_card
from bot_tools.http_clients import client
from bot_tools.media import checked_image
from bot_tools.storage import Identity, Store, ToolError


LIVE_API = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
CARD_API = "https://api.bilibili.com/x/web-interface/card"
DYNAMIC_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
DYNAMIC_DESKTOP_API = (
    "https://api.bilibili.com/x/polymer/web-dynamic/desktop/v1/feed/space"
)
MAX_SUBSCRIPTIONS = 50
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DYNAMIC_IMAGE_BYTES = 4 * 1024 * 1024
MAX_DYNAMIC_GIF_BYTES = 16 * 1024 * 1024
MAX_DYNAMIC_IMAGE_PIXELS = 12_000_000
UID_PATTERN = re.compile(r"(?:https?://space\.bilibili\.com/)?([1-9][0-9]{0,19})/?")
SKIP_DYNAMIC_TYPES = {
    "DYNAMIC_TYPE_LIVE_RCMD", "DYNAMIC_TYPE_LIVE", "DYNAMIC_TYPE_AD",
    "DYNAMIC_TYPE_BANNER",
}
DYNAMIC_LABELS = {
    "DYNAMIC_TYPE_FORWARD": "转发了一条动态",
    "DYNAMIC_TYPE_WORD": "发布了新文字动态",
    "DYNAMIC_TYPE_DRAW": "发布了新图文动态",
    "DYNAMIC_TYPE_AV": "发布了新投稿",
    "DYNAMIC_TYPE_ARTICLE": "发布了新专栏",
    "DYNAMIC_TYPE_MUSIC": "发布了新音频",
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}
_browser_context: Any = None
_browser_playwright: Any = None
_browser_lock: asyncio.Lock | None = None
_browser_lock_loop: asyncio.AbstractEventLoop | None = None


class BiliRiskControl(ToolError):
    pass


@dataclass(frozen=True)
class DynamicItem:
    dynamic_id: int
    dynamic_type: str
    author: str
    summary: str = ""
    published_at: int = 0
    avatar_url: str = ""
    image_urls: tuple[str, ...] = ()
    verified: bool = False


@dataclass(frozen=True)
class Notice:
    uid: int
    kind: str
    message: str
    dynamic_id: int | None = None
    dynamic: DynamicItem | None = None


@dataclass(frozen=True)
class PushTarget:
    bot: str
    scope: str
    kind: str
    target: str


def parse_uid(value: object) -> int:
    raw = str(value or "").strip()
    match = UID_PATTERN.fullmatch(raw)
    if not match:
        raise ToolError("请输入 B站 UID，例如：/bili 关注 2。")
    uid = int(match.group(1))
    if uid > 9_223_372_036_854_775_807:
        raise ToolError("B站 UID 超出支持范围。")
    return uid


def _text(value: object, limit: int) -> str:
    result = html.unescape(str(value or ""))
    result = " ".join(result.replace("\x00", "").split())
    return result[:limit]


def _body_text(value: object, limit: int = 1_800) -> str:
    result = html.unescape(str(value or "")).replace("\x00", "")
    result = result.replace("\r\n", "\n").replace("\r", "\n")
    result = "\n".join(line.rstrip() for line in result.split("\n")).strip()
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result[:limit]


def _valid_bili_image_url(value: object) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and any(
        host == suffix[1:] or host.endswith(suffix)
        for suffix in (".hdslb.com", ".bilibili.com", ".bilivideo.com")
    )


def _dynamic_image_urls(dynamic: object) -> tuple[str, ...]:
    result: list[str] = []

    def walk(node: object) -> None:
        if len(result) >= 9:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"src", "cover"} and _valid_bili_image_url(value):
                    url = str(value)
                    if url not in result:
                        result.append(url)
                elif isinstance(value, (dict, list, tuple)):
                    walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(dynamic)
    return tuple(result)


def _response_json(response: httpx.Response) -> dict[str, Any]:
    if response.status_code == 412:
        raise BiliRiskControl("B站接口触发风控，请稍后重试。")
    if response.status_code != 200:
        raise ToolError(f"B站接口暂时不可用（HTTP {response.status_code}）。")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ToolError("B站接口响应过大，已停止处理。")
    try:
        payload = response.json()
    except ValueError:
        raise ToolError("B站接口返回了无法识别的数据。") from None
    if not isinstance(payload, dict):
        raise ToolError("B站接口返回格式不正确。")
    if payload.get("code") != 0:
        code = payload.get("code", "未知")
        if code in {-352, -412}:
            raise BiliRiskControl("B站接口触发风控，请稍后重试。")
        raise ToolError(f"B站接口请求失败（代码 {code}）。")
    return payload


async def fetch_user(uid: int, transport: httpx.AsyncBaseTransport | None = None) -> str:
    try:
        async with asyncio.timeout(90):
            return await _fetch_user(uid, transport)
    except TimeoutError:
        raise ToolError("B站用户资料查询超时，请稍后重试。") from None


async def _fetch_user(uid: int, transport: httpx.AsyncBaseTransport | None) -> str:
    errors: list[ToolError] = []
    options = dict(
        headers=HEADERS, timeout=httpx.Timeout(15, connect=8),
        follow_redirects=False, trust_env=False, transport=transport,
    )
    try:
        try:
            async with client("bililive-card", **options) as session:
                response = await session.get(CARD_API, params={"mid": str(uid)})
        except httpx.TransportError:
            raise ToolError("暂时无法连接 B站，请稍后重试。") from None
        payload = _response_json(response)
        data = payload.get("data")
        card = data.get("card") if isinstance(data, dict) else None
        name = _text(card.get("name") if isinstance(card, dict) else "", 80)
        if name:
            return name
    except ToolError as exc:
        errors.append(exc)
    try:
        live = await fetch_live([uid], transport)
        info = live.get(uid)
        name = _text(info.get("uname") if isinstance(info, dict) else "", 80)
        if name:
            return name
    except ToolError as exc:
        errors.append(exc)
    # The live endpoint omits accounts without a live room; that is not
    # evidence that the account does not exist. The space feed's outer
    # author (not a forwarded post's original author) can verify it instead.
    try:
        items = await fetch_dynamic(uid, transport)
        name = next((_text(item.author, 80) for item in items if _text(item.author, 80)), "")
        if name:
            return name
    except ToolError as exc:
        errors.append(exc)
    if any(isinstance(error, BiliRiskControl) for error in errors):
        raise BiliRiskControl("B站用户资料查询暂时受限，请稍后重试。")
    # Empty, malformed or unavailable sources cannot prove a nonexistent UID.
    raise ToolError("暂时无法确认这个 B站用户，请检查 UID 或稍后重试。")


async def fetch_live(
    uids: Iterable[int], transport: httpx.AsyncBaseTransport | None = None,
) -> dict[int, dict[str, Any]]:
    unique = sorted(set(int(uid) for uid in uids))
    if not unique:
        return {}
    result: dict[int, dict[str, Any]] = {}
    options = dict(
        headers=HEADERS, timeout=httpx.Timeout(20, connect=8),
        follow_redirects=False, trust_env=False, transport=transport,
    )
    try:
        async with client("bililive-live", **options) as session:
            for start in range(0, len(unique), 100):
                response = await session.post(
                    LIVE_API,
                    data={"uids[]": [str(uid) for uid in unique[start:start + 100]]},
                )
                payload = _response_json(response)
                data = payload.get("data")
                if not isinstance(data, dict):
                    continue
                for key, info in data.items():
                    if not isinstance(info, dict):
                        continue
                    try:
                        uid = int(key)
                    except (TypeError, ValueError):
                        continue
                    if uid in unique:
                        result[uid] = info
    except httpx.TransportError:
        raise ToolError("暂时无法连接 B站直播接口。") from None
    return result


def parse_dynamic_items(payload: dict[str, Any]) -> list[DynamicItem]:
    data = payload.get("data")
    raw_items = data.get("items") if isinstance(data, dict) else None
    result: list[DynamicItem] = []
    for raw in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(raw, dict):
            continue
        try:
            dynamic_id = int(raw.get("id_str"))
        except (TypeError, ValueError):
            continue
        raw_modules = raw.get("modules")
        if isinstance(raw_modules, dict):
            modules = raw_modules
        elif isinstance(raw_modules, list):
            modules = {}
            for module in raw_modules:
                if isinstance(module, dict):
                    modules.update(module)
        else:
            modules = {}
        author = modules.get("module_author")
        author_user = author.get("user") if isinstance(author, dict) else None
        author_name = _text(
            author.get("name") if isinstance(author, dict) else "", 80,
        ) or _text(
            author_user.get("name") if isinstance(author_user, dict) else "", 80,
        )
        avatar_url = _text(
            author_user.get("face") if isinstance(author_user, dict) else "", 500,
        ) or _text(
            author.get("face") if isinstance(author, dict) else "", 500,
        )
        official = (
            author_user.get("official") if isinstance(author_user, dict) else None
        )
        try:
            official_role = int(official.get("role") or 0) if isinstance(official, dict) else 0
        except (TypeError, ValueError):
            official_role = 0
        verified = bool(
            isinstance(official, dict)
            and (
                _text(official.get("title"), 100)
                or _text(official.get("desc"), 100)
                or official_role > 0
            )
        )
        try:
            published_at = int(author.get("pub_ts") or 0) if isinstance(author, dict) else 0
        except (TypeError, ValueError):
            published_at = 0
        dynamic_type = _text(raw.get("type"), 64)
        dynamic = modules.get("module_dynamic")
        module_desc = modules.get("module_desc")
        summary = _body_text(
            module_desc.get("text") if isinstance(module_desc, dict) else ""
        )
        if isinstance(dynamic, dict):
            desc = dynamic.get("desc") or dynamic.get("dyn_desc")
            if not summary and isinstance(desc, dict):
                summary = _body_text(desc.get("text"))
            major = dynamic.get("major")
            if not isinstance(major, dict):
                major = dynamic
            if not summary and isinstance(major, dict):
                for part in (
                    "archive", "article", "opus", "music", "draw",
                    "dyn_archive", "dyn_article", "dyn_opus", "dyn_music",
                    "dyn_draw",
                ):
                    block = major.get(part)
                    if not isinstance(block, dict):
                        continue
                    summary_block = block.get("summary")
                    summary = _body_text(
                        block.get("title") or block.get("desc") or (
                            summary_block.get("text")
                            if isinstance(summary_block, dict) else ""
                        )
                    )
                    if summary:
                        break
        if dynamic_id > 0 and author_name:
            result.append(DynamicItem(
                dynamic_id, dynamic_type, author_name, summary,
                published_at=published_at,
                avatar_url=avatar_url if _valid_bili_image_url(avatar_url) else "",
                image_urls=_dynamic_image_urls(dynamic),
                verified=verified,
            ))
    return result


async def _fetch_dynamic_api(
    api: str, uid: int, transport: httpx.AsyncBaseTransport | None,
    *, compatibility: bool = False,
) -> dict[str, Any]:
    headers = {**HEADERS, "Referer": f"https://space.bilibili.com/{uid}/dynamic",
               "Origin": "https://space.bilibili.com"}
    options = dict(
        headers=headers, timeout=httpx.Timeout(15, connect=8),
        follow_redirects=False, trust_env=False, transport=transport,
    )
    # Request every dynamic type. Frontend metadata also avoids the empty
    # successful responses seen for both video and picture/text accounts.
    params = {"host_mid": str(uid), "type": "all"}
    if compatibility:
        params.update(platform="web", timezone_offset="-480", web_location="333.1387")
    try:
        async with client("bililive-dynamic", **options) as session:
            response = await session.get(api, params=params)
    except httpx.TransportError:
        raise ToolError("暂时无法连接 B站动态接口。") from None
    return _response_json(response)


async def fetch_dynamic(
    uid: int, transport: httpx.AsyncBaseTransport | None = None,
    *, known_dynamic_id: int | None = None,
) -> list[DynamicItem]:
    # Bound browser fetch promises and slow fallback paths so one account does
    # not stop future polling indefinitely. Cancellation still propagates.
    try:
        async with asyncio.timeout(50):
            return await _fetch_dynamic_feed(uid, transport, known_dynamic_id)
    except TimeoutError:
        raise ToolError("B站动态检查超时，请稍后重试。") from None


def _validated_dynamic_items(payload: dict[str, Any]) -> list[DynamicItem]:
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ToolError("B站动态列表格式异常。")
    items = parse_dynamic_items(payload)
    if data["items"] and not items:
        raise ToolError("B站动态列表暂时无法解析。")
    return items


async def _fetch_dynamic_feed(
    uid: int, transport: httpx.AsyncBaseTransport | None,
    known_dynamic_id: int | None,
) -> list[DynamicItem]:
    errors: list[ToolError] = []
    empty = False
    sources = (
        (DYNAMIC_API, True, "web-compatible"),
        (DYNAMIC_DESKTOP_API, True, "desktop-compatible"),
        (DYNAMIC_DESKTOP_API, False, "desktop-basic"),
    )
    for api, compatibility, source in sources:
        try:
            payload = await _fetch_dynamic_api(api, uid, transport, compatibility=compatibility)
            items = _validated_dynamic_items(payload)
            if items and (known_dynamic_id is None or max(x.dynamic_id for x in items) >= known_dynamic_id):
                logger.debug("Bilibili dynamic UID {} source={} items={}", uid, source, len(items))
                return items
            empty = empty or not items
            logger.warning("Bilibili dynamic UID {} source={} returned an empty or older feed; trying fallback", uid, source)
        except ToolError as exc:
            errors.append(exc)
    if transport is None:
        try:
            payload = await _fetch_dynamic_in_browser(uid, known_dynamic_id=known_dynamic_id)
            items = _validated_dynamic_items(payload)
            if items and (known_dynamic_id is None or max(x.dynamic_id for x in items) >= known_dynamic_id):
                logger.debug("Bilibili dynamic UID {} source=browser items={}", uid, len(items))
                return items
            empty = empty or not items
        except ToolError as exc:
            errors.append(exc)
    # An empty result must not hide a failed check for a previously nonempty
    # account. A genuinely new, consistently empty account can remain unseeded.
    if known_dynamic_id is None and empty and not errors:
        return []
    if empty or not errors:
        raise BiliRiskControl("B站动态列表暂时为空或落后，已保留上次检查位置。")
    raise errors[-1]


def _get_browser_lock() -> asyncio.Lock:
    global _browser_lock, _browser_lock_loop
    loop = asyncio.get_running_loop()
    if _browser_lock is None or _browser_lock_loop is not loop:
        _browser_lock = asyncio.Lock()
        _browser_lock_loop = loop
    return _browser_lock


async def _browser() -> Any:
    global _browser_context, _browser_playwright
    async with _get_browser_lock():
        if _browser_context is not None:
            try:
                _ = _browser_context.pages
                return _browser_context
            except Exception:
                with contextlib.suppress(Exception):
                    await _browser_context.close()
                _browser_context = None
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ToolError("B站动态接口触发风控，服务器未安装浏览器降级组件。") from None
        _browser_playwright = await async_playwright().start()
        browser_dir = Path(os.environ.get("BOT_DATA_DIR", "data")) / "bililive-browser"
        browser_dir.mkdir(parents=True, exist_ok=True)
        try:
            _browser_context = await _browser_playwright.chromium.launch_persistent_context(
                browser_dir,
                headless=True,
                chromium_sandbox=False,
                user_agent=HEADERS["User-Agent"],
                device_scale_factor=1,
                timeout=30_000,
                args=["--disable-dev-shm-usage"],
            )
            await _browser_context.set_extra_http_headers({
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            })
            return _browser_context
        except Exception as exc:
            with contextlib.suppress(Exception):
                await _browser_playwright.stop()
            _browser_playwright = None
            raise ToolError("B站动态浏览器降级组件启动失败。") from exc


async def _fetch_dynamic_in_browser(uid: int, *, known_dynamic_id: int | None = None) -> dict[str, Any]:
    context = await _browser()
    page = await context.new_page()
    try:
        await page.set_extra_http_headers({
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://space.bilibili.com/{uid}/dynamic",
        })
        await page.goto(
            "https://www.bilibili.com/", wait_until="domcontentloaded", timeout=30_000,
        )
        payload = None
        for api in (DYNAMIC_API, DYNAMIC_DESKTOP_API):
            candidate = await asyncio.wait_for(page.evaluate(
                """async ({url, uid}) => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), 10000);
                    try {
                        const query = new URLSearchParams({host_mid: uid, type: 'all',
                            platform: 'web', timezone_offset: '-480', web_location: '333.1387'});
                        const response = await fetch(`${url}?${query}`, {
                            credentials: 'include', signal: controller.signal,
                            headers: {'Accept': 'application/json, text/plain, */*'}
                        });
                        return await response.json();
                    } catch (_) { return {code: -1}; }
                    finally { clearTimeout(timer); }
                }""",
                {"url": api, "uid": str(uid)},
            ), timeout=12)
            if isinstance(candidate, dict) and candidate.get("code") == 0:
                try:
                    items = _validated_dynamic_items(candidate)
                except ToolError:
                    continue
                payload = candidate
                if items and (known_dynamic_id is None or max(x.dynamic_id for x in items) >= known_dynamic_id):
                    break
    except Exception as exc:
        raise ToolError("B站动态浏览器请求失败，请稍后重试。") from exc
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(page.close(), timeout=3)
    if not isinstance(payload, dict):
        raise ToolError("B站动态浏览器返回格式不正确。")
    if payload.get("code") != 0:
        code = payload.get("code", "未知")
        raise ToolError(f"B站动态接口仍受风控（代码 {code}），请稍后重试。")
    return payload


def _dynamic_card_image(data: bytes) -> bytes:
    """Normalize one API image for a static QQ card.

    Gallery uploads retain GIF animation and therefore use the stricter shared
    validator.  A dynamic card only needs its first frame, so a bounded larger
    GIF can be decoded once and converted to a small JPEG instead of being
    discarded solely because the complete animation exceeds 4 MiB.
    """
    if not data.startswith((b"GIF87a", b"GIF89a")):
        return checked_image(data)
    if len(data) > MAX_DYNAMIC_GIF_BYTES:
        raise ToolError("B站动态 GIF 过大。")
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format != "GIF" or source.width * source.height > MAX_DYNAMIC_IMAGE_PIXELS:
                raise ToolError("B站动态 GIF 尺寸过大或格式不正确。")
            source.seek(0)
            frame = source.convert("RGBA")
            result = Image.new("RGB", frame.size, "white")
            result.paste(frame, mask=frame.getchannel("A"))
            result.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            result.save(output, "JPEG", quality=88, optimize=True)
            return output.getvalue()
    except ToolError:
        raise
    except (
        UnidentifiedImageError, OSError, ValueError,
        Image.DecompressionBombError, Image.DecompressionBombWarning,
    ):
        raise ToolError("B站动态 GIF 损坏、过大或格式不受支持。") from None


async def _fetch_bili_image(
    url: str, transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    if url.startswith("http://"):
        url = "https://" + url.removeprefix("http://")
    if not _valid_bili_image_url(url):
        raise ToolError("B站动态图片地址不受信任。")
    try:
        async with client(
            "bililive-card-image", headers=HEADERS, timeout=15,
            follow_redirects=False, trust_env=False, transport=transport,
        ) as session:
            async with session.stream("GET", url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if not content_type.startswith("image/"):
                    raise ToolError("B站动态图片格式不正确。")
                is_gif = content_type == "image/gif" or urlsplit(url).path.lower().endswith(".gif")
                byte_limit = MAX_DYNAMIC_GIF_BYTES if is_gif else MAX_DYNAMIC_IMAGE_BYTES
                data = bytearray()
                async for chunk in response.aiter_bytes(65_536):
                    data.extend(chunk)
                    if len(data) > byte_limit:
                        raise ToolError("B站动态图片过大。")
    except httpx.HTTPError:
        raise ToolError("B站动态图片下载失败。") from None
    return await asyncio.to_thread(_dynamic_card_image, bytes(data))


async def fetch_dynamic_card(item: DynamicItem) -> bytes:
    """Build a stable dynamic card from the already fetched public API data."""
    semaphore = asyncio.Semaphore(4)

    async def download(url: str) -> bytes | None:
        if not url:
            return None
        try:
            async with semaphore:
                return await _fetch_bili_image(url)
        except Exception:
            return None

    assets = await asyncio.gather(*(
        download(url) for url in (item.avatar_url, *item.image_urls)
    ))
    avatar = assets[0] if assets else None
    pictures = [data for data in assets[1:] if data is not None]
    return await asyncio.to_thread(
        render_dynamic_card,
        author=item.author,
        body=item.summary or DYNAMIC_LABELS.get(item.dynamic_type, "发布了新动态"),
        published_at=item.published_at,
        avatar_data=avatar,
        picture_data=pictures,
        verified=item.verified,
    )


async def close_browser() -> None:
    global _browser_context, _browser_playwright
    async with _get_browser_lock():
        if _browser_context is not None:
            with contextlib.suppress(Exception):
                await _browser_context.close()
            _browser_context = None
        if _browser_playwright is not None:
            with contextlib.suppress(Exception):
                await _browser_playwright.stop()
            _browser_playwright = None


class BiliLiveStore:
    def __init__(self, store: Store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bililive_targets (
                    scope TEXT PRIMARY KEY, bot TEXT NOT NULL, kind TEXT NOT NULL,
                    target BLOB NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS bililive_users (
                    uid INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
                    live_status INTEGER, dynamic_id INTEGER,
                    last_live_check REAL NOT NULL DEFAULT 0,
                    last_dynamic_check REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    last_error_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS bililive_subscriptions (
                    scope TEXT NOT NULL, bot TEXT NOT NULL, uid INTEGER NOT NULL,
                    live INTEGER NOT NULL DEFAULT 1,
                    dynamic INTEGER NOT NULL DEFAULT 1,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(scope,uid),
                    FOREIGN KEY(uid) REFERENCES bililive_users(uid));
                CREATE INDEX IF NOT EXISTS idx_bililive_sub_live
                    ON bililive_subscriptions(live,uid);
                CREATE INDEX IF NOT EXISTS idx_bililive_sub_dynamic
                    ON bililive_subscriptions(dynamic,uid);
                CREATE TABLE IF NOT EXISTS bililive_dynamic_events (
                    dynamic_id INTEGER PRIMARY KEY, uid INTEGER NOT NULL,
                    published_at REAL NOT NULL, discovered_at REAL NOT NULL,
                    completed_at REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'discovered',
                    sent INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0);
            """)

    def _vault(self, db) -> Fernet:
        folder = self.store.path / "secrets"
        path = folder / "bililive-targets-master.key"
        if folder.is_symlink() or path.is_symlink():
            raise ToolError("B站推送目标加密文件路径异常，请检查数据目录。")
        folder.mkdir(mode=0o700, exist_ok=True)
        if not path.exists():
            if db.execute("SELECT 1 FROM bililive_targets LIMIT 1").fetchone():
                raise ToolError("B站推送目标主密钥丢失，请从备份恢复。")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(Fernet.generate_key())
                output.flush()
                os.fsync(output.fileno())
        try:
            return Fernet(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ToolError("无法读取 B站推送目标主密钥。") from exc

    def _save_target(self, db, who: Identity, now: float) -> None:
        if who.private or not who.scope.startswith("group:"):
            raise ToolError("B站推送目前只支持 QQ 群；请在目标群内设置。")
        target = who.scope.removeprefix("group:")
        encrypted = self._vault(db).encrypt(target.encode("utf-8"))
        db.execute(
            "INSERT INTO bililive_targets(scope,bot,kind,target,updated) VALUES(?,?,?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET bot=excluded.bot,kind=excluded.kind,"
            "target=excluded.target,updated=excluded.updated",
            (who.scope_key, who.bot, "group", encrypted, now),
        )

    def subscribe(self, who: Identity, uid: int, name: str, now: float | None = None) -> bool:
        self.store.require_admin(who)
        now = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._save_target(db, who, now)
            exists = db.execute(
                "SELECT 1 FROM bililive_subscriptions WHERE scope=? AND uid=?",
                (who.scope_key, uid),
            ).fetchone()
            if exists:
                db.execute("UPDATE bililive_users SET name=? WHERE uid=?", (name, uid))
                db.execute(
                    "UPDATE bililive_subscriptions SET live=1,dynamic=1,updated=? "
                    "WHERE scope=? AND uid=?", (now, who.scope_key, uid),
                )
                return False
            total = db.execute(
                "SELECT COUNT(*) FROM bililive_subscriptions WHERE scope=?", (who.scope_key,),
            ).fetchone()[0]
            if total >= MAX_SUBSCRIPTIONS:
                raise ToolError(f"每个群最多关注 {MAX_SUBSCRIPTIONS} 位 B站用户。")
            db.execute(
                "INSERT INTO bililive_users(uid,name) VALUES(?,?) "
                "ON CONFLICT(uid) DO UPDATE SET name=excluded.name",
                (uid, name),
            )
            db.execute(
                "INSERT INTO bililive_subscriptions(scope,bot,uid,live,dynamic,created,updated) "
                "VALUES(?,?,?,?,?,?,?)",
                (who.scope_key, who.bot, uid, 1, 1, now, now),
            )
        self.store.remember_scope(who, now)
        return True

    def unsubscribe(self, who: Identity, uid: int) -> str:
        self.store.require_admin(who)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT u.name FROM bililive_subscriptions s JOIN bililive_users u ON u.uid=s.uid "
                "WHERE s.scope=? AND s.uid=?", (who.scope_key, uid),
            ).fetchone()
            if row is None:
                raise ToolError(f"本群没有关注 UID {uid}。")
            db.execute(
                "DELETE FROM bililive_subscriptions WHERE scope=? AND uid=?",
                (who.scope_key, uid),
            )
            if not db.execute(
                "SELECT 1 FROM bililive_subscriptions WHERE scope=? LIMIT 1", (who.scope_key,),
            ).fetchone():
                db.execute("DELETE FROM bililive_targets WHERE scope=?", (who.scope_key,))
        return row["name"] or str(uid)

    def set_mode(self, who: Identity, uid: int, mode: str, enabled: bool,
                 now: float | None = None) -> str:
        self.store.require_admin(who)
        if mode not in {"live", "dynamic"}:
            raise ToolError("未知推送类型。")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT u.name FROM bililive_subscriptions s JOIN bililive_users u ON u.uid=s.uid "
                "WHERE s.scope=? AND s.uid=?", (who.scope_key, uid),
            ).fetchone()
            if row is None:
                raise ToolError(f"本群没有关注 UID {uid}，请先关注。")
            self._save_target(db, who, time.time() if now is None else now)
            db.execute(
                f"UPDATE bililive_subscriptions SET {mode}=?,updated=? WHERE scope=? AND uid=?",
                (int(enabled), time.time() if now is None else now, who.scope_key, uid),
            )
            return row["name"] or str(uid)

    def list(self, who: Identity) -> list[dict[str, Any]]:
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT s.uid,u.name,s.live,s.dynamic,u.live_status,u.last_error,u.last_error_at "
                "FROM bililive_subscriptions s JOIN bililive_users u ON u.uid=s.uid "
                "WHERE s.scope=? ORDER BY casefold(u.name),s.uid", (who.scope_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def uids(self, mode: str) -> list[int]:
        if mode not in {"live", "dynamic"}:
            raise ValueError("invalid bililive mode")
        with self.store.connect() as db:
            return [int(row[0]) for row in db.execute(
                f"SELECT DISTINCT uid FROM bililive_subscriptions WHERE {mode}=1 ORDER BY uid"
            )]

    def dynamic_cursor(self, uid: int) -> int | None:
        with self.store.connect() as db:
            row = db.execute("SELECT dynamic_id FROM bililive_users WHERE uid=?", (uid,)).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else None

    def targets(self, uid: int, mode: str) -> list[PushTarget]:
        if mode not in {"live", "dynamic"}:
            raise ValueError("invalid bililive mode")
        with self.store.connect() as db:
            rows = db.execute(
                f"SELECT t.bot,t.scope,t.kind,t.target FROM bililive_subscriptions s "
                f"JOIN bililive_targets t ON t.scope=s.scope WHERE s.uid=? AND s.{mode}=1",
                (uid,),
            ).fetchall()
            vault = self._vault(db) if rows else None
            result = []
            for row in rows:
                try:
                    target = vault.decrypt(bytes(row["target"])).decode("utf-8")
                except (InvalidToken, UnicodeError) as exc:
                    raise ToolError("B站推送目标无法解密，请在群内重新设置订阅。") from exc
                result.append(PushTarget(row["bot"], row["scope"], row["kind"], target))
        return result

    def update_live(self, snapshots: dict[int, dict[str, Any]],
                    now: float | None = None) -> list[Notice]:
        now = time.time() if now is None else now
        notices: list[Notice] = []
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for uid, info in snapshots.items():
                row = db.execute(
                    "SELECT live_status,name FROM bililive_users WHERE uid=?", (uid,),
                ).fetchone()
                if row is None:
                    continue
                name = _text(info.get("uname"), 80) or row["name"] or str(uid)
                try:
                    status = int(info.get("live_status") or 0)
                except (TypeError, ValueError):
                    status = 0
                status = 0 if status == 2 else int(status == 1)
                old = row["live_status"]
                db.execute(
                    "UPDATE bililive_users SET name=?,live_status=?,last_live_check=?,"
                    "last_error=CASE WHEN last_error LIKE 'live:%' THEN '' ELSE last_error END,"
                    "last_error_at=CASE WHEN last_error LIKE 'live:%' THEN 0 ELSE last_error_at END "
                    "WHERE uid=?", (name, status, now, uid),
                )
                if old is None or int(old) == status:
                    continue
                if status:
                    room = info.get("short_id") or info.get("room_id") or ""
                    title = _text(info.get("title"), 160) or "未填写标题"
                    parent = _text(info.get("area_v2_parent_name"), 40)
                    area = _text(info.get("area_v2_name"), 40)
                    area_text = " / ".join(item for item in (parent, area) if item)
                    lines = [f"🔴 {name} 开播了", f"标题：{title}"]
                    if area_text:
                        lines.append(f"分区：{area_text}")
                    lines.append(f"https://live.bilibili.com/{room}")
                    notices.append(Notice(uid, "live", "\n".join(lines)))
                elif _env_bool("BILILIVE_OFF_NOTIFY", False):
                    notices.append(Notice(uid, "live", f"⚪ {name} 下播了。"))
        return notices

    def update_dynamic(self, uid: int, items: list[DynamicItem],
                       now: float | None = None) -> list[Notice]:
        now = time.time() if now is None else now
        if not items:
            with self.store.connect() as db:
                # Defensive guard for callers outside fetch_dynamic too.
                row = db.execute("SELECT dynamic_id FROM bililive_users WHERE uid=?", (uid,)).fetchone()
                if row is not None and row[0] is not None:
                    raise BiliRiskControl("B站动态列表异常为空，已保留上次检查位置。")
                db.execute("UPDATE bililive_users SET last_dynamic_check=? WHERE uid=?", (now, uid))
            return []
        newest = max(item.dynamic_id for item in items)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT dynamic_id,name FROM bililive_users WHERE uid=?", (uid,),
            ).fetchone()
            if row is None:
                return []
            old = row["dynamic_id"]
            if old is not None and newest < int(old):
                raise BiliRiskControl("B站动态列表暂时落后，已保留上次检查位置。")
            author = next((item.author for item in items if item.author), row["name"])
            db.execute(
                "UPDATE bililive_users SET name=?,dynamic_id=?,last_dynamic_check=?,"
                "last_error=CASE WHEN last_error LIKE 'dynamic:%' THEN '' ELSE last_error END,"
                "last_error_at=CASE WHEN last_error LIKE 'dynamic:%' THEN 0 ELSE last_error_at END WHERE uid=?",
                (author or row["name"], newest, now, uid),
            )
            if old is None:
                return []
            fresh = sorted(
                (item for item in items if item.dynamic_id > int(old)
                 and item.dynamic_type not in SKIP_DYNAMIC_TYPES),
                key=lambda item: item.dynamic_id,
            )[-3:]
            db.executemany(
                "INSERT OR IGNORE INTO bililive_dynamic_events(dynamic_id,uid,published_at,discovered_at) VALUES(?,?,?,?)",
                [(item.dynamic_id, uid, item.published_at, now) for item in fresh],
            )
            db.execute("DELETE FROM bililive_dynamic_events WHERE dynamic_id IN "
                       "(SELECT dynamic_id FROM bililive_dynamic_events ORDER BY discovered_at DESC LIMIT -1 OFFSET 2000)")
        notices = []
        for item in fresh:
            label = DYNAMIC_LABELS.get(item.dynamic_type, "发布了新动态")
            lines = [f"📰 {item.author} {label}"]
            if item.summary:
                lines.append(item.summary)
            lines.append(f"https://t.bilibili.com/{item.dynamic_id}")
            notices.append(Notice(
                uid, "dynamic", "\n".join(lines), dynamic_id=item.dynamic_id,
                dynamic=item,
            ))
        return notices

    def record_dynamic_delivery(self, dynamic_id: int, *, sent: int = 0,
                                failed: int = 0, skipped: int = 0,
                                now: float | None = None) -> None:
        status = "partial" if sent and failed else "sent" if sent else "failed" if failed else "skipped"
        with self.store.connect() as db:
            db.execute("UPDATE bililive_dynamic_events SET completed_at=?,status=?,sent=?,failed=?,skipped=? WHERE dynamic_id=?",
                       (time.time() if now is None else now, status, sent, failed, skipped, dynamic_id))

    def set_error(self, uid: int, kind: str, error: BaseException,
                  now: float | None = None) -> None:
        safe = f"{kind}:{type(error).__name__}"[:80]
        with self.store.connect() as db:
            db.execute(
                "UPDATE bililive_users SET last_error=?,last_error_at=? WHERE uid=?",
                (safe, time.time() if now is None else now, uid),
            )

    def clear_error(self, uid: int, kind: str) -> None:
        with self.store.connect() as db:
            db.execute("UPDATE bililive_users SET last_error='',last_error_at=0 WHERE uid=? AND last_error LIKE ?",
                       (uid, kind + ":%"))


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def help_text() -> str:
    return help_panel("B站直播与动态推送", [
        "/bili 关注 UID · 同时开启直播和动态推送",
        "/bili 取关 UID",
        "/bili 列表 · 查看本群订阅与开关",
        "/bili 已开播 · 查看订阅中正在直播的人",
        "/bili 开启直播 UID · /bili 关闭直播 UID",
        "/bili 开启动态 UID · /bili 关闭动态 UID",
        "/bili 状态 · 查看最近接口检查或错误",
    ], footer="关注、取关和开关仅限群管理员；首次轮询只记录当前状态，不补发历史动态。")


async def dispatch(
    store: Store, who: Identity, raw: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    if who.private or not who.scope.startswith("group:"):
        raise ToolError("B站推送目前只支持 QQ 群，请在目标群内使用 /bili。")
    args = raw.split()
    service = BiliLiveStore(store)
    if not args or args == ["help"] or args == ["帮助"]:
        return help_text()
    action = args[0]
    if action in {"关注", "添加", "subscribe"}:
        if len(args) != 2:
            raise ToolError("用法：/bili 关注 UID")
        uid = parse_uid(args[1])
        name = await fetch_user(uid, transport)
        created = await asyncio.to_thread(service.subscribe, who, uid, name)
        return panel("B站关注已保存" if created else "本群已经关注", [
            f"{name}（UID {uid}）", "直播推送：开 · 动态推送：开",
        ], footer="首次检查只建立状态基线，不会补发旧直播或历史动态。")
    if action in {"取关", "删除", "unsubscribe"}:
        if len(args) != 2:
            raise ToolError("用法：/bili 取关 UID")
        uid = parse_uid(args[1])
        name = await asyncio.to_thread(service.unsubscribe, who, uid)
        return panel("B站关注已移除", f"{name}（UID {uid}）不再向本群推送。")
    if action in {"开启直播", "关闭直播", "开启动态", "关闭动态"}:
        if len(args) != 2:
            raise ToolError(f"用法：/bili {action} UID")
        uid = parse_uid(args[1])
        mode = "live" if action.endswith("直播") else "dynamic"
        enabled = action.startswith("开启")
        name = await asyncio.to_thread(
            service.set_mode, who, uid, mode, enabled,
        )
        label = "直播" if mode == "live" else "动态"
        return panel("B站推送设置已更新", f"{name}（UID {uid}）的{label}推送已{'开启' if enabled else '关闭'}。")
    rows = await asyncio.to_thread(service.list, who)
    if action in {"列表", "list"}:
        lines = [
            f"{row['name']}（{row['uid']}）· 直播{'开' if row['live'] else '关'} · 动态{'开' if row['dynamic'] else '关'}"
            for row in rows
        ]
        return panel("本群 B站关注", lines or ["尚未关注任何 B站用户。"],
                     footer=f"共 {len(rows)} / {MAX_SUBSCRIPTIONS} 个；/bili help 查看管理命令。")
    if action in {"已开播", "直播中"}:
        live_rows = [row for row in rows if row["live"] and row["live_status"] == 1]
        return panel("本群订阅 · 正在直播", [
            f"🔴 {row['name']}（UID {row['uid']}）" for row in live_rows
        ] or ["当前没有检测到正在直播的订阅。"], footer="状态由后台定时检查更新。")
    if action in {"状态", "status"}:
        errors = [row for row in rows if row["last_error"]]
        return panel("B站推送状态", [
            f"关注：{len(rows)} 个",
            f"直播推送：{sum(bool(row['live']) for row in rows)} 个",
            f"动态推送：{sum(bool(row['dynamic']) for row in rows)} 个",
            *(f"UID {row['uid']}：最近检查失败（{row['last_error']}）" for row in errors[:8]),
        ], footer="主动消息若被 QQ 平台拒绝，会记录在服务端日志；不会重复发送。")
    raise ToolError("不支持的操作。发送 /bili help 查看用法。")
