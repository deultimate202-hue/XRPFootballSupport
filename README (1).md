# XRP ARMY Super Football Fans Bot

## Quick start (same variables as before)

Set these on your host. Nothing else is required:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token from @BotFather |
| `OPENAI_API_KEY` (or `AI_API_KEY` / `GROQ_API_KEY`) | your AI key. `gsk_...` is detected as Groq (free), `sk-...` as OpenAI |
| `TARGET_GROUP_ID` | public group ID (optional: or send `/setgroup` in the group) |
| `ADMIN_CHAT_ID` | staff chat ID (optional: or send `/setadmin` in the staff group) |
| `LEAGUE_IDS` | your usual numbers work, e.g. `39,140,2` |
| `FOOTBALL_API_KEY` | **not needed** - live scores use a free feed with no key; if set, it is ignored |

Redeploy, then send `/aitest` to the bot in private chat to confirm the AI works, and `/testalert` in the staff chat to confirm posting works.

---


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
3. Keep the defaults: `AI_BASE_URL=https://api.groq.com/openai/v1`, `AI_MODEL=openai/gpt-oss-20b`, `AI_FALLBACK_MODEL=openai/gpt-oss-120b`.

**Model names change.** Groq retired `llama-3.1-8b-instant` and `llama-3.3-70b-versatile` on 2026-08-16 (https://console.groq.com/docs/deprecations). If your configured model disappears, the bot lists the provider's models and switches to a working one automatically (it logs `switching to ...`). Send `/aitest` to the bot in private chat any time to see the exact AI error, and check https://console.groq.com/docs/models and https://console.groq.com/settings/limits for current models and free limits.

**Other free options** (same code, change the three `AI_*` variables; check each provider's current model list first):
- Google Gemini: key from https://aistudio.google.com/apikey, `AI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/`
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
- **ESPN blocking (HTTP 403 in the logs):** in Aug 2026 ESPN started rejecting browser-style User-Agents. The bot sends an identifying one and automatically tries two alternative routes. If every route still returns 403, ESPN is blocking your host's IP range. Options: set `ESPN_PROXY_URL` to a proxy, host the bot on a different provider or a home/VPS machine, or use API-Football.

**If you later want an official source:** API-Football (`FOOTBALL_API_PROVIDER=direct` + `FOOTBALL_API_KEY`, numeric `LEAGUE_IDS` like `39,140,2`) is still supported. Its free plan is about 100 requests/day. football-data.org's free tier is not suitable here: it has delayed scores and no goal-scorer or card data.

## 2. Connect your two groups (no IDs or variables needed)

You do not have to look up any group ID. Start the bot first, then tell it which group is which, from inside Telegram:

1. Create two groups: the **public community group** and a **private staff group**.
2. Add your bot to both. Make it an **admin** in the public group with *Delete messages* permission. In the staff group it only needs to read and send messages.
3. In the **public group**, send: `/setgroup`
4. In the **staff group**, send: `/setadmin`
5. In the staff group, send `/testalert`. A sample goal alert should appear in the public group.
6. Send `/status` anywhere to see what is connected.

**Who can do this:** only a group admin. The **first admin to run `/setgroup` or `/setadmin` becomes the bot owner**, and from then on only that person can change the settings. Do the setup yourself right after adding the bot. If you turned on "Remain anonymous" as an admin, turn it off first or the command is rejected. To pre-set the owner instead, add the optional variable `OWNER_USER_ID` (your Telegram user ID; send `/chatid` to the bot to see it).

**Until both groups are set:** the live engine stays idle (no scores are fetched and nothing is posted), and DM support tells users that the support team is not connected yet.

**Where it is stored:** `settings.json` in `STATE_DIR`. **You must attach a persistent disk at `/data`** or the settings are forgotten on every redeploy and you will have to run the two commands again.

**Other things to know**
- To switch groups, the owner runs `/setgroup` or `/setadmin` in the new group; it replaces the old one.
- If Telegram upgrades a group to a supergroup, the bot follows the new ID automatically.
- After setup, consider disabling "Allow Groups" for the bot in @BotFather (`/setjoingroups`) so strangers cannot add it elsewhere.
- Prefer variables? `TARGET_GROUP_ID`, `ADMIN_CHAT_ID` and `OWNER_USER_ID` still work as optional fallbacks. Values stored by the commands take priority.

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
| Nothing is posted / `/testalert` fails | Run `/setgroup` in the public group and `/status` to confirm; bot must be a member |
| Settings forgotten after redeploy | No persistent disk at `/data` |
| "Only the bot owner can change these settings" | Someone else claimed ownership first; set `OWNER_USER_ID` or clear `settings.json` |
| `HTTP 403 ... ESPN is blocking this server` | See 'ESPN blocking' above (proxy / different host) |
| `model_not_found` / AI hiccup | Send `/aitest`; update `AI_MODEL` (auto-recovery usually handles it) |
| `ESPN does not know league slug` | Wrong slug in `LEAGUE_IDS`; run `python check_espn.py` |
| `API errors: {'rateLimit': ...}` in logs | (API-Football only) daily quota hit; narrow `LEAGUE_IDS` |
| `Conflict: terminated by other getUpdates` | Two instances running with the same token |
| No kickoff previews / no polling | `LEAGUE_IDS` don't match today's fixtures; set `ALWAYS_POLL=true` to test |
| Duplicate alerts after redeploy | `/data` volume not persisted |
