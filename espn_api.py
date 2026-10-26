"""
espn_api.py - Keyless live-football provider (no account, no API key).

Reads ESPN's public scoreboard JSON:
    https://site.api.espn.com/apis/site/v2/sports/soccer/<league-slug>/scoreboard

and converts each match into the same shape the match engine already understands
(API-Football style), so match_engine.py needs almost no changes.

IMPORTANT - read this:
* This is an UNOFFICIAL, undocumented endpoint. ESPN can change or block it at any
  time and gives no SLA. Run `python check_espn.py` once to confirm it works for your
  leagues, and keep an eye on the logs.
* Goals/red cards only appear in the feed once ESPN has attached the player to the
  event, so alerts can trail the real goal by a short time.
* Event IDs are derived from event order ("goal #2 of this match"), which keeps keys
  stable even if ESPN later corrects a minute or a scorer's name.
* A goal ruled out by VAR is simply removed by ESPN; the bot cannot announce that.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import aiohttp

from match_engine import FINISHED_STATUSES, LIVE_STATUSES, APIError

log = logging.getLogger("espn_api")

# Since Aug 2026 ESPN's site.api answers 403 to browser-like User-Agents ("Mozilla/5.0 ...").
# It accepts an identifying UA or none at all, so we try these routes in order and remember
# the first one that works.  (UA, base URL)
IDENT_UA = "XRPArmyBot/1.0 (+https://xrpfootball.com)"
ROUTES = [
    ("https://site.api.espn.com/apis/site/v2/sports/soccer", IDENT_UA),
    ("https://site.api.espn.com/apis/site/v2/sports/soccer", None),       # no User-Agent at all
    ("https://site.web.api.espn.com/apis/site/v2/sports/soccer", None),
]

STATUS_MAP = {
    "STATUS_SCHEDULED": "NS",
    "STATUS_FIRST_HALF": "1H",
    "STATUS_HALFTIME": "HT",
    "STATUS_SECOND_HALF": "2H",
    "STATUS_END_OF_REGULATION": "BT",
    "STATUS_FULL_TIME": "FT",
    "STATUS_FINAL": "FT",
    "STATUS_FINAL_AET": "AET",
    "STATUS_FINAL_PEN": "PEN",
    "STATUS_POSTPONED": "PST",
    "STATUS_CANCELED": "CANC",
    "STATUS_CANCELLED": "CANC",
    "STATUS_ABANDONED": "ABD",
    "STATUS_SUSPENDED": "SUSP",
}


def _short_status(status: dict) -> str:
    t = status.get("type") or {}
    name = (t.get("name") or "").upper()
    state = t.get("state")
    if name in STATUS_MAP:
        return STATUS_MAP[name]
    if "EXTRA" in name or "OVERTIME" in name:
        return "ET"
    if "SHOOTOUT" in name or "PENALT" in name:
        return "P"
    if state == "in":
        return "LIVE"
    if state == "post":
        return "FT"
    return "NS"


def _parse_clock(display: str | None) -> tuple[int | None, int | None]:
    """'67'' -> (67, None);  '45'+2'' -> (45, 2);  '90'+5'' -> (90, 5)."""
    nums = re.findall(r"\d+", display or "")
    elapsed = int(nums[0]) if nums else None
    extra = int(nums[1]) if len(nums) > 1 else None
    return elapsed, extra


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize(event: dict, comp: dict, slug: str, league_name: str) -> dict:
    """Convert one ESPN event into an API-Football-shaped fixture dict."""
    competitors = comp["competitors"]
    home = next(c for c in competitors if c.get("homeAway") == "home")
    away = next(c for c in competitors if c.get("homeAway") == "away")

    def tname(c: dict) -> str:
        t = c["team"]
        return t.get("displayName") or t.get("name") or t.get("shortDisplayName") or "?"

    team_names = {str(c["team"]["id"]): tname(c) for c in competitors}
    status = comp.get("status") or event.get("status") or {}
    short = _short_status(status)
    elapsed, _ = _parse_clock(status.get("displayClock"))
    if short == "NS":
        elapsed = None

    events: list[dict] = []
    goal_n = 0
    for d in comp.get("details") or []:
        type_text = ((d.get("type") or {}).get("text") or "").lower()
        if "shootout" in type_text:
            continue
        athletes = d.get("athletesInvolved") or []
        a0 = athletes[0] if athletes else {}
        player_name = a0.get("displayName") or a0.get("shortName")
        player_id = a0.get("id")
        team_id = str((d.get("team") or {}).get("id", ""))
        ev_elapsed, ev_extra = _parse_clock((d.get("clock") or {}).get("displayValue"))
        base = {
            "time": {"elapsed": ev_elapsed, "extra": ev_extra},
            "player": {"id": player_id, "name": player_name},
            "team": {"id": team_id, "name": team_names.get(team_id, "")},
        }
        if d.get("scoringPlay"):
            goal_n += 1
            detail = "Own Goal" if d.get("ownGoal") else "Penalty" if d.get("penaltyKick") else "Normal Goal"
            events.append({**base, "type": "Goal", "detail": detail, "uid": f"goal-{goal_n}-{team_id}"})
        elif d.get("redCard"):
            detail = "Second Yellow card" if "second yellow" in type_text else "Red Card"
            events.append(
                {**base, "type": "Card", "detail": detail, "uid": f"red-{player_id or player_name}-{team_id}"}
            )
        # yellow cards, substitutions, missed penalties: intentionally ignored

    ts = int(datetime.fromisoformat(event["date"].replace("Z", "+00:00")).timestamp())
    return {
        "fixture": {
            "id": int(event["id"]),
            "timestamp": ts,
            "status": {"short": short, "elapsed": elapsed},
        },
        "league": {"id": slug, "name": league_name},
        "teams": {"home": {"id": home["team"]["id"], "name": tname(home)},
                  "away": {"id": away["team"]["id"], "name": tname(away)}},
        "goals": {"home": _to_int(home.get("score")), "away": _to_int(away.get("score"))},
        "events": events,
    }


