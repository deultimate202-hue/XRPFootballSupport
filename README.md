# XRP ARMY Super Football Fans Bot - Setup Guide

Live match alerts + AI community manager + 1-on-1 support desk with admin ticket tunnel.

```
main.py          Telegram handlers, AI manager, moderation, ticket tunnel
match_engine.py  Live polling worker, dedup ledger, alert formatting
espn_api.py      Keyless ESPN data provider (default)
check_espn.py    One-minute test of the live-score feed
storage.py       Atomic JSON persistence (dedup ledger + ticket routing)
```

## 1. Get your keys

### Telegram bot token
1. Open **@BotFather** -> `/newbot` -> pick a name and a username ending in `bot`.
2. Copy the token -> `TELEGRAM_BOT_TOKEN`.
3. Still in BotFather: `/setprivacy` -> select your bot -> **Disable**. Without this the bot cannot see ordinary group messages (needed for moderation and AI replies).
4. Optional: `/setjoingroups` -> Enable, `/setdescription`, `/setuserpic`.

### AI key (free): Groq
1. Go to https://console.groq.com/keys and sign up (Google/GitHub login works, no credit card).
2. Click **Create API Key**, copy it -> `AI_API_KEY`.
3. Keep the defaults: `AI_BASE_URL=https://api.groq.com/openai/v1`, `AI_MODEL=llama-3.1-8b-instant`, `AI_FALLBACK_MODEL=llama-3.3-70b-versatile`.

Free-tier limits change, so check https://console.groq.com/settings/limits. At the time of writing, `llama-3.1-8b-instant` allows thousands of requests/day, while `llama-3.3-70b-versatile` is smarter but capped at roughly 100K tokens/day. The default uses the 8B model for volume and falls back to the 70B when rate-limited. Swap them if your group is small and you want better answers.

**Other free options** (same code, just change the three `AI_*` variables):
- Google Gemini: key from https://aistudio.google.com/apikey, `AI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/`, `AI_MODEL=gemini-2.0-flash`
- OpenRouter: key from https://openrouter.ai/keys, `AI_BASE_URL=https://openrouter.ai/api/v1`, a model ending in `:free`

### Live scores: no account or key needed (ESPN provider)
The default `FOOTBALL_API_PROVIDER=espn` reads ESPN's public scoreboard feed. There is nothing to sign up for.

1. Set `FOOTBALL_API_PROVIDER=espn`.
2. Set `LEAGUE_IDS` to ESPN league slugs:

| League | Slug | League | Slug |
|---|---|---|---|
| Premier League | `eng.1` | Bundesliga | `ger.1` |
| La Liga | `esp.1` | Ligue 1 | `fra.1` |
| Serie A | `ita.1` | Champions League | `uefa.champions` |
| Europa League | `uefa.europa` | MLS | `usa.1` |

3. **Verify it before going live:** run `python check_espn.py` (ideally during a real match). It prints each match, score, goals and red cards exactly as the bot will read them. If a slug is wrong you'll see a 404 error naming it.

**Honest limitations of this free option**
- It is an **unofficial, undocumented** feed. ESPN may change or block it without notice, and there is no uptime guarantee or published latency. Watch the logs after each deploy.
- Goals and red cards are posted once ESPN lists them with a player name, so an alert can trail the real goal by a short while.
- It cannot announce a goal later cancelled by VAR (ESPN just removes it).
- Check ESPN's terms of use before relying on it for a public or commercial community.
- Uses roughly one request per league per minute while matches are on.

**If you later want an official source:** API-Football (`FOOTBALL_API_PROVIDER=direct` + `FOOTBALL_API_KEY`, numeric `LEAGUE_IDS` like `39,140,2`) is still supported. Its free plan is about 100 requests/day. football-data.org's free tier is not suitable here: it has delayed scores and no goal-scorer or card data.

## 2. Find `TARGET_GROUP_ID` and `ADMIN_CHAT_ID`

1. Create two groups: the **public community group** and a **private staff group**.
2. Add your bot to both. Make it an **admin** in the public group with *Delete messages* permission (needed for moderation). In the staff group it only needs to read and send messages.
3. Temporarily set placeholder values for the two IDs (e.g. `-1`), start the bot, and send **`/chatid`** in each group. The bot replies with the chat's ID.
   - Alternative: add **@RawDataBot** or **@getidsbot** to the group and read the `chat.id` field.
4. Group IDs are **negative**; supergroups look like `-1001234567890`. Copy the full number, including the minus sign.
5. Put the real values in your environment and restart.
6. In the staff group, send **`/testalert`**: a sample goal alert should appear in the public group.

