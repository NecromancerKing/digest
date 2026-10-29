"""Map data for story maps (D35): country outlines and towns from Natural Earth.

Natural Earth is public domain. ``make_maps`` downloads the pinned files, checks their
hashes, projects everything once (Mercator, 1000 units wide, Antarctica left out),
simplifies the shared borders and writes two files that are committed:

- ``static/img/world.svg``: every country as one path, which each map on the site draws
  with ``<use>``, so a browser fetches the world once;
- ``data/maps.json``: each country's rings and box, and the towns stories may name, for
  the build to draw a story's own country and dot.

Run ``uv run poe maps`` only to change the data.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pycountry

DATA = Path(__file__).parent / "data" / "maps.json"
WORLD = Path(__file__).parent / "static" / "img" / "world.svg"
COUNTRIES = (
    "https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-50m.json",
    # pragma: allowlist nextline secret
    "04342cdc1e3016bcd7db1630de95684d67b79fe3c8c460321e87aef469502394",
)
TOWNS = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/"
    "ne_10m_populated_places_simple.geojson",
    # pragma: allowlist nextline secret
    "fd3fa867a320cbd5c5b6bb5bc550afeec2939fb2cef688e508007282a55ac42f",
)
WIDTH = 1000
TOP_LAT, BOTTOM_LAT = 84.0, -60.0
TOLERANCE = 0.2  # map units dropped by simplification: under a pixel at story-page zoom
MIN_RING = 0.3  # rings smaller than this (islets) are left out
NORDIC = {"SE", "NO", "DK", "FI", "IS"}
# Countries Natural Earth gives no ISO number.
NO_NUMBER = {"Kosovo": "XK", "N. Cyprus": "CY", "Somaliland": "SO"}
# English names the towns file spells another way.
TOWN_ALIASES = {
    "Gothenburg": "Göteborg",
    "Kiev": "Kyiv",
    "Odessa": "Odesa",
    "Bombay": "Mumbai",
    "Calcutta": "Kolkata",
    "Peking": "Beijing",
    "Saigon": "Ho Chi Minh City",
    "Washington": "Washington, D.C.",
}


def _merc_y(lat: float) -> float:
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


Y_TOP = _merc_y(TOP_LAT)
SCALE = WIDTH / (2 * math.pi)
HEIGHT = round((Y_TOP - _merc_y(BOTTOM_LAT)) * SCALE)


def project(lon: float, lat: float) -> tuple[float, float]:
    """Longitude and latitude to map units: x from 0 to ``WIDTH``, y down from 84° N."""
    lat = max(min(lat, TOP_LAT), BOTTOM_LAT)
    return (lon + 180) / 360 * WIDTH, (Y_TOP - _merc_y(lat)) * SCALE


Point = tuple[float, float]


def simplify(points: Sequence[Point], tolerance: float) -> list[Point]:
    """Douglas-Peucker: drop points closer than ``tolerance`` to the line they lie on."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = points[a], points[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        far, at = 0.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            d = (
                abs(dy * (px - ax) - dx * (py - ay)) / norm
                if norm
                else math.hypot(px - ax, py - ay)
            )
            if d > far:
                far, at = d, i
        if far > tolerance:
            keep[at] = True
            stack += [(a, at), (at, b)]
    return [p for p, k in zip(points, keep, strict=True) if k]


def box(points: Iterable[Point]) -> tuple[float, float, float, float]:
    xs, ys = zip(*points, strict=True)
    return min(xs), min(ys), max(xs), max(ys)


def _arcs(topo: dict[str, Any]) -> list[list[Point]]:
    """TopoJSON's delta-encoded arcs, projected and simplified. Shared borders are one arc,
    so neighbours stay matched after simplification."""
    (sx, sy), (tx, ty) = topo["transform"]["scale"], topo["transform"]["translate"]
    out: list[list[Point]] = []
    for arc in topo["arcs"]:
        x = y = 0
        pts: list[Point] = []
        for dx, dy in arc:
            x, y = x + dx, y + dy
            pts.append(project(x * sx + tx, y * sy + ty))
        out.append(simplify(pts, TOLERANCE))
    return out


def _ring(arcs: list[list[Point]], refs: list[int]) -> list[Point]:
    pts: list[Point] = []
    for ref in refs:
        arc = arcs[ref] if ref >= 0 else arcs[~ref][::-1]
        pts += arc[1:] if pts else arc
    return pts