class ESPNFootballAPI:
    """Drop-in replacement for FootballAPI; `leagues` are ESPN slugs (eng.1, esp.1, ...)."""

    def __init__(self, leagues: list[str], proxy: str | None = None):
        self.leagues = leagues
        self.proxy = proxy          # optional ESPN_PROXY_URL if your host's IP range is blocked
        self._good_route = 0
        self._session: aiohttp.ClientSession | None = None
        self._league_of: dict[int, str] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, url: str, params: dict, ua: str | None, retries: int = 3) -> tuple[int, dict | None]:
        """One route. Returns (status, json) for 200/403/404; raises APIError for real failures."""
        session = await self._get_session()
        headers = {"Accept": "application/json"}
        if ua:
            headers["User-Agent"] = ua
        last: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                async with session.get(
                    url, params=params, headers=headers, proxy=self.proxy,
                    skip_auto_headers=None if ua else ["User-Agent"],
                ) as resp:
                    if resp.status in (403, 404):
                        return resp.status, None
                    if resp.status == 429 or resp.status >= 500:
                        last = APIError(f"HTTP {resp.status}")
                        await asyncio.sleep(2 ** attempt)
                        continue
                    if resp.status >= 400:
                        raise APIError(f"HTTP {resp.status}")
                    return 200, await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last = exc
                await asyncio.sleep(2 ** attempt)
        raise APIError(f"ESPN request failed after {retries} attempts: {last}")

    async def _fetch(self, slug: str, params: dict, retries: int = 3) -> dict:
        saw_403 = saw_404 = False
        for offset in range(len(ROUTES)):
            idx = (self._good_route + offset) % len(ROUTES)
            base, ua = ROUTES[idx]
            status, data = await self._request(f"{base}/{slug}/scoreboard", params, ua, retries)
            if status == 200:
                if idx != self._good_route:
                    log.info("ESPN route %d works - using it from now on", idx)
                self._good_route = idx
                return data or {}
            saw_403 |= status == 403
            saw_404 |= status == 404
        if saw_403:
            raise APIError(
                f"HTTP 403 for {slug}: ESPN is blocking this server on every route. "
                "Try ESPN_PROXY_URL, host the bot elsewhere, or see README 'ESPN blocked'."
            )
        raise APIError(f"ESPN does not know league slug '{slug}' (HTTP 404)")

    async def _scoreboard(self, slug: str, dates: str | None = None) -> list[dict]:
        params = {"limit": "100"}
        if dates:
            params["dates"] = dates
        data = await self._fetch(slug, params)
        league_name = ((data.get("leagues") or [{}])[0]).get("name") or slug
        out = []
        for event in data.get("events") or []:
            comps = event.get("competitions") or []
            if not comps:
                continue
            try:
                item = normalize(event, comps[0], slug, league_name)
            except (KeyError, StopIteration, ValueError, TypeError) as exc:
                log.warning("Skipping unparseable ESPN event %s: %r", event.get("id"), exc)
                continue
            self._league_of[item["fixture"]["id"]] = slug
            out.append(item)
        return out

    async def _all_leagues(self, dates: str | None) -> list[dict]:
        results = await asyncio.gather(
            *(self._scoreboard(slug, dates) for slug in self.leagues), return_exceptions=True
        )
        items: list[dict] = []
        errors = []
        for slug, res in zip(self.leagues, results):
            if isinstance(res, Exception):
                errors.append(f"{slug}: {res}")
            else:
                items.extend(res)
        if errors and len(errors) == len(self.leagues):
            raise APIError("; ".join(errors))
        for e in errors:
            log.warning("ESPN partial failure - %s", e)
        return items

    async def _by_id(self, fid: int) -> list[dict]:
        slugs = [self._league_of[fid]] if fid in self._league_of else self.leagues
        today = datetime.now(timezone.utc).date()
        dates = f"{(today - timedelta(days=1)):%Y%m%d}-{today:%Y%m%d}"
        for slug in slugs:
            for item in await self._scoreboard(slug, dates):
                if item["fixture"]["id"] == fid:
                    return [item]
        return []

    async def get(self, endpoint: str, params: dict | None = None, retries: int = 3) -> list[dict]:
        """Same call signature the engine uses with API-Football: get('fixtures', {...})."""
        params = params or {}
        if endpoint != "fixtures":
            raise APIError(f"Unsupported endpoint for ESPN provider: {endpoint}")
        if "id" in params:
            return await self._by_id(int(params["id"]))
        if "date" in params:
            return await self._all_leagues(params["date"].replace("-", ""))
        if "live" in params:
            # Today's matches that are in progress OR just finished (so FT is posted).
            items = await self._all_leagues(None)
            return [i for i in items if i["fixture"]["status"]["short"] in (LIVE_STATUSES | FINISHED_STATUSES)]
        return []
