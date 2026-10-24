from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import Engine, select

from digest import db
from digest.config import Config, TemplateSettings, load_config
from digest.datastories import credit_line, parse_data_feed
from digest.db.schema import items
from digest.ingest.feeds import FeedParseError
from digest.ingest.stage import DataContext, run_ingest
from digest.timeutil import iso
from tests.fake_web import FakeWeb
from tests.test_fetcher import make_fetcher

T0 = dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.UTC)
ZONE = ZoneInfo("Europe/Stockholm")
URL = "https://data.weather.example/warning.json"
PAGE = "https://weather.example/warnings"

DATA_SOURCE = """
- id: weather
  name: SMHI
  homepage: https://weather.example/warnings
  feeds:
    - {url: "https://data.weather.example/warning.json", kind: data_api}
  lang: sv
  country: SE
  category_hints: [climate]
  usage_policy: open_license
  template: smhi_warning
  official: true
  terms_checked_on: 2026-10-01
  enabled: true
"""


def area(
    area_id: int,
    level: str,
    name_en: str,
    name_sv: str,
    *,
    counties: tuple[str, ...] = (),
    start: str = "2026-10-25T09:00:00.000Z",
    end: str | None = "2026-10-25T16:00:00.000Z",
    detail: str | None = None,
) -> dict[str, object]:
    """One warning area in the shape of SMHI's warning.json (values made up)."""
    out: dict[str, object] = {
        "id": area_id,
        "approximateStart": start,
        "published": "2026-10-24T07:30:00.000Z",
        "areaName": {"sv": name_sv, "en": name_en},
        "warningLevel": {"sv": level.title(), "en": level.title(), "code": level},
        "affectedAreas": [
            {"id": i, "sv": c + " län", "en": c + " County"} for i, c in enumerate(counties)
        ],
        "descriptions": [{"title": {"en": "Description"}, "text": {"en": "SMHI-WORDING-CANARY"}}],
    }
    if end:
        out["approximateEnd"] = end
    if detail:
        out["eventDescription"] = {"sv": detail, "en": detail, "code": "X"}
    return out


def warning(wid: int, code: str, en: str, sv: str, *areas: dict[str, object]) -> dict[str, object]:
    return {
        "id": wid,
        "event": {"sv": sv, "en": en, "code": code},
        "descriptions": [],
        "warningAreas": list(areas),
    }


FIXTURE = [
    warning(
        1,
        "WIND",
        "Wind",
        "Vind",
        area(10, "YELLOW", "Western Götaland", "Västra Götaland", counties=("Halland", "Skåne")),
        area(11, "ORANGE", "Coast of Skåne", "Skånekusten", counties=("Skåne",)),
    ),
    warning(
        2, "WIND_SEA", "Wind at sea", "Vind till sjöss",
        area(20, "YELLOW", "Kattegat", "Kattegatt"),
    ),
    warning(
        3, "WATER_SHORTAGE", "Risk for water shortage", "Risk för vattenbrist",
        area(30, "MESSAGE", "Gotland", "Gotland"),
    ),
    warning(
        4,
        "RAIN",
        "Rain",
        "Regn",
        *(area(40 + i, "YELLOW", f"Area {i}", f"Område {i}", detail="Heavy rain")
          for i in range(5)),
    ),
]  # fmt: skip


def parse(data: object, lang: str = "en", **settings: object) -> list:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    return parse_data_feed(
        "smhi_warning",
        json.dumps(data).encode(),
        lang=lang,
        link=PAGE,
        zone=ZONE,
        settings=TemplateSettings.model_validate(settings),
    )


def test_warning_text_in_english() -> None:
    wind, rain = parse(FIXTURE)
    assert wind.guid == "smhi-warning:1:ORANGE"
    assert wind.title == "SMHI issues orange warning for wind in Coast of Skåne"
    assert wind.summary == (
        "SMHI has issued an orange warning for wind in Coast of Skåne. "
        "Counties affected: Skåne. It applies from 25 Oct 10:00 until 25 Oct 17:00."
    )
    assert rain.title == "SMHI issues yellow warning for rain in 5 areas"
    assert "for rain (heavy rain) in Area 0, Area 1, Area 2 and 2 more areas." in rain.summary
    assert all("CANARY" not in (e.summary or "") for e in (wind, rain))  # never SMHI's prose
    assert wind.link == PAGE and wind.dedupe_key == wind.guid
    # SMHI's own values travel with the entry, unchanged, for the site's warnings box.
    assert wind.data is not None
    assert {k: wind.data[k] for k in ("template", "warning_id", "level", "event", "areas")} == {
        "template": "smhi_warning",
        "warning_id": "1",
        "level": "orange",
        "event": "Wind",
        "areas": ["Coast of Skåne"],
    }
    assert wind.data["starts_at"] < wind.data["ends_at"]


