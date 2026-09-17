"""Pillow renderer for Bilibili dynamic notification cards."""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageOps

from bot_tools.media import RENDER_LOCK, font


CARD_WIDTH = 920
PADDING = 44
CONTENT_WIDTH = CARD_WIDTH - PADDING * 2


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, text_font, width: int,
               max_lines: int = 60) -> list[str]:
    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not paragraph:
            lines.append("")
            if len(lines) >= max_lines:
                break
            continue
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and draw.textlength(candidate, font=text_font) > width:
                lines.append(current.rstrip())
                current = char.lstrip() if char.isspace() else char
                if len(lines) >= max_lines:
                    break
            else:
                current = candidate
        if len(lines) >= max_lines:
            break
        if current:
            lines.append(current.rstrip())
        if len(lines) >= max_lines:
            break
    if len(lines) >= max_lines and lines:
        lines[-1] = lines[-1].rstrip("…") + "…"
    return lines or [""]


def _open_image(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        return source.convert("RGB")


def _rounded(image: Image.Image, radius: int) -> tuple[Image.Image, Image.Image]:
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, image.width - 1, image.height - 1), radius=radius, fill=255,
    )
    return image, mask


def _avatar(data: bytes | None, size: int) -> tuple[Image.Image, Image.Image]:
    if data:
        image = ImageOps.fit(_open_image(data), (size, size), Image.Resampling.LANCZOS)
    else:
        image = Image.new("RGB", (size, size), "#00aeec")
        draw = ImageDraw.Draw(image)
        label_font = font(34)
        box = draw.textbbox((0, 0), "B", font=label_font)
        draw.text(
            ((size - (box[2] - box[0])) / 2, (size - (box[3] - box[1])) / 2 - box[1]),
            "B", font=label_font, fill="white",
        )
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    return image, mask


def _prepare_pictures(picture_data: list[bytes]) -> list[Image.Image]:
    pictures: list[Image.Image] = []
    for data in picture_data[:9]:
        try:
            pictures.append(_open_image(data))
        except Exception:
            continue
    return pictures


def render_dynamic_card(*, author: str, body: str, published_at: int = 0,
                        avatar_data: bytes | None = None,
                        picture_data: list[bytes] | None = None,
                        verified: bool = False) -> bytes:
    """Create a readable Bilibili-style JPEG card for a QQ group message."""
    picture_data = picture_data or []
    with RENDER_LOCK:
        measure = Image.new("RGB", (CARD_WIDTH, 64), "white")
        measure_draw = ImageDraw.Draw(measure)
        author_font = font(34)
        time_font = font(22)
        body_font = font(29)
        footer_font = font(20)
        lines = _wrap_text(measure_draw, body.strip(), body_font, CONTENT_WIDTH)
        line_height = 46
        body_height = sum(30 if line == "" else line_height for line in lines)
        pictures = _prepare_pictures(picture_data)

        image_gap = 12
        image_top_gap = 28 if pictures else 0
        picture_layout: list[tuple[Image.Image, int, int, int, int]] = []
        pictures_height = 0
        if len(pictures) == 1:
            source = pictures[0]
            scale = min(CONTENT_WIDTH / source.width, 1_200 / source.height, 1.0)
            width = max(1, round(source.width * scale))
            height = max(1, round(source.height * scale))
            resized = source.resize((width, height), Image.Resampling.LANCZOS)
            picture_layout.append((resized, PADDING, 0, width, height))
            pictures_height = height
        elif pictures:
            columns = 2 if len(pictures) in {2, 4} else 3
            cell = (CONTENT_WIDTH - image_gap * (columns - 1)) // columns
            rows = (len(pictures) + columns - 1) // columns
            pictures_height = rows * cell + (rows - 1) * image_gap
            for index, source in enumerate(pictures):
                tile = ImageOps.fit(source, (cell, cell), Image.Resampling.LANCZOS)
                x = PADDING + (index % columns) * (cell + image_gap)
                y = (index // columns) * (cell + image_gap)
                picture_layout.append((tile, x, y, cell, cell))

        header_height = 96
        footer_height = 72
        card_height = (
            PADDING + header_height + 22 + body_height + image_top_gap
            + pictures_height + 30 + footer_height
        )
        canvas = Image.new("RGB", (CARD_WIDTH, card_height), "#ffffff")
        draw = ImageDraw.Draw(canvas)

        avatar, avatar_mask = _avatar(avatar_data, 76)
        canvas.paste(avatar, (PADDING, PADDING), avatar_mask)
        text_x = PADDING + 96
        draw.text((text_x, PADDING + 2), author or "B站用户", font=author_font, fill="#18191c")
        if published_at > 0:
            timestamp = datetime.fromtimestamp(
                published_at, timezone(timedelta(hours=8)),
            ).strftime("%Y年%m月%d日 %H:%M")
        else:
            timestamp = "刚刚发布"
        draw.text((text_x, PADDING + 53), timestamp, font=time_font, fill="#9499a0")
        if verified:
            badge_x = min(
                CARD_WIDTH - PADDING - 22,
                text_x + int(draw.textlength(author or "B站用户", font=author_font)) + 12,
            )
            draw.ellipse(
                (badge_x, PADDING + 9, badge_x + 22, PADDING + 31), fill="#00aeec",
            )
            draw.text((badge_x + 5, PADDING + 8), "✓", font=font(16), fill="white")

        y = PADDING + header_height + 22
        for line in lines:
            if line:
                draw.text((PADDING, y), line, font=body_font, fill="#18191c")
                y += line_height
            else:
                y += 30

        y += image_top_gap
        for source, x, offset_y, width, height in picture_layout:
            rounded, mask = _rounded(source, 12)
            canvas.paste(rounded, (x, y + offset_y), mask)
        y += pictures_height + 30
        draw.line((PADDING, y, CARD_WIDTH - PADDING, y), fill="#e3e5e7", width=1)
        draw.text(
            (PADDING, y + 24), "哔哩哔哩动态 · 完整内容见消息下方链接",
            font=footer_font, fill="#9499a0",
        )

        output = io.BytesIO()
        canvas.save(output, "JPEG", quality=90, optimize=True, progressive=True)
        return output.getvalue()
