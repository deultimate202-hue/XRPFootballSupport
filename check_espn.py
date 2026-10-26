"""
check_espn.py - One-minute sanity check for the keyless ESPN provider.

    python check_espn.py                 # uses LEAGUE_IDS from .env (or defaults)
    python check_espn.py eng.1 esp.1     # or pass league slugs directly

Prints today's matches exactly as the bot will see them (status, score, goals, red cards).
Run it DURING a live match to confirm goals/cards show up before relying on the bot.
"""
import asyncio
import os
import re
import sys

from dotenv import load_dotenv

from espn_api import ESPNFootballAPI


async def main() -> None:
    load_dotenv()
    leagues = sys.argv[1:] or [x for x in re.split(r"[,\s]+", os.getenv("LEAGUE_IDS", "")) if x] \
        or ["eng.1", "esp.1", "uefa.champions"]
    api = ESPNFootballAPI(leagues)
    try:
        for slug in leagues:
            try:
                items = await api._scoreboard(slug)
            except Exception as exc:  # noqa: BLE001
                print(f"[{slug}] ERROR: {exc}")
                continue
            print(f"\n=== {slug}: {len(items)} match(es) ===")
            for it in items:
                st = it["fixture"]["status"]
                print(f"{it['teams']['home']['name']} {it['goals']['home']} - {it['goals']['away']} "
                      f"{it['teams']['away']['name']}  [{st['short']} {st['elapsed'] or ''}]")
                for ev in it["events"]:
                    t = ev["time"]
                    icon = "⚽" if ev["type"] == "Goal" else "🟥"
                    print(f"    {icon} {t['elapsed']}'{('+' + str(t['extra'])) if t['extra'] else ''} "
                          f"{ev['player']['name']} ({ev['team']['name']}) - {ev['detail']}")
    finally:
        await api.close()


asyncio.run(main())