def test_warning_text_in_swedish() -> None:
    wind, _ = parse(FIXTURE, "sv")
    assert wind.title == "SMHI utfärdar orange varning för vind i Skånekusten"
    assert wind.summary == (
        "SMHI har utfärdat en orange varning för vind i Skånekusten. "
        "Berörda län: Skåne län. Den gäller från 25 okt kl. 10.00 till 25 okt kl. 17.00."
    )


def test_same_data_same_text() -> None:
    assert parse(FIXTURE) == parse(json.loads(json.dumps(FIXTURE)))


def test_thresholds_and_excluded_events() -> None:
    assert [e.guid for e in parse(FIXTURE, smhi_min_warning_level="orange")] == [
        "smhi-warning:1:ORANGE"
    ]
    guids = [e.guid for e in parse(FIXTURE, smhi_exclude_events=[])]
    assert "smhi-warning:2:YELLOW" in guids  # marine warnings when not excluded
    assert not any(":3:" in g for g in guids)  # messages are below yellow


def test_missing_end_time_and_bad_input() -> None:
    (e,) = parse([warning(5, "SNOW", "Snow", "Snö", area(50, "RED", "Kiruna", "Kiruna", end=None))])
    assert e.summary is not None and e.summary.endswith("in Kiruna. It applies from 25 Oct 10:00.")
    with pytest.raises(FeedParseError):
        parse_data_feed(
            "smhi_warning", b"<html>", lang="en", link=PAGE, zone=ZONE, settings=TemplateSettings()
        )
    with pytest.raises(FeedParseError):
        parse_data_feed("nope", b"[]", lang="en", link=PAGE, zone=ZONE, settings=TemplateSettings())


def test_credit_lines() -> None:
    assert credit_line("smhi_warning", "en") == "Source: SMHI weather warnings."
    assert credit_line("smhi_warning", "sv") == "Källa: SMHI:s vädervarningar."


def test_unknown_template_fails_config(ingest_config_dir: Path) -> None:
    from digest.config import ConfigError

    (ingest_config_dir / "sources.yaml").write_text(
        DATA_SOURCE.replace("smhi_warning", "made_up"), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="unknown template"):
        load_config(ingest_config_dir)


# --------------------------------------------------------------------------- through ingest


@pytest.fixture
def config(ingest_config_dir: Path) -> Config:
    (ingest_config_dir / "sources.yaml").write_text(DATA_SOURCE, encoding="utf-8")
    return load_config(ingest_config_dir)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    path = tmp_path / "state.sqlite"
    db.migrate(path)
    return db.make_engine(path)


class Budget:
    def remaining(self) -> float:
        return 600.0


def test_data_feed_is_ingested_as_items(engine: Engine, config: Config) -> None:
    web = FakeWeb()
    web.route("https://data.weather.example/robots.txt", httpx.Response(404))
    web.route(URL, httpx.Response(200, content=json.dumps(FIXTURE).encode()))
    stats = run_ingest(
        engine,
        config,
        make_fetcher(web, config, now=T0),
        Budget(),
        retention_hours=72,
        now=lambda: T0,
        data=DataContext("en", ZONE, True),
    )
    assert stats.items_new == 2
    with engine.connect() as conn:
        rows = conn.execute(select(items).order_by(items.c.id)).all()
    assert {r.usage_policy for r in rows} == {"open_license"}
    assert {r.url for r in rows} == {PAGE}
    assert rows[0].lang == "en"
    # A second poll with the same data adds nothing.
    again = run_ingest(
        engine,
        config,
        make_fetcher(web, config, now=T0 + dt.timedelta(hours=1)),
        Budget(),
        retention_hours=72,
        now=lambda: T0 + dt.timedelta(hours=1),
        data=DataContext("en", ZONE, True),
    )
    assert again.items_new == 0 and again.duplicates == 2
    # Still listed by SMHI: each poll records it, so the warnings box keeps it.
    with engine.connect() as conn:
        seen = {json.loads(r.data)["seen_at"] for r in conn.execute(select(items.c.data))}
    assert seen == {iso(T0 + dt.timedelta(hours=1))}


def test_data_feed_not_polled_when_templates_off(engine: Engine, config: Config) -> None:
    web = FakeWeb()
    stats = run_ingest(
        engine,
        config,
        make_fetcher(web, config, now=T0),
        Budget(),
        retention_hours=72,
        now=lambda: T0,
        data=DataContext("en", ZONE, False),
    )
    assert stats.feeds_due == 0 and web.requests == []
