from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from digest.config import Config, ConfigError, Settings, load_config
from digest.publish.render import build_site
from digest.regions import RegionMatcher

from .conftest import CONFIG_DIR
from .test_publish import NOW, story


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config(CONFIG_DIR)


@pytest.fixture(scope="module")
def matcher(config: Config) -> RegionMatcher:
    return RegionMatcher(config.regions)


@pytest.mark.parametrize(
    ("text", "regions"),
    [
        ("Thousands rally in Tel Aviv against Netanyahu", ("middle_east",)),
        ("Gunmen kill 10 in eastern Nigeria", ("africa",)),
        ("Papua New Guinea quake", ("oceania",)),  # longest name first, not Guinea
        ("New Mexico wildfire spreads", ("north_america",)),  # not Mexico
        ("Kraftig jordbävning i södra Japan", ("asia",)),  # Nordic sources count too
        ("Iran\N{RIGHT SINGLE QUOTATION MARK}s leaders meet in Tehran", ("middle_east",)),
        ("Trump says talks with Iran went well", ("north_america", "middle_east")),
        ("Scientists find a new exoplanet", ()),
        ("Turkey prices rise before the holiday", ()),  # lower-case words never match
    ],
)
def test_regions_from_place_names(
    matcher: RegionMatcher, text: str, regions: tuple[str, ...]
) -> None:
    got = matcher.regions_of([text.replace("Turkey prices", "turkey prices")])
    assert got == regions


def test_a_third_region_needs_half_the_mentions(matcher: RegionMatcher) -> None:
    texts = ["Israel and Gaza talks in Cairo", "Israeli strike", "UN in Geneva"]
    assert matcher.regions_of(texts) == ("middle_east",)  # 4 against 1


def test_a_name_may_be_in_only_one_region(tmp_path: Path) -> None:
    import shutil

    import yaml

    shutil.copytree(CONFIG_DIR, tmp_path / "config")
    path = tmp_path / "config" / "regions.yaml"
    regions = yaml.safe_load(path.read_text("utf-8"))
    regions[1]["places"].append(regions[0]["places"][0])
    path.write_text(yaml.safe_dump(regions), encoding="utf-8")
    with pytest.raises(ConfigError, match="is in europe and middle_east"):
        load_config(tmp_path / "config")


def test_region_box_pages_and_sitemap(tmp_path: Path, config: Config) -> None:
    items = [
        dataclasses.replace(story(1, headline="Gaza talks resume"), regions=("middle_east",)),
        dataclasses.replace(story(2, headline="Kenya votes"), regions=("africa",)),
        story(3),
    ]
    out = tmp_path / "site"
    build_site(out, items, settings=Settings(), config=config, now=NOW)
    home = (out / "index.html").read_text("utf-8")
    assert home.count("data-region-box") == 2  # wide and phone layouts
    assert 'href="/r/middle-east/"' in home  # works without JavaScript
    assert "Gaza talks resume" in home and "data-weather hidden" in home
    zones = json.loads(home.split('<script type="application/json" id="regions">')[1].split("<")[0])
    assert "Europe/" in zones["europe"] and "Africa/Cairo" in zones["middle_east"]
    page = (out / "r/africa/index.html").read_text("utf-8")
    assert "Kenya votes" in page and "Gaza talks resume" not in page
    assert (out / "r/oceania/index.html").is_file()  # empty regions get a page too
    assert "/r/latin-america/" in (out / "sitemap.xml").read_text("utf-8")
    privacy = (out / "privacy.html").read_text("utf-8")
    assert "Open-Meteo" in privacy
