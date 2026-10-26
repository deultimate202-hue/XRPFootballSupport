"""
match_engine.py - Automated live-match engine for XRP ARMY Super Football Fans.

Responsibilities
----------------
* Talk to API-Football v3 (RapidAPI *or* direct api-sports.io).
* Poll live fixtures every POLL_INTERVAL seconds - but only while matches are
  actually scheduled/in progress, to protect your daily API quota.
* Detect goals, red cards, VAR-disallowed goals, half-time and full-time.
* Post pre-match previews shortly before kickoff.
* Guarantee each event is posted EXACTLY ONCE (persistent dedup ledger).

The engine is decoupled from Telegram: it only needs an async `post(text) -> bool`
callable that sends an HTML-formatted message to the target group.

API-Football note: events carry no unique ID, so a deterministic key is built from
fixture + minute + extra-time + type + detail + player + team (+ an occurrence
counter for the rare case of two identical events in the same minute).
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import aiohttp

from storage import JsonStore

log = logging.getLogger("match_engine")

PORTAL_URL = "https://XRPFootball.com"
COMMUNITY_URL = "https://www.netmark.ai/xrp"

# API-Football fixture status codes
LIVE_STATUSES = {"1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "LIVE"}
FINISHED_STATUSES = {"FT", "AET", "PEN"}
DEAD_STATUSES = {"PST", "CANC", "ABD", "AWD", "WO"}
NOT_STARTED_STATUSES = {"TBD", "NS"}

PostFn = Callable[[str], Awaitable[bool]]


# --------------------------------------------------------------------------- #
# Message formatting (HTML parse mode)
# --------------------------------------------------------------------------- #
def _e(value: Any) -> str:
    return html.escape(str(value), quote=False)


def fmt_minute(elapsed: int | None, extra: int | None) -> str:
    if elapsed is None:
        return "?"
    return f"{elapsed}+{extra}" if extra else str(elapsed)


def goal_message(home: str, away: str, hg: int, ag: int, minute: str, player: str, tag: str = "") -> str:
    return (
        "⚽ <b>GOAL ALERT!</b> ⚽\n"
        f"🏆 {_e(home)} {hg} - {ag} {_e(away)}\n"
        f"⏱️ Minute: {minute}'\n"
        f"👤 Goal Scorer: {_e(player)}{tag}\n"
        f'<a href="{PORTAL_URL}">🌐 Match Odds &amp; Discussion on XRPFootball.com</a>'
    )


def sample_goal_message() -> str:
    """Used by the admin-only /testalert command to verify posting works."""
    return "🧪 <i>Test alert</i>\n" + goal_message("Home FC", "Away United", 1, 0, "23", "Sample Player")


def red_card_message(ctx: dict, ev: dict) -> str:
    t = ev.get("time") or {}
    player = (ev.get("player") or {}).get("name") or "Unknown player"
    team = (ev.get("team") or {}).get("name") or ""
    reason = "Second yellow card" if "second yellow" in (ev.get("detail") or "").lower() else "Straight red card"
    return (
        "🟥 <b>RED CARD!</b> 🟥\n"
        f"🏆 {_e(ctx['home'])} {ctx['hg']} - {ctx['ag']} {_e(ctx['away'])}\n"
        f"⏱️ Minute: {fmt_minute(t.get('elapsed'), t.get('extra'))}'\n"
        f"👤 {_e(player)} ({_e(team)}) - {reason}\n"
        "💬 Was it the right call? Tell us below 👇"
    )


def var_message(ctx: dict, ev: dict) -> str:
    t = ev.get("time") or {}
    team = (ev.get("team") or {}).get("name") or ""
    return (
        "📺 <b>VAR: GOAL DISALLOWED</b>\n"
        f"🏆 {_e(ctx['home'])} {ctx['hg']} - {ctx['ag']} {_e(ctx['away'])}\n"
        f"⏱️ Minute: {fmt_minute(t.get('elapsed'), t.get('extra'))}' ({_e(team)})\n"
        "The score above is the updated scoreline."
    )


def halftime_message(ctx: dict) -> str:
    return (
        "⏸️ <b>HALF-TIME</b>\n"
        f"🏆 {_e(ctx['league'])}\n"
        f"{_e(ctx['home'])} {ctx['hg']} - {ctx['ag']} {_e(ctx['away'])}\n"
        "💬 What have you made of the first half? Drop your thoughts below 👇\n"
        f'<a href="{COMMUNITY_URL}">🌐 Community portal</a>'
    )


def fulltime_message(ctx: dict) -> str:
    return (
        "🏁 <b>FULL-TIME</b>\n"
        f"🏆 {_e(ctx['league'])}\n"
        f"{_e(ctx['home'])} {ctx['hg']} - {ctx['ag']} {_e(ctx['away'])}\n"
        "💬 What did you think of that one? Share your thoughts below 👇\n"
        f'<a href="{COMMUNITY_URL}">🌐 Community portal</a>'
    )


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #
class APIError(Exception):
    pass


class _NotReady(Exception):
    pass


class FootballAPI:
    """Async API-Football v3 client. provider = 'rapidapi' | 'direct'."""

    def __init__(self, api_key: str, provider: str = "rapidapi"):
        if provider == "direct":
            self.base = "https://v3.football.api-sports.io"
            self.headers = {"x-apisports-key": api_key}
        else:
            self.base = "https://api-football-v1.p.rapidapi.com/v3"
            self.headers = {
                "X-RapidAPI-Key": api_key,
                "X-RapidAPI-Host": "api-football-v1.p.rapidapi.com",
            }
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def get(self, endpoint: str, params: dict | None = None, retries: int = 3) -> list[dict]:
        session = await self._get_session()
        url = f"{self.base}/{endpoint.lstrip('/')}"
        last_exc: Exception | None = None

        for attempt in range(1, retries + 1):
            try:
                async with session.get(url, params=params, headers=self.headers) as resp:
                    remaining = resp.headers.get("x-ratelimit-requests-remaining")
                    if remaining is not None and remaining.isdigit() and int(remaining) < 10:
                        log.warning("API-Football daily quota almost exhausted: %s requests left", remaining)

                    if resp.status == 429:
                        wait = min(int(resp.headers.get("Retry-After", 5 * attempt)), 60)
                        log.warning("Rate limited (429); sleeping %ss", wait)
                        last_exc = APIError("rate limited")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status >= 500:
                        last_exc = APIError(f"HTTP {resp.status}")
                        await asyncio.sleep(2 ** attempt)
                        continue
                    if resp.status >= 400:
                        raise APIError(f"HTTP {resp.status}: {(await resp.text())[:200]}")

                    body = await resp.json(content_type=None)
                    # API-Football reports quota/plan problems as HTTP 200 + "errors"
                    if body.get("errors"):
                        raise APIError(f"API errors: {body['errors']}")
                    return body.get("response") or []
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                await asyncio.sleep(2 ** attempt)

        raise APIError(f"{endpoint} failed after {retries} attempts: {last_exc}")


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class MatchEngine:
    def __init__(
        self,
        api: FootballAPI,
        post: PostFn,
        league_ids: list,
        store: JsonStore,
        timezone_name: str = "UTC",
        poll_interval: int = 60,
        preview_minutes: int = 60,
        schedule_refresh_minutes: int = 120,
        always_poll: bool = False,
        is_ready: Callable[[], bool] | None = None,
    ):
        self.api = api
        self.post = post
        self.league_ids = [str(x) for x in league_ids]  # ints (API-Football) or slugs (ESPN)
        self.store = store
        self.tz = ZoneInfo(timezone_name)
        self.poll_interval = poll_interval
        self.preview_minutes = preview_minutes
        self.schedule_refresh_s = schedule_refresh_minutes * 60
        self.always_poll = always_poll
        self.is_ready = is_ready  # e.g. 'has the public group been set yet?'

        # fixture_id -> snapshot of the match (the spec's `live_matches_cache`)
        self.live_matches_cache: dict[int, dict] = {}
        # fixture_id -> {ts, home, away, league, status} for today/tomorrow
        self._schedule: dict[int, dict] = {}
        self._schedule_refreshed_at = 0.0
        self._last_prune = 0.0
        self._stop = asyncio.Event()

    # ------------------------------ lifecycle ------------------------------ #
    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info(
            "Match engine started (leagues=%s, interval=%ss)",
            self.league_ids or "ALL", self.poll_interval,
        )
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                if self.is_ready is not None and not self.is_ready():
                    raise _NotReady
                await self._refresh_schedule()
                await self._send_previews()
                if self._should_poll():
                    await self._poll_live()
                self._prune_ledger()
            except asyncio.CancelledError:
                raise
            except _NotReady:
                pass  # public group not set yet - stay idle, no API calls, nothing posted
            except Exception:  # never let the worker die
                log.exception("Engine tick failed; will retry next cycle")

            delay = max(5.0, self.poll_interval - (time.monotonic() - started))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        log.info("Match engine stopped")

    # ------------------------------ ledger -------------------------------- #
    @property
    def _posted(self) -> dict[str, int]:
        return self.store.data["posted"]

    def _mark(self, key: str) -> None:
        self._posted[key] = int(time.time())
        self.store.save()

    def _prune_ledger(self) -> None:
        if time.time() - self._last_prune < 3600:
            return
        self._last_prune = time.time()
        cutoff = time.time() - 4 * 86400
        stale = [k for k, ts in self._posted.items() if ts < cutoff]
        for k in stale:
            del self._posted[k]
        if stale:
            self.store.save()
            log.info("Pruned %d old ledger entries", len(stale))

    async def _post_once(self, key: str, text: str, silent: bool = False) -> None:
        if key in self._posted:
            return
        if silent or await self.post(text):
            self._mark(key)

    # ------------------------------ schedule ------------------------------ #
    def _wanted(self, item: dict) -> bool:
        return not self.league_ids or str(item["league"]["id"]) in self.league_ids

    async def _refresh_schedule(self) -> None:
        if time.time() - self._schedule_refreshed_at < self.schedule_refresh_s:
            return
        now = datetime.now(timezone.utc)
        dates = [now.date(), (now + timedelta(days=1)).date()]
        if now.hour < 3:  # catch late kickoffs from "yesterday" UTC
            dates.insert(0, (now - timedelta(days=1)).date())
        try:
            fixtures: dict[int, dict] = {}
            for d in dates:
                for it in await self.api.get("fixtures", {"date": d.isoformat()}):
                    if not self._wanted(it):
                        continue
                    fx = it["fixture"]
                    fixtures[fx["id"]] = {
                        "ts": fx["timestamp"],
                        "home": it["teams"]["home"]["name"],
                        "away": it["teams"]["away"]["name"],
                        "league": it["league"]["name"],
                        "status": fx["status"]["short"],
                    }
            self._schedule = fixtures
            self._schedule_refreshed_at = time.time()
            log.info("Schedule refreshed: %d monitored fixtures", len(fixtures))
        except APIError as exc:
            log.error("Schedule refresh failed: %s", exc)
            # retry in ~5 minutes instead of hammering the API every tick
            self._schedule_refreshed_at = time.time() - self.schedule_refresh_s + 300

    def _should_poll(self) -> bool:
        if self.always_poll or self.live_matches_cache:
            return True
        now = time.time()
        for f in self._schedule.values():
            if f["status"] in DEAD_STATUSES | FINISHED_STATUSES:
                continue
            if f["ts"] - 600 <= now <= f["ts"] + 3 * 3600:
                return True
        return False

    # ------------------------------ previews ------------------------------ #
    async def _send_previews(self) -> None:
        now = time.time()
        for fid, f in self._schedule.items():
            if f["status"] not in NOT_STARTED_STATUSES:
                continue
            delta = f["ts"] - now
            if not (0 < delta <= self.preview_minutes * 60):
                continue
            key = f"{fid}:preview"
            if key in self._posted:
                continue
            kickoff = datetime.fromtimestamp(f["ts"], self.tz)
            text = (
                "📣 <b>MATCH PREVIEW</b>\n"
                f"🏆 {_e(f['league'])}\n"
                f"⚔️ <b>{_e(f['home'])}</b> vs <b>{_e(f['away'])}</b>\n"
                f"🕒 Kickoff: {kickoff.strftime('%H:%M %Z')} (in ~{int(delta // 60)} min)\n"
                "💬 Who are you backing today? Drop your prediction below 👇\n"
                f'<a href="{PORTAL_URL}">🌐 Join the discussion on XRPFootball.com</a>'
            )
            if await self.post(text):
                self._mark(key)

    # ------------------------------ live polling -------------------------- #
    async def _poll_live(self) -> None:
        params = {"live": "-".join(map(str, self.league_ids)) if self.league_ids else "all"}
        items = await self.api.get("fixtures", params)

        seen: set[int] = set()
        for item in items:
            if not self._wanted(item):
                continue
            seen.add(item["fixture"]["id"])
            await self._process_fixture(item)

        # Matches that dropped out of the live feed are probably finished.
        for fid in [f for f in self.live_matches_cache if f not in seen]:
            await self._handle_missing(fid)

    async def _handle_missing(self, fid: int) -> None:
        snap = self.live_matches_cache.get(fid)
        if snap is None:
            return
        try:
            items = await self.api.get("fixtures", {"id": fid})
        except APIError as exc:
            log.warning("Could not confirm status of fixture %s: %s", fid, exc)
            items = []

        status = items[0]["fixture"]["status"]["short"] if items else None
        if status in FINISHED_STATUSES or status in LIVE_STATUSES:
            # Processes any last events (e.g. a stoppage-time goal) and posts FT.
            await self._process_fixture(items[0])
        elif status in DEAD_STATUSES:
            self.live_matches_cache.pop(fid, None)
        else:
            snap["missing"] = snap.get("missing", 0) + 1
            if snap["missing"] >= 5:
                log.info("Dropping fixture %s from cache after repeated misses", fid)
                self.live_matches_cache.pop(fid, None)

    async def _process_fixture(self, item: dict) -> None:
        fx = item["fixture"]
        fid = fx["id"]
        status = fx["status"]["short"]
        ctx = {
            "home": item["teams"]["home"]["name"],
            "away": item["teams"]["away"]["name"],
            "hg": item["goals"].get("home") or 0,
            "ag": item["goals"].get("away") or 0,
            "league": item["league"]["name"],
        }
        self.live_matches_cache[fid] = {
            **ctx,
            "status": status,
            "minute": fx["status"].get("elapsed"),
            "missing": 0,
        }

        # First time we ever see this fixture: record what already happened
        # WITHOUT posting, so a restart/late start never spams old events.
        baseline_key = f"{fid}:baseline"
        baseline = baseline_key not in self._posted
        if baseline:
            self._mark(baseline_key)

        counts: Counter[str] = Counter()
        for ev in item.get("events") or []:
            t = ev.get("time") or {}
            if ev.get("uid"):  # provider supplied a stable ID (ESPN adapter)
                base = f"{fid}:{ev['uid']}"
            else:
                base = ":".join(
                    str(x)
                    for x in (
                        fid, t.get("elapsed"), t.get("extra"), ev.get("type"), ev.get("detail"),
                        (ev.get("player") or {}).get("id"), (ev.get("team") or {}).get("id"),
                    )
                )
            counts[base] += 1
            key = f"{base}#{counts[base]}"
            if key in self._posted:
                continue
            if baseline:
                self._mark(key)
                continue
            text = self._format_event(ev, ctx)
            if text is None:
                self._mark(key)  # irrelevant event (yellow card, sub...) - never revisit
            elif await self.post(text):
                self._mark(key)
            else:
                break  # Telegram failed - keep order and retry next tick

        if status == "HT":
            await self._post_once(f"{fid}:HT", halftime_message(ctx), silent=baseline)
        if status in FINISHED_STATUSES:
            await self._post_once(f"{fid}:FT", fulltime_message(ctx), silent=baseline)
            self.live_matches_cache.pop(fid, None)

    @staticmethod
    def _format_event(ev: dict, ctx: dict) -> str | None:
        etype = ev.get("type")
        detail = (ev.get("detail") or "")
        dl = detail.lower()
        t = ev.get("time") or {}

        if etype == "Goal":
            if "missed" in dl:
                return None
            tag = " (OG)" if "own" in dl else " (Pen)" if "penalty" in dl else ""
            player = (ev.get("player") or {}).get("name") or "Unknown"
            return goal_message(
                ctx["home"], ctx["away"], ctx["hg"], ctx["ag"],
                fmt_minute(t.get("elapsed"), t.get("extra")), player, tag,
            )
        if etype == "Card" and ("red" in dl or "second yellow" in dl):
            return red_card_message(ctx, ev)
        if etype == "Var" and "goal" in dl and ("cancel" in dl or "disallow" in dl):
            return var_message(ctx, ev)
        return None

    # ------------------------------ helpers ------------------------------- #
    def live_summary(self) -> str:
        if not self.live_matches_cache:
            return "No tracked matches are live right now."
        lines = []
        for s in self.live_matches_cache.values():
            when = "HT" if s["status"] == "HT" else f"{s['minute']}'" if s["minute"] else s["status"]
            lines.append(f"🔴 {s['home']} {s['hg']} - {s['ag']} {s['away']} ({when})")
        return "\n".join(lines)
