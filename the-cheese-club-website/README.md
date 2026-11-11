# The Cheese Club Website

A public, read-only community website for The Cheese Club (Age of Empires IV), connected to the
Discord bot's existing Railway PostgreSQL database.

## Included
- Homepage with founders, community totals, top-5 previews and a searchable member directory
- AoE IV leaderboard (all queues, 1v1, 2v2, 3v3, 4v4), ranked by community match wins, with each
  player's official Solo and Team ratings from AoE4World
- Voice-activity leaderboard
- Match history with search and a match-detail view (teams, civilizations, bans)
- Member profiles, linkable with `?member=<id>` on `/`, `/community` and `/leaderboard`
- Automatic refresh every 30 seconds on the homepage and community page
- Placeholder pages for Tournament, Contact, Login and Sign Up (marked "Soon" in the navigation)

## Deploy as a NEW Railway service
1. Push this folder to a NEW GitHub repository.
2. In Railway, open the existing project and choose **Add Service → GitHub Repo**.
3. Select the new website repository.
4. Open the new website service's **Variables**:
   - Add `DATABASE_URL` as a Railway Variable Reference to the existing Postgres service's `DATABASE_URL`.
     Do not copy/paste or expose the actual database password in chat.
   - Add `PUBLIC_ID_SECRET` set to a long random string (for example the output of
     `python -c "import secrets; print(secrets.token_hex(32))"`). Keep it secret and never change it,
     or existing profile links will stop working. See "Member privacy" below.
   - Do not change the existing bot or Postgres variables.
5. Deploy. Railway will use the Procfile / railway.json start command.
6. Generate a public domain in the new website service's Settings → Networking.

## Database access
- The site only reads the bot's tables: `aoe_queue_stats`, `aoe_matches`, `aoe4_links`, `vc_stats`,
  `vc_logs` and `discord_member_profiles`.
- On startup it runs `CREATE TABLE IF NOT EXISTS discord_member_profiles`, so the site still works
  before the bot's member sync has created that table. It never alters or deletes tables or rows.
- The public API has no write endpoints.

## Member privacy
Discord user IDs are never sent to browsers. Each member gets a stable public ID (`p` followed by
16 hex characters) made by encrypting their Discord ID with `PUBLIC_ID_SECRET`. Pages show only a
masked hint such as `4246**************`.
- If `PUBLIC_ID_SECRET` is not set, the key is derived from `DATABASE_URL`, so profile links change
  if the database password is rotated. Set `PUBLIC_ID_SECRET` to avoid this.
- `/api/players/{id}` also accepts a raw Discord ID, so older shared links keep working.
- Member search matches names, or a complete Discord ID. Partial Discord IDs are not searchable.

## Voice time
A member's voice time is their `vc_stats` total. Members with no `vc_stats` rows fall back to the
sum of their `vc_logs` sessions. Every page and endpoint uses this same rule.

## AoE4World ratings
Ratings are fetched from `https://aoe4world.com/api/v0/players/{profile_id}` and cached in memory
for 30 minutes. Profiles AoE4World doesn't know are also cached for 30 minutes. Temporary failures
keep the last good value and retry after 5 minutes.

## Known limitations
- Queue waiting lists are held in the bot's memory, so they are not available on the website yet.
- Tournament registration and brackets, member login and sign-up are future features.

## Project layout
- `main.py`: API and page routes
- `static/*.html`: one file per page, each with its own page-specific styles and script
- `static/common.js`: shared helpers (civilization flags, match details, ban text, navigation)
- `static/common.css`: shared styles for those helpers
- `static/civ-flags/`: civilization emblems (128×128)

## Local run
```bash
python -m venv .venv
# activate the environment
pip install -r requirements.txt
export DATABASE_URL='your-postgres-connection-string'
export PUBLIC_ID_SECRET='any-long-random-string'
uvicorn main:app --reload
```

## API routes
- `GET /api/health`
- `GET /api/overview`
- `GET /api/leaderboards/aoe?queue=all|1v1|2v2|3v3|4v4&limit=&scope=public|community`
- `GET /api/leaderboards/vc?limit=&scope=public|community`
- `GET /api/members?limit=&search=`
- `GET /api/matches?limit=&search=`
- `GET /api/matches/{match_id}`
- `GET /api/players/{public_id}`

`scope=community` raises the bar for inclusion: 5+ games for the AoE leaderboard and 100+ hours
for voice, versus 1 game and 10 hours for `public`.
