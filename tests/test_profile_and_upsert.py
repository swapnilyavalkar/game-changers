"""Regression tests: App Router player profiles, and blank profile values never erasing stored ones."""
import json
import pathlib
import sys
import tempfile

from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import player_fetcher  # noqa: E402


def _rsc_page(player_info: dict) -> str:
    """A minimal App Router page: props arrive as self.__next_f.push() chunks."""
    payload = json.dumps({"playerInfo": {"status": True, "data": player_info}, "userAgent": "x"})
    chunk = "9:" + payload
    return (
        "<html><head></head><body><script>self.__next_f.push(["
        + json.dumps(1) + "," + json.dumps(chunk) + "])</script></body></html>"
    )


def test_profile_without_next_data_script_still_yields_styles():
    html = _rsc_page({
        "player_id": 1, "name": "Test Player", "batting_hand": "RHB",
        "bowling_style": "Right-arm fast", "total_matches": 10, "total_runs": 100,
        "total_wickets": 5, "player_skill": "ALL",
    })
    nd = player_fetcher._extract_next_data(html)
    assert nd is not None
    res = player_fetcher._parse_next_data(nd, "1")
    assert (res["batting_style"], res["bowling_style"], res["role"]) == ("RHB", "Right-arm fast", "all_rounder")


def test_page_without_player_data_returns_none():
    assert player_fetcher._extract_next_data("<html><body>nothing here</body></html>") is None


def test_blank_profile_values_do_not_overwrite_stored_ones():
    import db
    db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    db.init_db()
    base = {"cricheroes_id": "T1", "name": "T", "role": "batsman", "is_game_changers": 1}
    db.upsert_player({**base, "batting_style": "RHB", "bowling_style": "Right-arm fast", "photo_url": "http://x/p.jpg"})
    db.upsert_player({**base, "batting_style": None, "bowling_style": "", "photo_url": None})
    row = [p for p in db.get_all_players() if p["cricheroes_id"] == "T1"][0]
    assert (row["batting_style"], row["bowling_style"], row["photo_url"]) == ("RHB", "Right-arm fast", "http://x/p.jpg")
    db.upsert_player({**base, "batting_style": "LHB"})
    row = [p for p in db.get_all_players() if p["cricheroes_id"] == "T1"][0]
    assert row["batting_style"] == "LHB"
