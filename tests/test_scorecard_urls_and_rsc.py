"""Regression tests: CricHeroes /summary links and App Router (no __NEXT_DATA__) pages."""
import pathlib
import sys

from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import fetcher  # noqa: E402

BASE = "https://cricheroes.in/scorecard/28014702/individual/game-changers-vs-super-game-changers"
FIXTURE = ROOT / "tests" / "fixtures" / "scorecard_rsc_28014702.html"


def test_summary_link_is_replaced_not_extended():
    assert fetcher._match_tab_url(BASE + "/summary", "commentary") == BASE + "/commentary"
    assert fetcher._match_tab_url(BASE + "/summary", "scorecard") == BASE + "/scorecard"


def test_tab_url_is_idempotent_and_handles_bare_and_query_urls():
    for u in (BASE, BASE + "/", BASE + "/scorecard", BASE + "/summary/", BASE + "/summary?x=1"):
        assert fetcher._match_tab_url(u, "scorecard") == BASE + "/scorecard"


def test_rsc_page_is_parsed_for_both_teams():
    nd = fetcher._extract_next_data(BeautifulSoup(FIXTURE.read_text(encoding="utf-8"), "lxml"))
    assert nd and len(nd["props"]["pageProps"]["scorecard"]) == 2
    names = {}
    for team_id in ("2182806", "2182828"):
        fetcher.TEAM_ID = team_id
        res = fetcher._parse_scorecard_from_next_data(nd, "28014702", {})
        assert res["overs"] == 6 and res["match_date"] == "2026-11-01"
        names[team_id] = {s["player_name"] for s in res["player_stats"]}
    assert len(names["2182806"]) == 11 and len(names["2182828"]) == 9
    assert not names["2182806"] & names["2182828"]