Note: if you convert a group to a supergroup later (e.g. by making history public), its ID changes. Re-run `/chatid`.

## 3. Run it

### Local / Linux VPS (Python)
```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in values
python main.py
```
Keep it alive with systemd:
```ini
# /etc/systemd/system/xrpbot.service
[Unit]
Description=XRP ARMY Telegram Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/xrp-bot
ExecStart=/opt/xrp-bot/.venv/bin/python main.py
Restart=always
RestartSec=5
User=bot

[Install]
WantedBy=multi-user.target
```

### Docker (VPS)
```bash
docker build -t xrp-bot .
docker run -d --name xrp-bot --restart unless-stopped \
  --env-file .env -v xrp_bot_data:/data xrp-bot
```

### Render (Background Worker) / Railway
- Create a **Background Worker** (Render) or service (Railway) from this repo, using the Dockerfile.
- Add all variables from `.env.example` in the dashboard (no inline `# comments` in the value fields).
- **Attach a persistent disk/volume mounted at `/data`.** Without it, the dedup ledger and open tickets are lost on every redeploy and goals from live matches could be re-announced. (Render disks require a paid worker; Railway offers Volumes.)
- **Run exactly ONE instance.** Telegram long-polling allows one consumer per token; two overlapping instances cause `Conflict` errors and could double-post. Configure your platform to stop the old instance before starting the new one, or accept a few seconds of downtime per deploy. The bot shuts down gracefully on SIGTERM and replays any updates Telegram queued meanwhile.

## 4. How it behaves

**Live engine**
- Posts goal alerts (with `(Pen)` / `(OG)` tags), red cards (straight and second yellow), VAR-disallowed goals, half-time, full-time, and a preview ~60 min before kickoff.
- Exactly-once: every event gets a deterministic key stored in `engine_state.json` before being forgotten. The first time a fixture is seen, existing events are recorded silently, so restarts never spam old goals.
- Full-time is confirmed with a direct fixture lookup when a match leaves the live feed, so late goals are still announced before the FT message.

**Public group**
- The bot replies when mentioned, when someone replies to it, or when a question about football/XRP/onboarding is asked (with per-user and per-chat cooldowns).
- Moderation removes messages from non-admins that contain links outside the three authorized domains or earnings/financial-advice claims, warns the user, and logs it to the staff group. Group admins are exempt.
- AI replies pass through the same filter before sending: unauthorized links are stripped and earnings language is replaced by a safe fallback.
- `/live` shows tracked live matches (no API cost); `/trivia` posts a trivia question.

**Private DMs and tickets**
- `/start` shows the menu: authorized links, FAQ shortcuts, **Talk to a human**.
- The AI returns `{"reply": ..., "escalate": bool, "summary": ...}`. If `escalate` is true or the user taps the button, a ticket is posted in the staff group.
- **Staff reply to the ticket message** -> the answer is delivered to the user's DM (text or any media). Follow-up user messages appear in the staff group threaded under the same ticket. While a ticket is open the AI stays silent.
- Staff reply `/close` to a ticket to close it; users can send `/close` themselves.

## 5. Compliance notes (please read)

- The filters are keyword-based safety nets, not legal compliance. Review the wording with whoever owns your legal/compliance obligations.
- Links to "match odds" and sports content can fall under gambling/advertising rules in some countries. Check the rules for where your audience lives.
- Keep the bot's system prompt (`SYSTEM_PROMPT` in `main.py`) factual. The bot is instructed not to invent product details, so if you want it to explain onboarding in more depth, add accurate, approved text to that prompt.
- Respect the API-Football terms of use for your plan when redistributing live data publicly.

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| Bot ignores group chatter | BotFather `/setprivacy` -> Disable, then remove and re-add the bot to the group |
| Moderation does nothing | Bot must be a group admin with *Delete messages* |
| `Chat not found` on startup posts | Wrong `TARGET_GROUP_ID` (missing `-100`?) or bot not in the group |
| `ESPN does not know league slug` | Wrong slug in `LEAGUE_IDS`; run `python check_espn.py` |
| `API errors: {'rateLimit': ...}` in logs | (API-Football only) daily quota hit; narrow `LEAGUE_IDS` |
| `Conflict: terminated by other getUpdates` | Two instances running with the same token |
| No kickoff previews / no polling | `LEAGUE_IDS` don't match today's fixtures; set `ALWAYS_POLL=true` to test |
| Duplicate alerts after redeploy | `/data` volume not persisted |
