from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from digest.config import Config, Settings, load_config
from digest.publish.mapdata import DATA, HEIGHT, WIDTH, WORLD, path_d, simplify
from digest.publish.maps import clip, place, story_map
from digest.publish.model import Name
from digest.publish.render import build_site

from .conftest import CONFIG_DIR
from .test_publish import NOW, story


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config(CONFIG_DIR)


def places(*names: str) -> tuple[Name, ...]:
    return tuple(Name(n, "place") for n in names)


def test_place_knows_countries_in_english_and_swedish_and_towns() -> None:
    assert place("Ukraine") == ("UA", None)
    assert place("Ukraina") == ("UA", None)
    assert place("the Netherlands") == ("NL", None)
    assert place("U.S.") == ("US", None)
    code, at = place("Gothenburg")  # type: ignore[misc]
    assert code == "SE" and at is not None
    assert place("Göteborg") == place("Gothenburg")


def test_place_skips_ambiguous_and_unknown_names() -> None:
    assert place("Georgia") is None  # the country or the US state
    assert place("Jersey") is None
    assert place("Atlantis") is None


def test_no_map_without_a_known_place() -> None:
    assert story_map((), "thumb", "en") is None
    assert story_map((Name("Ukraine", "org"),), "thumb", "en") is None  # places only
    assert story_map(places("Atlantis"), "hero", "en") is None


def test_a_country_fills_without_a_dot() -> None:
    m = story_map(places("Ukraine"), "thumb", "en")
    assert m is not None
    assert m.on.startswith("M") and m.dot is None and m.also == ()
    assert m.label == "Ukraine"


def test_a_town_gets_a_dot_and_its_country() -> None:
    m = story_map(places("Gothenburg"), "hero", "en")
    assert m is not None and m.dot is not None
    assert m.label == "Gothenburg, Sweden"
    x, y, w, h = map(float, m.view.split())
    assert x <= m.dot[0] <= x + w and y <= m.dot[1] <= y + h


def test_a_town_named_later_in_the_same_country_still_gets_its_dot() -> None:
    m = story_map(places("Sweden", "Uppsala"), "thumb", "en")
    assert m is not None and m.dot is not None
    assert m.label == "Uppsala, Sweden"


def test_other_named_countries_are_shaded_at_most_two() -> None:
    m = story_map(places("Ukraine", "Russia", "Poland", "Belarus"), "hero", "en")
    assert m is not None and len(m.also) == 2


def test_labels_follow_the_edition_language() -> None:
    m = story_map(places("Russia"), "hero", "sv")
    assert m is not None and m.label == "Ryssland"
    m = story_map(places("Russia"), "hero", "en")
    assert m is not None and m.label == "Russia"


@pytest.mark.parametrize("country", ["Russia", "Canada", "United States", "Indonesia", "Chile"])
@pytest.mark.parametrize("kind", ["thumb", "hero"])
def test_views_stay_on_the_map_and_shapes_stay_small(country: str, kind: str) -> None:
    m = story_map(places(country), kind, "en")
    assert m is not None
    x, y, w, h = map(float, m.view.split())
    assert x >= 0 and y >= 0 and x + w <= WIDTH + 0.1 and y + h <= HEIGHT + 0.1
    assert len(m.on) < (6_000 if kind == "thumb" else 14_000)  # page weight (NFR-1)


def test_clip_keeps_the_part_inside() -> None:
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
    out = clip(square, (5, 5, 20, 20))
    xs, ys = zip(*out, strict=True)
    assert min(xs) == 5 and max(xs) == 10 and min(ys) == 5 and max(ys) == 10
    assert clip(square, (50, 50, 60, 60)) == []


def test_simplify_keeps_the_ends_and_drops_near_points() -> None:
    line = [(0.0, 0.0), (1.0, 0.01), (2.0, 0.0), (3.0, 5.0)]
    assert simplify(line, 0.1) == [(0.0, 0.0), (2.0, 0.0), (3.0, 5.0)]
    assert path_d([[(0, 0), (1, 0), (1, 1), (0, 0)]]) == "M0 0l1 0 0 1-1-1z"


def test_the_committed_map_data_is_there() -> None:
    svg = WORLD.read_text("utf-8")
    assert 'id="w"' in svg and "non-scaling-stroke" in svg
    assert WORLD.stat().st_size < 260_000  # one shared download, cached
    assert DATA.is_file()


def test_cards_get_a_map_or_the_section_icon_and_stories_a_hero(
    tmp_path: Path, config: Config
) -> None:
    out = tmp_path / "site"
    mapped = dataclasses.replace(story(1), names=places("Kyiv"))
    plain = story(2)
    build_site(out, [mapped, plain], settings=Settings(), config=config, now=NOW)
    home = (out / "index.html").read_text("utf-8")
    assert len(re.findall(r'<span class="thumb"><svg class="map"', home)) == 1
    assert len(re.findall(r'<span class="thumb thumb-icon"', home)) == 1
    assert re.search(r'<use href="/assets/img/world\.svg\?v=[0-9a-f]{10}#w"/>', home)
    page = (out / "story/2026-10-24/story-1.html").read_text("utf-8")
    assert 'class="story-map"' in page and "Kyiv, Ukraine</figcaption>" in page
    assert 'class="map-dot"' in page
    assert 'class="story-map"' not in (out / "story/2026-10-24/story-2.html").read_text("utf-8")
