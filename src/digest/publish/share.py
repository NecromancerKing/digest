"""Share images (D29): a 1200 by 630 PNG per recent story, for link previews and Discover.

Drawn from our own words only (headline, section, source count, date) in the site's fonts
and colours, never from source images. Only stories published in the last
``publish.share_image_days`` get one, so builds stay quick and the file count low; older
pages keep the site-wide card.
"""

from __future__ import annotations

import io
import unicodedata
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONTS = Path(__file__).resolve().parent / "static" / "fonts"
WIDTH, HEIGHT = 1200, 630
PAD = 80
BG, FG, MUTED, EMBER = "#F5F1EB", "#2B2724", "#6B625A", "#B33A0A"  # site.css, light theme
# Section colours (site.css, light theme): text, and the tint behind it.
COLOURS = {
    "blue": ("#2F5DA8", "#E6EEFA"),
    "teal": ("#176964", "#E1F2F0"),
    "amber": ("#9A5B00", "#FBEFD9"),
    "violet": ("#6D4BC3", "#EEE9FB"),
    "green": ("#33702A", "#E7F3E1"),
    "rose": ("#B83A4B", "#FBE6E9"),
}
HEADLINE_SIZES = (88, 80, 72, 64, 58, 52, 48)  # the largest that fits wins
LINE_HEIGHT = 1.08  # .story-main h1
MARK = 60  # the logo mark, drawn like icon.svg


@cache
def _font(face: str, size: int, weight: int) -> ImageFont.FreeTypeFont:
    """``fraunces`` or ``figtree`` at ``size`` px; Fraunces's optical size follows the size."""
    font = ImageFont.truetype(FONTS / f"{face}.woff2", size)
    axes: list[float] = []
    for a in font.get_variation_axes():
        value = float(size if a["name"] == b"Optical Size" else weight)
        if a["minimum"] is not None:
            value = max(value, a["minimum"])
        if a["maximum"] is not None:
            value = min(value, a["maximum"])
        axes.append(value)
    font.set_variation_by_axes(axes)
    return font


@cache
def _glyph(face: str, ch: str) -> bytes:
    canvas = Image.new("L", (64, 64))
    ImageDraw.Draw(canvas).text((8, 8), ch, font=_font(face, 32, 500), fill=255)
    return canvas.tobytes()


def _has(face: str, ch: str) -> bool:
    return _glyph(face, ch) != _glyph(face, "\ue000")  # private use: drawn as the missing glyph


def drawable(face: str, text: str) -> str:
    """``text`` with characters the subset font lacks swapped for plain ones: ``ș`` -> ``s``,
    ``₂`` -> ``2``, a missing dash -> ``-``; anything else missing is left out."""
    out: list[str] = []
    for ch in text:
        if ch.isspace():
            out.append(" ")
        elif _has(face, ch):
            out.append(ch)
        else:
            plain = "".join(
                c
                for c in unicodedata.normalize("NFKD", ch)
                if not unicodedata.combining(c) and _has(face, c)
            )
            out.append(plain or ("-" if unicodedata.category(ch) == "Pd" else ""))
    return " ".join("".join(out).split())


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if font.getlength(trial) <= width:
            line = trial
            continue
        if line:
            lines.append(line)
        while font.getlength(word) > width:  # longer than a line: break it
            cut = len(word) - 1
            while cut > 1 and font.getlength(word[:cut]) > width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        line = word
    return [*lines, line] if line else lines


@dataclass(frozen=True)
class _Lines:
    font: ImageFont.FreeTypeFont
    lines: list[str]
    cap: int  # cap height: the first baseline sits this far below the block's top
    step: int  # baseline to baseline
    height: int


def _layout(text: str, size: int) -> _Lines:
    font = _font("fraunces", size, 600)
    lines = _wrap(text, font, WIDTH - 2 * PAD)
    cap = -round(font.getbbox("H", anchor="ls")[1])
    step = round(size * LINE_HEIGHT)
    return _Lines(font, lines, cap, step, cap + (len(lines) - 1) * step + size // 4)


def _fit(text: str, room: int) -> _Lines:
    """The largest headline size whose lines fit ``room``; else the smallest, cut short."""
    for size in HEADLINE_SIZES[:-1]:
        fitted = _layout(text, size)
        if fitted.height <= room:
            return fitted
    size = HEADLINE_SIZES[-1]
    smallest = _layout(text, size)
    keep = max(1, 1 + (room - smallest.cap - size // 4) // smallest.step)
    if len(smallest.lines) <= keep:
        return smallest
    lines = [*smallest.lines[: keep - 1], smallest.lines[keep - 1].rstrip(",;:") + "…"]
    return _Lines(smallest.font, lines, smallest.cap, smallest.step, room)


def render(*, headline: str, section: str, colour: str, footer: str, product: str) -> bytes:
    """The share image as PNG bytes: section tag, headline, then logo and ``footer``."""
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    ink, tint = COLOURS.get(colour, (FG, "#E8E0D5"))

    # Section tag: a pill with a dot, like .tag on the site.
    tag_font = _font("figtree", 28, 700)
    label = drawable("figtree", section)
    tag_h = 54
    tag_w = 24 + 12 + 12 + round(tag_font.getlength(label)) + 24
    draw.rounded_rectangle((PAD, PAD, PAD + tag_w, PAD + tag_h), radius=tag_h // 2, fill=tint)
    cy = PAD + tag_h // 2
    draw.ellipse((PAD + 24, cy - 6, PAD + 36, cy + 6), fill=ink)
    draw.text((PAD + 48, cy), label, font=tag_font, fill=ink, anchor="lm")

    # Footer: the logo mark and name on the left, the footer line on the right.
    top = HEIGHT - PAD - MARK
    draw.rounded_rectangle((PAD, top, PAD + MARK, top + MARK), radius=17, fill=FG)
    unit = MARK / 64
    for y, w, fill in ((19, 34, BG), (29, 24, BG), (39, 16, EMBER)):
        x0, y0 = PAD + 15 * unit, top + y * unit
        draw.rounded_rectangle((x0, y0, x0 + w * unit, y0 + 6 * unit), radius=3 * unit, fill=fill)
    my = top + MARK // 2
    name_font = _font("fraunces", 34, 650)
    draw.text(
        (PAD + MARK + 18, my), drawable("fraunces", product), font=name_font, fill=FG, anchor="lm"
    )
    foot_font = _font("figtree", 26, 500)
    draw.text(
        (WIDTH - PAD, my), drawable("figtree", footer), font=foot_font, fill=MUTED, anchor="rm"
    )

    # Headline: the largest size whose lines fit between the tag and the footer.
    first = PAD + tag_h + 44
    fitted = _fit(drawable("fraunces", headline), top - 40 - first)
    for i, line in enumerate(fitted.lines):
        baseline = first + fitted.cap + i * fitted.step
        draw.text((PAD, baseline), line, font=fitted.font, fill=FG, anchor="ls")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
