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
- Live activity on the homepage: games in progress, recent activity and this week's highlights
- Player pages (`/player/<id>`): record, form, civilizations, streaks, rivals, teammates, achievements and recent matches
- Civilization stats (`/civs`): picks, win rates and bans per civilization, filterable by queue
- 12 achievements calculated from match history and voice time
- About page (`/about`) and privacy page (`/privacy`)
- Link previews (Open Graph) on every page, with player-specific previews on player pages
- "Log in with Discord" accounts with a member area (side menu):
  - `/dashboard`: record, streak, club rank, wins and losses per week, voice time per week, civs,
    last match and achievements in progress
  - `/stats`: official AoE4World rating over time, games per queue, and when you usually play
  - `/account`: link your AoE4 account (shared with the bot's `/aoe4link`) and Discord server status
  - `/settings`: hide your profile, log out
  - `/queue`: live queues (1v1, 2v2, 3v3, 4v4, 4v3, 3v2) with Join, Switch and Leave
  - Logged-in members also get a "Welcome back" strip on the homepage
- Placeholder pages for Tournament and Contact (greyed out in the navigation)

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
   - Optional: `SITE_URL`, your public address such as `https://thecheeseclub.example.com`. Link previews
     use it to build full links. Without it the site works the address out from each request.
   - Optional: `HIDDEN_DISCORD_IDS`, a comma-separated list of Discord IDs for members who asked to be
     hidden. See "Member privacy" below.
   - Do not change the existing bot or Postgres variables.
5. Deploy. Railway will use the Procfile / railway.json start command.
6. Generate a public domain in the new website service's Settings → Networking.

## Logging in (Discord accounts)
Members log in with Discord; there are no passwords and no separate sign-up. To switch it on:
1. In the [Discord Developer Portal](https://discord.com/developers/applications), open your bot's
   application → **OAuth2**. Copy the **Client ID** and **Client Secret** (reset it if you can't see it).
2. Under **Redirects**, add your website address followed by `/auth/callback`, exactly, for example
   `https://thecheeseclub.up.railway.app/auth/callback`. Add one per address if you use several.
3. Add these variables to the website service in Railway:
   - `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET`
   - `SESSION_SECRET`: a long random string, like `PUBLIC_ID_SECRET` but different. Changing it logs
     everyone out.
   - `SITE_URL`: must match the redirect address you registered.
   - Optional: `DISCORD_GUILD_ID` (your server's ID; otherwise the server the bot syncs members from)
     and `DISCORD_INVITE_URL` (default `https://discord.gg/thecheeseclub`).
Without the client ID and secret, the site works normally and the login page says logging in is not
switched on yet. Scopes requested: `identify` and `guilds` (only to check server membership; not stored).

## Database access
- The site reads the bot's tables: `aoe_queue_stats`, `aoe_matches`, `aoe4_links`, `vc_stats`,
  `vc_logs`, `vc_live` and `discord_member_profiles`.
- It writes in exactly two places, both for logged-in members acting on their own account:
  `aoe4_links` (linking or unlinking their AoE4 account, the same way the bot's `/aoe4link` does) and
  its own `website_member_settings` table (the "Hide my profile" choice), which it creates on startup.
- On startup it runs `CREATE TABLE IF NOT EXISTS discord_member_profiles`, so the site still works
  before the bot's member sync has created that table. It never alters or deletes tables or rows.
- Write endpoints require a logged-in member and a request from the website itself (cross-site requests
  are refused).

## Member privacy
Discord user IDs are never sent to browsers. Each member gets a stable public ID (`p` followed by
16 hex characters) made by encrypting their Discord ID with `PUBLIC_ID_SECRET`. Pages show only a
masked hint such as `4246**************`.
- If `PUBLIC_ID_SECRET` is not set, the key is derived from `DATABASE_URL`, so profile links change
  if the database password is rotated. Set `PUBLIC_ID_SECRET` to avoid this.
- `/api/players/{id}` also accepts a raw Discord ID, so older shared links keep working.
- Member search matches names, or a complete Discord ID. Partial Discord IDs are not searchable.
- To hide a member (as offered on the privacy page), add their Discord ID to `HIDDEN_DISCORD_IDS` and
  redeploy. They disappear from leaderboards, member lists, player pages and highlights, and show as
  "Hidden player" in match details. Their games still count towards totals and civilization stats.
- To delete a member's records entirely, remove their rows from the bot's database tables.

## Live data from the Discord bot
These need the bot version that saves live state (Cheese Club bot, November 2026 or later). With an
older bot the site still works and simply shows less.
- **Matches in progress:** the bot saves each running match's state to `aoe_matches.active_state`
  every 10 seconds when something changes. The website shows the phase, the teams as they are
  drafted, the map, and civ bans and picks **only once Discord has revealed them** (the server strips
  hidden picks before anything reaches a browser). Open match pop-ups refresh every 15 seconds.
- **Ratings at match time:** the AoE4World ratings the bot froze at queue pop are kept in
  `aoe_matches.elo_snapshot` and shown in match pop-ups. "Biggest upset" uses them when every
  player has one.
- **In voice now:** the bot keeps `vc_live` up to date with who is in a public voice channel
  (visible to @everyone, AFK excluded). Rows not refreshed for 10 minutes are ignored.

## Queueing from the website
Needs the bot version with website queueing (it creates `aoe_queue_live` and `aoe_queue_requests`).
- The bot publishes who is waiting in each queue every few seconds, plus a heartbeat. If the website
  hasn't heard from the bot for 60 seconds, it shows the queue as offline and disables joining.
- Join, Switch and Leave on the website create a request that the bot carries out with the same code
  as its Discord queue menu (same lock, auto-switching, 30-minute timeout, queue message and match
  start). The website then shows the bot's answer.
- Members must be in the server and linked to AoE4World (or bypassed by an admin). By default they
  must also be in a public voice channel. To drop that rule, set `WEB_QUEUE_REQUIRE_VOICE=false` on
  the **bot** service in Railway; the website then shows a reminder instead.

## Queue alerts and notifications
- Members choose their alerts in **Settings → Queue alerts**: which queues, when (1 or 2 players away,
  or when someone joins an empty queue), quiet hours in their own time zone, and how (Discord DM,
  the website's notification bell, and/or browser pop-ups while the site is open).
- The **bot** sends queue alerts (at most one per member every 15 minutes, never while they're
  already queued or playing, never during quiet hours or after "Mute for today"), test alerts, and
  the "removed after 30 minutes" notice. Set `WEBSITE_URL` on the **bot** service to your website's
  address so links in DMs point to the right place (default `https://thecheeseclub.up.railway.app`).
- The **website** adds "your match is starting", match result and new achievement notices for
  members who have logged in (a background job every 30 seconds). A member's first check only records
  the achievements they already have, so nobody gets a flood of old ones.
- Tables: `member_alert_settings`, `member_notifications` (created by both bot and website),
  `website_users` and `member_achievements_seen` (website).

## Tournaments
Four formats: **1v1 Cup** and **Team Cup** (2v2/3v3/4v4 fixed teams), both single elimination,
**Random Team 3v3** and **Free-for-all** (below). Admins can cancel or **delete** a tournament
(type its name to confirm); deleting removes it everywhere, but games played stay in match history. Tournament games are marked
as tournament games (`aoe_matches.is_tournament`): they appear in match history and live with a
"Tournament" label, but don't change queue stats, community ratings, player insights, civ stats or
highlights.
- **Admins** create tournaments at `/tournament/new`. Set `ADMIN_DISCORD_IDS` on the **website** service
  (comma-separated Discord IDs) to choose who can create, start, cancel, set winners and remove players.
- **Lifecycle:** sign-ups open as soon as a tournament is created. Check-in opens automatically before the
  start (15–60 minutes, chosen by the admin). At the start time the website drops players who didn't check
  in, fills free spots from checked-in players on the waiting list, seeds everyone by AoE4World rating
  (community rating if they have none) and builds the bracket, giving byes to the top seeds. Admins can also
  start early.
- **Playing:** when a match is ready, the bot DMs both players and the website shows an **I'm ready** button.
  Once both are ready, the bot starts the game in Discord (thread, map veto, civ bans) with a tournament
  heading. Results come from the normal match result. In best-of series the next game starts automatically.
  A game cancelled in Discord doesn't count, and both players press Ready again.
- Champions and runners-up appear in the Hall of Champions and as trophies on their player pages.
- Tables: `tournaments`, `tournament_players`, `tournament_matches`, `tournament_games` (created by the website).

### Team Cup (2v2 / 3v3 / 4v4)
- A captain registers a team (name + teammates from the club). Each teammate accepts or declines the
  invite on the website; the team is registered once everyone has accepted (waiting list when full).
  Players can be in one team per tournament. Captains can replace a teammate who hasn't accepted.
- The captain checks in the team. Teams are seeded by their players' average rating.
- Every player presses Ready; the bot starts the match with the teams already set.

### Free-for-all
- 2–8 players in one lobby, 1–5 rounds, points by final place (configurable, default 8,6,4,3,2,1,0,0).
- When everyone is ready, the bot opens a Discord thread for the lobby (players host it in-game).
- Age of Empires IV doesn't report finishing places, so a player submits the order on the website and
  others confirm (at least half the lobby, minimum 2, including the reporter). A dispute goes to an
  admin, who can also set or correct placements at any time; standings recalculate.
- FFA games are not added to match history or stats.

### Random Team 3v3
- Players sign up alone. Each round the website makes new 3v3 teams: random, but balanced by rating
  (team averages within about 60) and avoiding repeated teammates (and, less strongly, opponents).
- If the player count isn't a multiple of 6, the extra players sit out: whoever has sat out least goes
  first, and nobody sits out twice in a row if avoidable. They get the sit-out points and can be
  substituted in by an admin if someone doesn't show up.
- Points are individual: win (default 1), loss 0, sit-out (default 0.5). Ties: opponents' points, then
  fewer sit-outs, then rating. Standings are calculated from saved results, so corrections update them.
- Admin chooses the rounds, points, a map for each round (or random from a pool) and the pool.
- When all six players in a match press Ready, the bot starts it in Discord with the teams already set
  (no draft), runs civ bans and picks, and the normal result confirmation records the winner. After the
  last match of a round the next round's teams are made automatically; after the last round the top 3
  are recorded (Hall of Fame, player trophies).
- Admin: set or correct results, substitute, regenerate the current round (before anyone is ready),
  remove a player from later rounds. Every admin action is saved in `tournament_admin_log`.
- Tables: `tournament_team_matches`, `tournament_round_byes`, `tournament_admin_log`,
  `tournament_teams`, `tournament_team_members`, `tournament_ffa_matches`.

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
- `GET /api/players/{public_id}/insights`
- `GET /api/civs?queue=all|1v1|2v2|3v3|4v4`
- `GET /api/activity`

`scope=community` raises the bar for inclusion: 5+ games for the AoE leaderboard and 100+ hours
for voice, versus 1 game and 10 hours for `public`.
