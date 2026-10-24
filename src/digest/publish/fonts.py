"""The site's two fonts, subset and self-hosted (spec section 10, D17).

Fraunces (headlines) and Figtree (text) are SIL OFL fonts from Google's font repository.
``make_fonts`` downloads the pinned files, checks their hashes, pins Fraunces's ``SOFT``
and ``WONK`` axes, keeps only the weights we use, and writes woff2 subsets with their
licence files into ``static/fonts``. The output is committed; run ``uv run poe fonts``
only to change a font. Needs the ``fonttools`` dev dependency.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import httpx

from digest.publish.render import STATIC

BASE = "https://github.com/google/fonts/raw/main/ofl"
OUT = STATIC / "fonts"
BUDGET_BYTES = 110 * 1024  # NFR-1
# Basic Latin, Latin-1 Supplement, Latin Extended-A, General Punctuation, € and arrows.
UNICODES = "U+0020-007E,U+00A0-00FF,U+0100-017F,U+2000-206F,U+20AC,U+2190-2193"


@dataclass(frozen=True)
class FontSpec:
    name: str  # output file stem
    path: str  # under BASE
    sha256: str
    axes: dict[str, float | tuple[float, float]]  # fontTools instancer limits
    licence: str  # path of OFL.txt under BASE


FONTS = (
    FontSpec(
        name="fraunces",
        path="fraunces/Fraunces%5BSOFT,WONK,opsz,wght%5D.ttf",
        # pragma: allowlist nextline secret
        sha256="177ff6c0f14e5550a3c624247cd1189611d4eb65d000b14944c63d967958abbb",
        axes={"SOFT": 50, "WONK": 0, "wght": (500, 700)},
        licence="fraunces/OFL.txt",
    ),
    FontSpec(
        name="figtree",
        path="figtree/Figtree%5Bwght%5D.ttf",
        # pragma: allowlist nextline secret
        sha256="26ad3db9b31ff7dde67a91ff515d022d2f495cd506590699cf264f0bfe6fb714",
        axes={"wght": (400, 700)},
        licence="figtree/OFL.txt",
    ),
)


def _download(url: str) -> bytes:
    r = httpx.get(url, follow_redirects=True, timeout=60)
    r.raise_for_status()
    return r.content


def make_fonts(out: Path = OUT) -> dict[str, int]:
    """Write ``<name>.woff2`` and ``<name>-OFL.txt`` per font; returns woff2 sizes in bytes."""
    import io

    from fontTools import subset
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    out.mkdir(parents=True, exist_ok=True)
    sizes: dict[str, int] = {}
    for spec in FONTS:
        raw = _download(f"{BASE}/{spec.path}")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec.sha256:
            raise ValueError(f"{spec.name}: sha256 {digest} is not the pinned one")
        pinned = instancer.instantiateVariableFont(TTFont(io.BytesIO(raw)), spec.axes)
        buf = io.BytesIO()
        pinned.save(buf)  # a round trip rebuilds gvar, which the subsetter needs
        font = TTFont(io.BytesIO(buf.getvalue()))
        options = subset.Options(
            flavor="woff2",
            layout_features=["kern", "liga", "calt", "tnum", "lnum", "case"],
            name_IDs=["*"],  # keep the copyright and licence notices
        )
        subsetter = subset.Subsetter(options)
        subsetter.populate(unicodes=subset.parse_unicodes(UNICODES))
        subsetter.subset(font)
        target = out / f"{spec.name}.woff2"
        font.flavor = "woff2"
        font.save(str(target))
        sizes[spec.name] = target.stat().st_size
        (out / f"{spec.name}-OFL.txt").write_bytes(_download(f"{BASE}/{spec.licence}"))
    total = sum(sizes.values())
    if total > BUDGET_BYTES:
        raise ValueError(f"fonts are {total} bytes, over the {BUDGET_BYTES}-byte budget")
    return sizes