def _unwrap(ring: list[Point]) -> list[Point]:
    """A ring that crosses the 180th meridian (Russia's far east) jumps across the whole
    map; move its western half past the right edge so the ring stays in one piece."""
    if all(abs(a[0] - b[0]) < WIDTH / 2 for a, b in itertools.pairwise(ring)):
        return ring
    return [(x + WIDTH, y) if x < WIDTH / 2 else (x, y) for x, y in ring]


def _code(geom: dict[str, Any]) -> str | None:
    name = geom.get("properties", {}).get("name", "")
    if "id" not in geom:
        return NO_NUMBER.get(name)
    if geom["id"] == "010":  # Antarctica
        return None
    country = pycountry.countries.get(numeric=geom["id"])
    return country.alpha_2 if country else None


def _countries(topo: dict[str, Any]) -> dict[str, list[list[Point]]]:
    arcs = _arcs(topo)
    out: dict[str, list[list[Point]]] = {}
    for geom in topo["objects"]["countries"]["geometries"]:
        code = _code(geom)
        if code is None:
            continue
        polys = geom["arcs"] if geom["type"] == "MultiPolygon" else [geom["arcs"]]
        for poly in polys:
            for refs in poly:
                ring = _unwrap(_ring(arcs, refs))
                x0, y0, x1, y1 = box(ring)
                if len(ring) >= 4 and max(x1 - x0, y1 - y0) >= MIN_RING:
                    out.setdefault(code, []).append(ring)
    return out


def path_d(rings: Iterable[Sequence[Point]]) -> str:
    """Rings as compact SVG path data: one decimal, relative moves after the first point."""
    parts: list[str] = []
    for ring in rings:
        pts = [(round(x * 10), round(y * 10)) for x, y in ring]
        cmds = [f"M{pts[0][0] / 10:g} {pts[0][1] / 10:g}"]
        px, py = pts[0]
        for x, y in pts[1:]:
            if (x, y) != (px, py):
                cmds.append(f"{(x - px) / 10:g} {(y - py) / 10:g}")
                px, py = x, y
        parts.append(cmds[0] + "l" + " ".join(cmds[1:]) + "z")
    return "".join(parts).replace(" -", "-")


def _towns(geo: dict[str, Any]) -> dict[str, list[Any]]:
    """Lowercased town name -> [country code, x, y]. Bigger towns win a shared name."""
    best: dict[str, tuple[int, list[Any]]] = {}
    for f in geo["features"]:
        p = f["properties"]
        code, pop = p.get("iso_a2"), p.get("pop_max") or 0
        if not code or code == "-99":
            continue
        if not (pop >= 100_000 or p.get("adm0cap") or (code in NORDIC and pop >= 10_000)):
            continue
        x, y = project(p["longitude"], p["latitude"])
        row = [code, round(x, 1), round(y, 1)]
        names = {p["name"], p["nameascii"], *(p.get("namealt") or "").split("|")}
        for n in names:
            key = n.strip().lower()
            if key and (key not in best or best[key][0] < pop):
                best[key] = (pop, row)
    out = {k: v for k, (_, v) in sorted(best.items())}
    for alias, name in TOWN_ALIASES.items():
        if name.lower() in out:
            out.setdefault(alias.lower(), out[name.lower()])
    return out


def _download(url: str, sha256: str) -> bytes:
    r = httpx.get(url, follow_redirects=True, timeout=120)
    r.raise_for_status()
    digest = hashlib.sha256(r.content).hexdigest()
    if digest != sha256:
        raise ValueError(f"{url}: sha256 {digest} is not the pinned one")
    return r.content


def make_maps() -> dict[str, int]:
    """Write ``world.svg`` and ``maps.json``; returns their sizes in bytes."""
    countries = _countries(json.loads(_download(*COUNTRIES)))
    towns = _towns(json.loads(_download(*TOWNS)))
    every = [ring for rings in countries.values() for ring in rings]
    WORLD.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}">'
        f'<path id="w" vector-effect="non-scaling-stroke" d="{path_d(every)}"/></svg>\n',
        encoding="utf-8",
    )
    data = {
        "height": HEIGHT,
        "countries": {
            code: [[c for x, y in ring for c in (round(x, 1), round(y, 1))] for ring in rings]
            for code, rings in sorted(countries.items())
        },
        "towns": towns,
    }
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", "utf-8")
    return {WORLD.name: WORLD.stat().st_size, DATA.name: DATA.stat().st_size}
