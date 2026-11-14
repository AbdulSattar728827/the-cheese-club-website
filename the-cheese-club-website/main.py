import os
import re
import json
import time
import hmac
import asyncio
import hashlib
import logging
import secrets
import html
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse

log = logging.getLogger("cheese_club")

DATABASE_URL = os.getenv("DATABASE_URL")
pool: Optional[asyncpg.Pool] = None
http_client: Optional[httpx.AsyncClient] = None

AOE4WORLD_API = "https://aoe4world.com/api/v0"
RATING_CACHE_SECONDS = 1800        # good results and "player not found" answers
RATING_RETRY_SECONDS = 300         # temporary failures (timeouts, 5xx, rate limits)
RATING_CACHE_MAX_ENTRIES = 5000
_rating_cache: dict = {}           # profile_id -> (expires_at, result)
_rating_semaphore = asyncio.Semaphore(8)
EMPTY_RATINGS = {"solo_elo": None, "team_elo": None}

# Website admins (tournament management): comma-separated Discord IDs.
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_DISCORD_IDS", "").split(",") if x.strip()}

TOURNAMENT_TABLES = [
    """CREATE TABLE IF NOT EXISTS tournaments (
        id SERIAL PRIMARY KEY, guild_id TEXT, name TEXT NOT NULL, description TEXT,
        mode TEXT NOT NULL DEFAULT '1v1', format TEXT NOT NULL DEFAULT 'single',
        max_players INTEGER NOT NULL DEFAULT 8, starts_at TIMESTAMPTZ NOT NULL,
        checkin_minutes INTEGER NOT NULL DEFAULT 30,
        bo_default INTEGER NOT NULL DEFAULT 1, bo_semi INTEGER NOT NULL DEFAULT 3, bo_final INTEGER NOT NULL DEFAULT 5,
        rules TEXT, prizes TEXT, status TEXT NOT NULL DEFAULT 'signup',
        winner_id TEXT, runner_up_id TEXT, created_by TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
        checkin_notified BOOLEAN NOT NULL DEFAULT FALSE
    )""",
    """CREATE TABLE IF NOT EXISTS tournament_players (
        tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL, signed_up_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        checked_in_at TIMESTAMPTZ, status TEXT NOT NULL DEFAULT 'registered',
        seed INTEGER, rating INTEGER, PRIMARY KEY (tournament_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS tournament_matches (
        id SERIAL PRIMARY KEY,
        tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
        round INTEGER NOT NULL, slot INTEGER NOT NULL, round_name TEXT NOT NULL, best_of INTEGER NOT NULL,
        p1_id TEXT, p2_id TEXT, p1_seed INTEGER, p2_seed INTEGER,
        p1_score INTEGER NOT NULL DEFAULT 0, p2_score INTEGER NOT NULL DEFAULT 0,
        winner_id TEXT, status TEXT NOT NULL DEFAULT 'pending', note TEXT,
        p1_ready_at TIMESTAMPTZ, p2_ready_at TIMESTAMPTZ, announced BOOLEAN NOT NULL DEFAULT FALSE,
        ready_since TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), finished_at TIMESTAMPTZ,
        next_match_id INTEGER, next_slot INTEGER, UNIQUE (tournament_id, round, slot)
    )""",
    # Random Team 3v3 settings on the shared tournaments table
    "ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS rounds INTEGER NOT NULL DEFAULT 5",
    "ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS points_win NUMERIC NOT NULL DEFAULT 1",
    "ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS points_bye NUMERIC NOT NULL DEFAULT 0.5",
    "ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS round_maps TEXT[] NOT NULL DEFAULT '{}'",
    "ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS map_pool TEXT[] NOT NULL DEFAULT '{}'",
    "ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS current_round INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS third_id TEXT",
    """CREATE TABLE IF NOT EXISTS tournament_team_matches (
        id SERIAL PRIMARY KEY,
        tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
        round INTEGER NOT NULL, match_no INTEGER NOT NULL,
        team1 TEXT[] NOT NULL, team2 TEXT[] NOT NULL, team1_avg INTEGER, team2_avg INTEGER, map TEXT,
        status TEXT NOT NULL DEFAULT 'ready', winner_team SMALLINT, aoe_match_id INTEGER,
        ready_ids TEXT[] NOT NULL DEFAULT '{}', announced BOOLEAN NOT NULL DEFAULT FALSE, note TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at TIMESTAMPTZ, UNIQUE (tournament_id, round, match_no)
    )""",
    """CREATE TABLE IF NOT EXISTS tournament_round_byes (
        tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
        round INTEGER NOT NULL, user_id TEXT NOT NULL, substituted_into INTEGER,
        PRIMARY KEY (tournament_id, round, user_id)
    )""",
    "ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS team_size INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS ffa_points INTEGER[] NOT NULL DEFAULT '{8,6,4,3,2,1,0,0}'",
    "ALTER TABLE tournament_matches ADD COLUMN IF NOT EXISTS ready_ids TEXT[] NOT NULL DEFAULT '{}'",
    """CREATE TABLE IF NOT EXISTS tournament_teams (
        id SERIAL PRIMARY KEY,
        tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
        name TEXT NOT NULL, captain_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'forming',
        checked_in_at TIMESTAMPTZ, seed INTEGER, rating INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS tournament_team_members (
        team_id INTEGER NOT NULL REFERENCES tournament_teams(id) ON DELETE CASCADE,
        tournament_id INTEGER NOT NULL, user_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'invited',
        invited_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), responded_at TIMESTAMPTZ,
        PRIMARY KEY (team_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS tournament_ffa_matches (
        id SERIAL PRIMARY KEY,
        tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
        round INTEGER NOT NULL, players TEXT[] NOT NULL, map TEXT,
        status TEXT NOT NULL DEFAULT 'ready', ready_ids TEXT[] NOT NULL DEFAULT '{}',
        announced BOOLEAN NOT NULL DEFAULT FALSE, thread_id TEXT,
        placements JSONB, reported_by TEXT, confirmations TEXT[] NOT NULL DEFAULT '{}', disputes TEXT[] NOT NULL DEFAULT '{}',
        note TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at TIMESTAMPTZ, UNIQUE (tournament_id, round)
    )""",
    """CREATE TABLE IF NOT EXISTS tournament_admin_log (
        id SERIAL PRIMARY KEY, tournament_id INTEGER, admin_id TEXT NOT NULL, action TEXT NOT NULL,
        detail JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS tournament_games (
        id SERIAL PRIMARY KEY,
        tmatch_id INTEGER NOT NULL REFERENCES tournament_matches(id) ON DELETE CASCADE,
        aoe_match_id INTEGER UNIQUE, game_no INTEGER NOT NULL, winner_id TEXT,
        status TEXT NOT NULL DEFAULT 'playing', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
]

# Members who asked not to appear on the website (see the privacy page).
# Comma-separated Discord IDs in the HIDDEN_DISCORD_IDS variable.
_ENV_HIDDEN = {x.strip() for x in os.getenv("HIDDEN_DISCORD_IDS", "").split(",") if x.strip()}
# Kept up to date in place (see refresh_hidden) so every "in HIDDEN_IDS" check sees
# both the HIDDEN_DISCORD_IDS variable and members' own "Hide my profile" setting.
HIDDEN_IDS = set(_ENV_HIDDEN)


def visible(rows):
    """Drop rows belonging to hidden members."""
    return [r for r in rows if str(r["user_id"]) not in HIDDEN_IDS]

# Voice-time totals, shared by every endpoint so all pages agree.
# A member's total comes from vc_stats; members who only have session rows
# in vc_logs fall back to the sum of those sessions.
VC_TOTALS_CTE = """
    vc_stats_totals AS (
        SELECT user_id, SUM(total_secs)::float AS total_secs
        FROM vc_stats GROUP BY user_id
    ),
    vc_log_totals AS (
        SELECT user_id, SUM(duration_s)::float AS total_secs
        FROM vc_logs GROUP BY user_id
    ),
    vc AS (
        SELECT COALESCE(vs.user_id, vl.user_id) AS user_id,
               COALESCE(vs.total_secs, vl.total_secs, 0)::float AS total_secs
        FROM vc_stats_totals vs
        FULL OUTER JOIN vc_log_totals vl ON vl.user_id = vs.user_id
    )
"""


# ---------------------------------------------------------------------------
# Public member IDs
# Discord IDs never leave the server. Each member gets a stable public ID made
# by encrypting the Discord ID with a keyed 64-bit Feistel cipher, so it can be
# turned back into the Discord ID here but cannot be reversed by visitors.
# ---------------------------------------------------------------------------
def _id_secret() -> bytes:
    configured = os.getenv("PUBLIC_ID_SECRET")
    if configured:
        return configured.encode()
    if DATABASE_URL:
        log.warning("PUBLIC_ID_SECRET is not set; deriving public member IDs from DATABASE_URL. "
                    "Set PUBLIC_ID_SECRET so profile links survive a database password change.")
        return hashlib.sha256(b"cheese-club-public-id:" + DATABASE_URL.encode()).digest()
    log.warning("PUBLIC_ID_SECRET is not set; public member IDs will change on every restart.")
    return secrets.token_bytes(32)


_ID_KEY = _id_secret()
_ID_ROUNDS = 6
_PUBLIC_ID_RE = re.compile(r"^p[0-9a-f]{16}$")
_DISCORD_ID_RE = re.compile(r"^\d{5,20}$")


def _round(i: int, half: int) -> int:
    digest = hmac.new(_ID_KEY, bytes([i]) + half.to_bytes(4, "big"), hashlib.sha256).digest()
    return int.from_bytes(digest[:4], "big")


def to_public_id(discord_id) -> Optional[str]:
    if discord_id is None:
        return None
    value = str(discord_id)
    if not _DISCORD_ID_RE.match(value) or int(value) >= 2**64:
        return None
    n = int(value)
    left, right = n >> 32, n & 0xFFFFFFFF
    for i in range(_ID_ROUNDS):
        left, right = right, left ^ _round(i, right)
    return "p" + format((left << 32) | right, "016x")


def from_public_id(public_id: str) -> Optional[str]:
    """Accept a public ID (or, for old shared links, a raw Discord ID) and return the Discord ID."""
    value = (public_id or "").strip().lower()
    if _DISCORD_ID_RE.match(value):
        return value
    if not _PUBLIC_ID_RE.match(value):
        return None
    n = int(value[1:], 16)
    left, right = n >> 32, n & 0xFFFFFFFF
    for i in reversed(range(_ID_ROUNDS)):
        left, right = right ^ _round(i, left), left
    return str((left << 32) | right)


def id_hint(discord_id) -> str:
    value = str(discord_id or "")
    return value if len(value) <= 4 else value[:4] + "*" * (len(value) - 4)


def publicize(row) -> dict:
    """Replace a row's raw Discord user_id with a public ID and a masked hint."""
    item = dict(row)
    raw = item.pop("user_id", None)
    item["user_id"] = to_public_id(raw)
    item["id_hint"] = id_hint(raw)
    return item


def like_pattern(text: str) -> str:
    """Escape LIKE wildcards so a search for '50%' or 'a_b' is taken literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def clean_search(search: str, max_len: int) -> str:
    return (search or "").strip()[:max_len]


# ---------------------------------------------------------------------------
# AoE4World ratings
# ---------------------------------------------------------------------------
def _prune_rating_cache(now: float) -> None:
    if len(_rating_cache) <= RATING_CACHE_MAX_ENTRIES:
        return
    for key in [k for k, (expires, _) in _rating_cache.items() if expires <= now]:
        _rating_cache.pop(key, None)
    while len(_rating_cache) > RATING_CACHE_MAX_ENTRIES:
        _rating_cache.pop(next(iter(_rating_cache)))


async def aoe4world_ratings(profile_id):
    """Fetch and cache current official AoE4World Solo and Team Ranked ratings."""
    if not profile_id:
        return dict(EMPTY_RATINGS)
    key = str(profile_id)
    cached = _rating_cache.get(key)
    if cached and time.monotonic() < cached[0]:
        return cached[1]
    async with _rating_semaphore:
        now = time.monotonic()
        cached = _rating_cache.get(key)
        if cached and now < cached[0]:
            return cached[1]
        previous = cached[1] if cached else None
        try:
            client = http_client or httpx.AsyncClient(timeout=10.0, follow_redirects=True)
            try:
                response = await client.get(f"{AOE4WORLD_API}/players/{key}")
            finally:
                if client is not http_client:
                    await client.aclose()
            if response.status_code == 404:
                # The profile does not exist; no point asking again for a while.
                result = dict(EMPTY_RATINGS)
                _rating_cache[key] = (now + RATING_CACHE_SECONDS, result)
                return result
            response.raise_for_status()
            modes = response.json().get("modes") or {}
            result = {
                "solo_elo": (modes.get("rm_solo") or {}).get("rating"),
                "team_elo": (modes.get("rm_team") or {}).get("rating"),
            }
            _rating_cache[key] = (now + RATING_CACHE_SECONDS, result)
            return result
        except Exception:
            # Keep serving the last good value, and wait before retrying so a slow
            # or rate-limited AoE4World is not hit again on every page load.
            result = previous or dict(EMPTY_RATINGS)
            _rating_cache[key] = (now + RATING_RETRY_SECONDS, result)
            return result
        finally:
            _prune_rating_cache(now)


async def add_official_ratings(rows):
    async def enrich(row):
        item = publicize(row)
        item.update(await aoe4world_ratings(item.get("profile_id")))
        return item
    return await asyncio.gather(*(enrich(row) for row in rows))


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, http_client
    http_client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
    if DATABASE_URL:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=15)
        async with pool.acquire() as conn:
            # The only table this site creates. It is filled by the Discord bot's member sync;
            # creating it here just keeps the website working before the first sync has run.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS discord_member_profiles (
                    guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    username TEXT NOT NULL, avatar_url TEXT,
                    is_bot BOOLEAN NOT NULL DEFAULT FALSE,
                    is_present BOOLEAN NOT NULL DEFAULT TRUE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
        async with pool.acquire() as conn:
            # Website-only settings chosen by logged-in members (e.g. "Hide my profile").
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS website_member_settings (
                    user_id    TEXT PRIMARY KEY,
                    hidden     BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            # Queue alerts and the notification bell (the bot creates the same tables)
            await conn.execute("""CREATE TABLE IF NOT EXISTS member_alert_settings (
    user_id           TEXT PRIMARY KEY,
    enabled           BOOLEAN NOT NULL DEFAULT FALSE,
    queues            TEXT[] NOT NULL DEFAULT '{}',
    alert_when        TEXT NOT NULL DEFAULT 'one_away',
    quiet_enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    quiet_start       SMALLINT NOT NULL DEFAULT 0,
    quiet_end         SMALLINT NOT NULL DEFAULT 540,
    tz                TEXT NOT NULL DEFAULT 'UTC',
    via_dm            BOOLEAN NOT NULL DEFAULT TRUE,
    via_web           BOOLEAN NOT NULL DEFAULT TRUE,
    via_browser       BOOLEAN NOT NULL DEFAULT FALSE,
    muted_until       TIMESTAMPTZ,
    last_alert_at     TIMESTAMPTZ,
    dm_status         TEXT,
    dm_status_at      TIMESTAMPTZ,
    test_requested_at TIMESTAMPTZ,
    test_done_at      TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
)""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS member_notifications (
    id         BIGSERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    ref        TEXT,
    title      TEXT NOT NULL,
    body       TEXT,
    link       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    read_at    TIMESTAMPTZ
)""")
            await conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS member_notifications_once ON member_notifications (user_id, kind, ref)""")
            await conn.execute("""CREATE INDEX IF NOT EXISTS member_notifications_recent ON member_notifications (user_id, created_at DESC)""")
            # Members who have logged in to the website (notices are only made for them)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS website_users (
                    user_id     TEXT PRIMARY KEY,
                    first_login TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS member_achievements_seen (
                    user_id        TEXT NOT NULL,
                    achievement_id TEXT NOT NULL,
                    seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, achievement_id)
                )
            """)
            # Tournaments (run by the website; the bot starts each game in Discord)
            for stmt in TOURNAMENT_TABLES:
                await conn.execute(stmt)
        await refresh_hidden()
        refresher = asyncio.create_task(_hidden_refresh_loop())
        notifier = asyncio.create_task(_notification_loop())
        tourney = asyncio.create_task(_tournament_loop())
    yield
    if pool:
        refresher.cancel()
        notifier.cancel()
        tourney.cancel()
        await pool.close()
    await http_client.aclose()


async def refresh_hidden():
    try:
        rows = await fetch("SELECT user_id FROM website_member_settings WHERE hidden")
    except Exception:
        log.exception("Could not load hidden members")
        return
    HIDDEN_IDS.clear()
    HIDDEN_IDS.update(_ENV_HIDDEN | {str(r["user_id"]) for r in rows})


async def _hidden_refresh_loop():
    while True:
        await asyncio.sleep(60)
        await refresh_hidden()


app = FastAPI(title="The Cheese Club API", version="0.2.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


async def fetch(query: str, *args):
    if pool is None:
        raise HTTPException(status_code=503, detail="Database is not configured. Set DATABASE_URL in Railway Variables.")
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchval(query: str, *args, default=0):
    if pool is None:
        raise HTTPException(status_code=503, detail="Database is not configured. Set DATABASE_URL in Railway Variables.")
    async with pool.acquire() as conn:
        value = await conn.fetchval(query, *args)
        return default if value is None else value


def match_summary(row) -> dict:
    """Public fields of a match list entry. Raw Discord IDs are not included."""
    return {
        "id": row["id"],
        "queue_type": row["queue_type"],
        "result": row["result"],
        "player_count": len(row["player_ids"] or []),
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "tournament": bool(row.get("is_tournament")) if hasattr(row, "get") else False,
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    if pool is None:
        return {"status": "waiting_for_database"}
    try:
        await fetchval("SELECT 1")
        return {"status": "ok", "database": "connected"}
    except Exception:
        return {"status": "degraded", "database": "unavailable"}


@app.get("/api/overview")
async def overview():
    total_aoe_players = await fetchval("SELECT COUNT(DISTINCT user_id) FROM aoe_queue_stats")
    total_matches = await fetchval("SELECT COUNT(*) FROM aoe_matches WHERE result <> 'pending'")
    active_matches = await fetchval(
        "SELECT COUNT(*) FROM aoe_matches WHERE result = 'pending' AND created_at >= NOW() - INTERVAL '6 hours'")
    vc_rows = await fetch(f"""
        WITH {VC_TOTALS_CTE}
        SELECT COUNT(*) FILTER (WHERE total_secs > 0) AS members,
               COALESCE(SUM(total_secs), 0)::float AS seconds
        FROM vc
    """)
    return {
        "vc_players": vc_rows[0]["members"] if vc_rows else 0,
        "aoe_players": total_aoe_players,
        "completed_matches": total_matches,
        "active_matches": active_matches,
        "total_vc_seconds": float(vc_rows[0]["seconds"] or 0) if vc_rows else 0.0,
    }


@app.get("/api/leaderboards/aoe")
async def aoe_leaderboard(queue: str = "all", limit: int = 100, scope: str = "public"):
    limit = max(1, min(limit, 500))
    min_games = 5 if scope == "community" else 1
    name_and_links = """
        COALESCE(
            (SELECT aoe4_name FROM aoe4_links l WHERE l.user_id=aoe_queue_stats.user_id ORDER BY linked_at DESC LIMIT 1),
            (SELECT username FROM discord_member_profiles p WHERE p.user_id=aoe_queue_stats.user_id AND p.is_bot=FALSE ORDER BY updated_at DESC LIMIT 1),
            (SELECT username FROM vc_logs v WHERE v.user_id=aoe_queue_stats.user_id ORDER BY left_at DESC LIMIT 1),
            'Player ' || RIGHT(aoe_queue_stats.user_id,4)
        ) AS aoe4_name,
        (SELECT profile_id FROM aoe4_links l WHERE l.user_id=aoe_queue_stats.user_id ORDER BY linked_at DESC LIMIT 1) AS profile_id,
        (SELECT avatar_url FROM discord_member_profiles p WHERE p.user_id=aoe_queue_stats.user_id AND p.is_bot=FALSE ORDER BY updated_at DESC LIMIT 1) AS avatar_url
    """
    if queue == "all":
        rows = await fetch(f"""
            SELECT user_id, {name_and_links},
                   SUM(wins)::int AS wins,
                   SUM(losses)::int AS losses,
                   SUM(no_results)::int AS no_results,
                   SUM(wins + losses)::int AS games
            FROM aoe_queue_stats
            GROUP BY user_id
            HAVING SUM(wins + losses) >= $2
            ORDER BY SUM(wins)::int DESC, SUM(wins + losses) DESC
            LIMIT $1
        """, limit, min_games)
    else:
        rows = await fetch(f"""
            SELECT user_id, {name_and_links},
                   wins, losses, no_results, (wins + losses)::int AS games
            FROM aoe_queue_stats
            WHERE queue_type = $1 AND (wins + losses) >= $3
            ORDER BY wins DESC, (wins + losses) DESC
            LIMIT $2
        """, queue, limit, min_games)
    return await add_official_ratings(visible(rows))


@app.get("/api/leaderboards/vc")
async def vc_leaderboard(limit: int = 100, scope: str = "public"):
    limit = max(1, min(limit, 500))
    min_seconds = 360000 if scope == "community" else 36000
    rows = await fetch(f"""
        WITH {VC_TOTALS_CTE},
        profile AS (
            SELECT DISTINCT ON (user_id) user_id, username, avatar_url
            FROM discord_member_profiles WHERE is_bot=FALSE
            ORDER BY user_id, updated_at DESC
        ), last_vc_name AS (
            SELECT DISTINCT ON (user_id) user_id, username
            FROM vc_logs ORDER BY user_id, left_at DESC
        )
        SELECT vc.user_id, vc.total_secs,
               COALESCE(profile.username, last_vc_name.username,
                        'Player ' || RIGHT(vc.user_id, 4)) AS username,
               profile.avatar_url
        FROM vc
        LEFT JOIN profile ON profile.user_id=vc.user_id
        LEFT JOIN last_vc_name ON last_vc_name.user_id=vc.user_id
        WHERE vc.total_secs >= $2
        ORDER BY vc.total_secs DESC LIMIT $1
    """, limit, min_seconds)
    return [publicize(r) for r in visible(rows)]


@app.get("/api/members")
async def members(limit: int = 250, search: str = ""):
    limit = max(1, min(limit, 500))
    search = clean_search(search, 80)
    rows = await fetch(f"""
        WITH {VC_TOTALS_CTE},
        aoe AS (
            SELECT user_id, SUM(wins)::int AS wins, SUM(losses)::int AS losses,
                   SUM(no_results)::int AS no_results, ROUND(AVG(elo))::int AS elo,
                   SUM(wins + losses)::int AS games
            FROM aoe_queue_stats GROUP BY user_id
        ),
        profile AS (
            SELECT DISTINCT ON (user_id) user_id, username, avatar_url
            FROM discord_member_profiles WHERE is_bot=FALSE AND is_present=TRUE
            ORDER BY user_id, updated_at DESC
        ),
        last_vc_name AS (
            SELECT DISTINCT ON (user_id) user_id, username
            FROM vc_logs ORDER BY user_id, left_at DESC
        ),
        links AS (
            SELECT DISTINCT ON (user_id) user_id, aoe4_name, profile_id, bypassed
            FROM aoe4_links ORDER BY user_id, linked_at DESC
        ),
        ids AS (
            SELECT user_id FROM aoe
            UNION SELECT user_id FROM vc
            UNION SELECT user_id FROM links
        )
        SELECT ids.user_id,
               COALESCE(profile.username,last_vc_name.username,links.aoe4_name,
                        'Player ' || RIGHT(ids.user_id,4)) AS username,
               profile.avatar_url, links.aoe4_name, links.profile_id,
               COALESCE(links.bypassed,FALSE) AS bypassed,
               COALESCE(aoe.wins,0)::int AS wins, COALESCE(aoe.losses,0)::int AS losses,
               COALESCE(aoe.no_results,0)::int AS no_results, COALESCE(aoe.elo,1000)::int AS elo,
               COALESCE(aoe.games,0)::int AS games, COALESCE(vc.total_secs,0)::float AS vc_seconds,
               (COALESCE(aoe.games,0) >= 5) AS has_aoe,
               (COALESCE(vc.total_secs,0) >= 360000) AS has_vc
        FROM ids
        LEFT JOIN profile ON profile.user_id=ids.user_id
        LEFT JOIN last_vc_name ON last_vc_name.user_id=ids.user_id
        LEFT JOIN links ON links.user_id=ids.user_id
        LEFT JOIN aoe ON aoe.user_id=ids.user_id
        LEFT JOIN vc ON vc.user_id=ids.user_id
        WHERE (COALESCE(aoe.games,0) >= 5 OR COALESCE(vc.total_secs,0) >= 360000)
          AND ($1 = ''
               OR COALESCE(profile.username,last_vc_name.username,links.aoe4_name,'') ILIKE $3
               OR ids.user_id = $1)
        ORDER BY (COALESCE(aoe.games,0)>0) DESC, COALESCE(aoe.elo,0) DESC,
                 COALESCE(vc.total_secs,0) DESC, username ASC
        LIMIT $2
    """, search, limit, like_pattern(search))
    return [publicize(r) for r in visible(rows)]


@app.get("/api/matches/{match_id}")
async def match_detail(match_id: int):
    """Return a complete, JSON-safe match detail record for the website modal."""
    rows = await fetch("""
        SELECT id, queue_type, player_ids, team1_ids, team2_ids,
               result, civ_data, to_jsonb(aoe_matches)->'civ_bans' AS civ_bans,
               CASE WHEN result = 'pending' THEN to_jsonb(aoe_matches)->'active_state' END AS active_state,
               to_jsonb(aoe_matches)->'elo_snapshot' AS elo_snapshot,
               COALESCE((to_jsonb(aoe_matches)->>'is_tournament')::boolean, FALSE) AS is_tournament,
               created_at, finished_at
        FROM aoe_matches WHERE id=$1 LIMIT 1
    """, match_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Match not found")
    row = rows[0]

    def json_object(value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return {}
        return value if isinstance(value, dict) else {}

    civs = json_object(row["civ_data"])
    civ_bans = json_object(row["civ_bans"])
    team1 = [str(x) for x in (row["team1_ids"] or [])]
    team2 = [str(x) for x in (row["team2_ids"] or [])]
    player_ids = [str(x) for x in (row["player_ids"] or [])]
    pool = []
    # Running match: use the bot's live state, already stripped of anything Discord hasn't revealed
    live = live_view(json_object(row["active_state"])) if row["result"] == "pending" else None
    live_civs = {}
    if live:
        team1, team2, pool = live["team1"], live["team2"], live["pool"]
        live_civs = live["civs"]
        civs = {uid: c["civ"] for uid, c in live_civs.items() if c.get("civ")}
        bans = live["bans"] or {}
        if bans.get("status") == "skipped":
            civ_bans = {"skipped": True}
        elif bans.get("status") == "revealed":
            civ_bans = {"team1": bans.get("team1") or [], "team2": bans.get("team2") or []}
        else:
            civ_bans = {}
    # AoE4World ratings frozen when the queue popped (exact rating at match time)
    snapshot = live["elo_tags"] if live else json_object(row["elo_snapshot"])
    all_ids = list(dict.fromkeys(team1 + team2 + pool + player_ids))

    players = {}
    if all_ids:
        profile_rows = await fetch("""
            SELECT DISTINCT ON (user_id) user_id, username, avatar_url
            FROM discord_member_profiles WHERE user_id = ANY($1::text[])
            ORDER BY user_id, updated_at DESC
        """, all_ids)
        vc_rows = await fetch("""
            SELECT DISTINCT ON (user_id) user_id, username
            FROM vc_logs WHERE user_id = ANY($1::text[])
            ORDER BY user_id, left_at DESC
        """, all_ids)
        # Community rating for this match's queue (the same figure "Biggest upset" uses),
        # falling back to the player's average across queues if they have none for it.
        elo_rows = await fetch("""
            SELECT user_id,
                   ROUND(AVG(elo) FILTER (WHERE LOWER(queue_type) = LOWER($2)))::int AS queue_elo,
                   ROUND(AVG(elo))::int AS any_elo
            FROM aoe_queue_stats WHERE user_id = ANY($1::text[])
            GROUP BY user_id
        """, all_ids, str(row["queue_type"] or ""))
        link_rows = await fetch("""
            SELECT DISTINCT ON (user_id) user_id, profile_id FROM aoe4_links
            WHERE user_id = ANY($1::text[]) ORDER BY user_id, linked_at DESC
        """, all_ids)
        # Official AoE4World rating: Solo for 1v1, Team (TG) for everything else.
        is_solo = str(row["queue_type"] or "").lower() == "1v1"
        official_type = "Solo" if is_solo else "TG"
        profile_ids = {str(r["user_id"]): r["profile_id"] for r in link_rows}
        official = dict(zip(profile_ids, await asyncio.gather(*(aoe4world_ratings(pid) for pid in profile_ids.values()))))
        for uid in all_ids:
            players[uid] = {"user_id": to_public_id(uid), "id_hint": id_hint(uid),
                            "username": None, "avatar_url": None, "elo": None,
                            "official_elo": None, "official_elo_type": official_type}
        for r in profile_rows:
            players[str(r["user_id"])].update(username=r["username"], avatar_url=r["avatar_url"])
        for r in vc_rows:
            item = players[str(r["user_id"])]
            item["username"] = item["username"] or r["username"]
        for r in elo_rows:
            players[str(r["user_id"])]["elo"] = r["queue_elo"] if r["queue_elo"] is not None else r["any_elo"]
        for uid, ratings in official.items():
            players[uid]["official_elo"] = ratings.get("solo_elo" if is_solo else "team_elo")
        for uid, value in snapshot.items():
            if uid in players and _rating_number(value) is not None:
                players[uid]["official_elo"] = _rating_number(value)
                players[uid]["official_elo_at_match"] = True
        for uid, item in players.items():
            item["username"] = item["username"] or f"Player {uid[-4:]}"
            civ_value = civs.get(uid)
            if isinstance(civ_value, dict):
                civ_value = civ_value.get("name") or civ_value.get("civilization") or civ_value.get("civ")
            item["civ"] = str(civ_value) if civ_value else "Not recorded"
            if uid in live_civs and live_civs[uid]["status"] in ("ready", "picking"):
                item["civ"] = "Civ chosen · hidden until reveal" if live_civs[uid]["status"] == "ready" else "Choosing a civ…"
                item["civ_status"] = live_civs[uid]["status"]
            if uid in HIDDEN_IDS:
                item.update(user_id=None, id_hint="", username="Hidden player", avatar_url=None, elo=None, official_elo=None)

    if live:
        for cap in live["captains"]:
            if cap in players and cap not in HIDDEN_IDS:
                players[cap]["captain"] = True

    return {
        "id": row["id"],
        "queue_type": row["queue_type"],
        "result": row["result"],
        "tournament": bool(row["is_tournament"]),
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "player_count": len(all_ids),
        "civ_bans": civ_bans,
        "team1": [players[x] for x in team1 if x in players],
        "team2": [players[x] for x in team2 if x in players],
        "players": [players[x] for x in player_ids if x in players],
        "pool": [players[x] for x in pool if x in players],
        "live": {
            "phase": live["phase"], "phase_label": live["phase_label"], "map": live["map"],
            "civs_revealed": live["civs_revealed"], "bans": live["bans"],
            "team1_locked": live["team1_locked"], "team2_locked": live["team2_locked"],
            "captains": [to_public_id(c) for c in live["captains"] if c],
        } if live else None,
    }


@app.get("/api/matches")
async def matches(limit: int = 100, search: str = ""):
    """List matches, optionally searching by match ID, Discord name or AoE4 name."""
    limit = max(1, min(limit, 250))
    search = clean_search(search, 100)
    rows = await fetch("""
        SELECT m.id, m.queue_type, m.player_ids, m.result, m.created_at, m.finished_at,
               COALESCE((to_jsonb(m)->>'is_tournament')::boolean, FALSE) AS is_tournament
        FROM aoe_matches AS m
        WHERE ($2 = ''
            OR m.id::text ILIKE $3
            OR EXISTS (
                SELECT 1 FROM discord_member_profiles AS p
                WHERE p.user_id = ANY(m.player_ids) AND p.username ILIKE $3
            )
            OR EXISTS (
                SELECT 1 FROM vc_logs AS v
                WHERE v.user_id = ANY(m.player_ids) AND v.username ILIKE $3
            )
            OR EXISTS (
                SELECT 1 FROM aoe4_links AS l
                WHERE l.user_id = ANY(m.player_ids) AND l.aoe4_name ILIKE $3
            )
        )
        ORDER BY m.created_at DESC
        LIMIT $1
    """, limit, search, like_pattern(search))
    return [match_summary(r) for r in rows]


@app.get("/api/players/{player_id}")
async def player_profile(player_id: str):
    user_id = from_public_id(player_id)
    if user_id is None or user_id in HIDDEN_IDS:
        raise HTTPException(status_code=404, detail="Player not found")
    stats = await fetch("""
        SELECT queue_type, SUM(wins)::int AS wins, SUM(losses)::int AS losses,
               SUM(no_results)::int AS no_results, ROUND(AVG(elo))::int AS elo
        FROM aoe_queue_stats WHERE user_id=$1 GROUP BY queue_type ORDER BY queue_type
    """, user_id)
    vc_seconds = await fetchval(f"""
        WITH {VC_TOTALS_CTE}
        SELECT total_secs FROM vc WHERE user_id=$1
    """, user_id, default=0)
    links = await fetch("""
        SELECT aoe4_name, profile_id, bypassed, linked_at FROM aoe4_links
        WHERE user_id=$1 ORDER BY linked_at DESC LIMIT 1
    """, user_id)
    profile = await fetch("""
        SELECT username, avatar_url FROM discord_member_profiles
        WHERE user_id=$1 AND is_bot=FALSE ORDER BY updated_at DESC LIMIT 1
    """, user_id)
    recent = await fetch("""
        SELECT id, queue_type, player_ids, result, created_at, finished_at FROM aoe_matches
        WHERE $1 = ANY(player_ids) ORDER BY created_at DESC LIMIT 10
    """, user_id)
    official_ratings = await aoe4world_ratings(links[0]["profile_id"] if links else None)
    return {
        "user_id": to_public_id(user_id),
        "id_hint": id_hint(user_id),
        **official_ratings,
        "username": profile[0]["username"] if profile else (links[0]["aoe4_name"] if links else f"Player {user_id[-4:]}"),
        "avatar_url": profile[0]["avatar_url"] if profile else None,
        "aoe4_link": dict(links[0]) if links else None,
        "queue_stats": [dict(r) for r in stats],
        "vc_seconds": float(vc_seconds or 0),
        "recent_matches": [match_summary(r) for r in recent],
    }


# ---------------------------------------------------------------------------
# Match history analysis (player insights, achievements, civ stats, activity)
# ---------------------------------------------------------------------------
MATCH_CACHE_SECONDS = 10
_match_cache = {"at": 0.0, "rows": None}
_match_lock = asyncio.Lock()

CIV_NAMES = [
    "English", "French", "Holy Roman Empire", "Rus", "Abbasid Dynasty", "Chinese", "Delhi Sultanate",
    "Mongols", "Japanese", "Byzantines", "Ottomans", "Malians", "Jeanne d'Arc", "Ayyubids",
    "Order of the Dragon", "Zhu Xi's Legacy", "Tughlaq Dynasty", "Sengoku Daimyo", "Macedonian Dynasty",
    "Knights Templar", "Golden Horde", "House of Lancaster", "Jin Dynasty", "Vikings", "Scots", "Random",
]
_CIV_ALIASES = {name.lower(): name for name in CIV_NAMES}
_CIV_ALIASES.update({
    "hre": "Holy Roman Empire", "roman empire": "Holy Roman Empire", "abbasid": "Abbasid Dynasty",
    "delhi": "Delhi Sultanate", "mongol": "Mongols", "byzantine": "Byzantines", "ottoman": "Ottomans",
    "malian": "Malians", "jeanne darc": "Jeanne d'Arc", "ayyubid": "Ayyubids", "zhu xis legacy": "Zhu Xi's Legacy",
    "tughlaq": "Tughlaq Dynasty", "sengoku": "Sengoku Daimyo", "macedonian": "Macedonian Dynasty",
    "templar": "Knights Templar", "templars": "Knights Templar", "templar knights": "Knights Templar",
    "lancaster": "House of Lancaster", "jin": "Jin Dynasty", "viking": "Vikings", "scottish": "Scots",
})
_CIV_ALIASES_LONGEST_FIRST = sorted(_CIV_ALIASES, key=len, reverse=True)


def canonical_civ(value) -> Optional[str]:
    """Normalise the many ways the bot may have saved a civilization name."""
    if isinstance(value, dict):
        value = value.get("name") or value.get("civilization") or value.get("civ")
    if not value:
        return None
    raw = re.sub(r"\s+", " ", str(value).replace("\u2019", "'").replace("\u2018", "'")).strip()
    key = raw.lower()
    if key in ("", "not recorded", "none", "unknown"):
        return None
    if key in _CIV_ALIASES:
        return _CIV_ALIASES[key]
    words = " " + re.sub(r"[^a-z0-9' ]+", " ", key) + " "
    words = re.sub(r"\s+", " ", words)
    for alias in _CIV_ALIASES_LONGEST_FIRST:
        if len(alias) >= 3 and f" {alias} " in words:
            return _CIV_ALIASES[alias]
    return raw


def _json_obj(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, (dict, list)) else {}


def _ban_list(bans) -> list:
    """Every civilization named in a match's ban record."""
    if not bans or (isinstance(bans, dict) and bans.get("skipped")):
        return []
    values = bans if isinstance(bans, list) else [v for k, v in bans.items() if k != "skipped"]
    out = []
    for v in values:
        for item in (v if isinstance(v, list) else [v]):
            civ = canonical_civ(item)
            if civ:
                out.append(civ)
    return out


async def load_matches() -> list:
    """All matches, oldest first, cached briefly. Small communities make this cheap."""
    now = time.monotonic()
    if _match_cache["rows"] is not None and now - _match_cache["at"] < MATCH_CACHE_SECONDS:
        return _match_cache["rows"]
    async with _match_lock:
        if _match_cache["rows"] is not None and time.monotonic() - _match_cache["at"] < MATCH_CACHE_SECONDS:
            return _match_cache["rows"]
        rows = await fetch("""
            SELECT id, queue_type, player_ids, team1_ids, team2_ids, result, civ_data,
                   to_jsonb(aoe_matches)->'civ_bans' AS civ_bans,
                   CASE WHEN result = 'pending' THEN to_jsonb(aoe_matches)->'active_state' END AS active_state,
                   to_jsonb(aoe_matches)->'elo_snapshot' AS elo_snapshot,
                   COALESCE((to_jsonb(aoe_matches)->>'is_tournament')::boolean, FALSE) AS is_tournament,
                   created_at, finished_at
            FROM aoe_matches ORDER BY created_at ASC, id ASC
        """)
        matches = []
        for r in rows:
            civs = _json_obj(r["civ_data"])
            matches.append({
                "id": r["id"],
                "queue": str(r["queue_type"] or "").lower(),
                "queue_type": r["queue_type"],
                "team1": [str(x) for x in (r["team1_ids"] or [])],
                "team2": [str(x) for x in (r["team2_ids"] or [])],
                "players": [str(x) for x in (r["player_ids"] or [])],
                "result": str(r["result"] or "").lower(),
                "civs": {str(k): canonical_civ(v) for k, v in civs.items()} if isinstance(civs, dict) else {},
                "bans": _ban_list(_json_obj(r["civ_bans"])),
                "state": _json_obj(r["active_state"]) if r["active_state"] else {},
                "elo_snapshot": _json_obj(r["elo_snapshot"]) if r["elo_snapshot"] else {},
                "created_at": r["created_at"],
                "finished_at": r["finished_at"],
                "tournament": bool(r["is_tournament"]),
            })
        _match_cache.update(at=time.monotonic(), rows=matches)
        return matches


async def load_club_matches() -> list:
    """Matches that count for club stats: tournament games are left out (they
    still appear in match history and live, with a Tournament label)."""
    return [m for m in await load_matches() if not m["tournament"]]


# ---------------------------------------------------------------------------
# Live match state from the Discord bot
# The bot saves each running match's full state, including secret picks. Only
# what Discord has already revealed may leave the server:
#   - civ picks: only after BOTH captains lock in (before that: ready / picking)
#   - civ bans:  only after both sides lock in, or when bans were skipped
# ---------------------------------------------------------------------------
PHASE_LABELS = {
    "team_size_pick": "Choosing team sizes",
    "coin_flip": "Coin flip for first pick",
    "draft": "Drafting teams",
    "teams_confirm": "Confirming teams",
    "map_veto_coinflip": "Coin flip for map veto",
    "map_veto": "Map veto",
    "civ_ban": "Civilization bans",
    "civ_select": "Picking civilizations",
    "pre_match": "Civs revealed, about to start",
    "in_match": "Game in progress",
}


def live_view(state: dict) -> Optional[dict]:
    """Public, spoiler-free view of a running match's state (Discord IDs still raw)."""
    if not state:
        return None
    phase = str(state.get("phase") or "")
    ids = lambda key: [str(x) for x in (state.get(key) or [])]
    is_1v1 = str(state.get("queue_type") or "").lower() == "1v1"

    revealed = bool(state.get("cap1_locked") and state.get("cap2_locked")) or phase in ("pre_match", "in_match")
    picks = {str(k): v for k, v in (state.get("civ_picks") or {}).items()}
    civs = {}
    for uid in ids("team1_ids") + ids("team2_ids"):
        if revealed:
            civs[uid] = {"civ": canonical_civ(picks.get(uid)) or picks.get(uid), "status": "revealed"}
        elif phase == "civ_select":
            civs[uid] = {"civ": None, "status": "ready" if uid in picks else "picking"}

    if state.get("bans_skipped"):
        bans = {"status": "skipped"}
    else:
        if is_1v1:
            locked1, locked2 = bool(state.get("civ_ban_p1_locked")), bool(state.get("civ_ban_p2_locked"))
            lists = ([state["civ_ban_p1_choice"]] if state.get("civ_ban_p1_choice") else [],
                     [state["civ_ban_p2_choice"]] if state.get("civ_ban_p2_choice") else [])
        else:
            locked1, locked2 = bool(state.get("ban_locked_t1")), bool(state.get("ban_locked_t2"))
            lists = (list(state.get("civ_bans_t1") or []), list(state.get("civ_bans_t2") or []))
        if locked1 and locked2:
            bans = {"status": "revealed", "team1": lists[0], "team2": lists[1]}
        elif phase == "civ_ban":
            bans = {"status": "choosing", "team1_locked": locked1, "team2_locked": locked2}
        else:
            bans = None

    pending = state.get("pending_action")
    label = PHASE_LABELS.get(phase, "Setting up")
    if is_1v1 and phase == "coin_flip":
        label = "Setting up the match"          # 1v1 has no draft, so no first-pick flip
    maps_left = len(state.get("map_pool") or [])
    if phase == "map_veto" and maps_left > 1:
        label = f"Map veto · {maps_left} maps left"
    if pending == "victory":
        label = "Result being confirmed"
    elif pending == "cancel":
        label = "Cancellation being confirmed"
    return {
        "phase": phase, "phase_label": label,
        "team1": ids("team1_ids"), "team2": ids("team2_ids"), "pool": ids("remaining_ids"),
        "captains": [str(state.get("captain1_id") or ""), str(state.get("captain2_id") or "")],
        "team1_locked": bool(state.get("cap1_locked")), "team2_locked": bool(state.get("cap2_locked")),
        "civs": civs, "civs_revealed": revealed, "bans": bans,
        "map": state.get("final_map") if is_1v1 else None,
        "elo_tags": {str(k): v for k, v in (state.get("elo_tags") or {}).items()},
    }


def _rating_number(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def decided(m) -> bool:
    return m["result"] in ("team1", "team2")


def player_outcome(m, uid):
    """'win', 'loss', or None if the player is not on a team in a decided match."""
    if not decided(m):
        return None
    side = 1 if uid in m["team1"] else 2 if uid in m["team2"] else 0
    if not side:
        return None
    return "win" if m["result"] == f"team{side}" else "loss"


def _when(m):
    return m["finished_at"] or m["created_at"]


async def people(ids) -> dict:
    """Display details for Discord user IDs: public ID, masked hint, name and avatar."""
    ids = [str(i) for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    profile_rows = await fetch("""
        SELECT DISTINCT ON (user_id) user_id, username, avatar_url FROM discord_member_profiles
        WHERE user_id = ANY($1::text[]) AND is_bot=FALSE ORDER BY user_id, updated_at DESC
    """, ids)
    vc_rows = await fetch("""
        SELECT DISTINCT ON (user_id) user_id, username FROM vc_logs
        WHERE user_id = ANY($1::text[]) ORDER BY user_id, left_at DESC
    """, ids)
    link_rows = await fetch("""
        SELECT DISTINCT ON (user_id) user_id, aoe4_name FROM aoe4_links
        WHERE user_id = ANY($1::text[]) ORDER BY user_id, linked_at DESC
    """, ids)
    names = {}
    for rows, key in ((link_rows, "aoe4_name"), (vc_rows, "username"), (profile_rows, "username")):
        for r in rows:
            if r[key]:
                names[str(r["user_id"])] = r[key]
    avatars = {str(r["user_id"]): r["avatar_url"] for r in profile_rows}
    out = {}
    for uid in ids:
        if uid in HIDDEN_IDS:
            out[uid] = {"user_id": None, "id_hint": "", "username": "Hidden player", "avatar_url": None}
        else:
            out[uid] = {"user_id": to_public_id(uid), "id_hint": id_hint(uid),
                        "username": names.get(uid) or f"Player {uid[-4:]}", "avatar_url": avatars.get(uid)}
    return out


ACHIEVEMENTS = [
    # id, icon, name, description, metric, target
    ("first_win", "⚔️", "First Blood", "Win your first community match.", "wins", 1),
    ("veteran", "🛡️", "Veteran", "Play 50 community matches.", "games", 50),
    ("centurion", "🏰", "Centurion", "Play 100 community matches.", "games", 100),
    ("on_fire", "🔥", "On Fire", "Win 5 matches in a row.", "best_streak", 5),
    ("unstoppable", "⚡", "Unstoppable", "Win 10 matches in a row.", "best_streak", 10),
    ("specialist", "🎯", "Civ Specialist", "Win 10 matches with one civilization.", "best_civ_wins", 10),
    ("well_travelled", "🧭", "Well Travelled", "Play 8 different civilizations.", "civs_played", 8),
    ("duelist", "🤺", "Duelist", "Play 25 1v1 matches.", "duels", 25),
    ("team_player", "🤝", "Team Player", "Play 25 team matches (2v2 and up).", "team_games", 25),
    ("regular", "🎙️", "Regular", "Spend 100 hours in voice.", "voice_hours", 100),
    ("voice_of_the_club", "📣", "Voice of the Club", "Spend 1,000 hours in voice.", "voice_hours", 1000),
    ("officially_ranked", "✅", "Officially Ranked", "Link your AoE4 account.", "linked", 1),
]


def compute_insights(uid: str, matches: list) -> dict:
    """Everything the player page derives from match history for one player."""
    record = []                    # (when, outcome, match) for decided matches, oldest first
    civs, rivals, mates = {}, {}, {}
    duels = team_games = 0
    for m in matches:
        outcome = player_outcome(m, uid)
        if outcome is None:
            continue
        record.append((_when(m), outcome, m))
        if m["queue"] == "1v1":
            duels += 1
        else:
            team_games += 1
        civ = m["civs"].get(uid)
        if civ:
            c = civs.setdefault(civ, {"civ": civ, "games": 0, "wins": 0, "losses": 0})
            c["games"] += 1
            c["wins" if outcome == "win" else "losses"] += 1
        mine, theirs = (m["team1"], m["team2"]) if uid in m["team1"] else (m["team2"], m["team1"])
        for other in theirs:
            r = rivals.setdefault(other, {"games": 0, "wins": 0, "losses": 0})
            r["games"] += 1
            r["wins" if outcome == "win" else "losses"] += 1
        for other in mine:
            if other != uid:
                t = mates.setdefault(other, {"games": 0, "wins": 0, "losses": 0})
                t["games"] += 1
                t["wins" if outcome == "win" else "losses"] += 1

    best = run = 0
    for _, outcome, _m in record:
        run = run + 1 if outcome == "win" else 0
        best = max(best, run)
    current = 0
    current_kind = None
    for _, outcome, _m in reversed(record):
        if current_kind is None:
            current_kind = outcome
        if outcome != current_kind:
            break
        current += 1

    civ_list = sorted(civs.values(), key=lambda c: (-c["games"], -c["wins"], c["civ"]))
    for c in civ_list:
        c["win_rate"] = round(100 * c["wins"] / c["games"], 1) if c["games"] else None
    return {
        "decided_matches": len(record),
        "wins_in_matches": sum(1 for r in record if r[1] == "win"),
        "duels": duels,
        "team_games": team_games,
        "best_streak": best,
        "current_streak": {"type": current_kind, "count": current} if record else None,
        "civs": civ_list,
        "favourite_civ": civ_list[0]["civ"] if civ_list else None,
        "best_civ_wins": max((c["wins"] for c in civ_list), default=0),
        "rivals": rivals,
        "teammates": mates,
        "form": [r[1][0].upper() for r in record[-10:]],   # e.g. W W L W …, oldest to newest
    }


def build_achievements(metrics: dict) -> list:
    out = []
    for key, icon, name, desc, metric, target in ACHIEVEMENTS:
        value = metrics.get(metric, 0) or 0
        out.append({"id": key, "icon": icon, "name": name, "description": desc,
                    "earned": value >= target, "progress": min(value, target), "target": target})
    return out


@app.get("/api/players/{player_id}/insights")
async def player_insights(player_id: str):
    user_id = from_public_id(player_id)
    if user_id is None or user_id in HIDDEN_IDS:
        raise HTTPException(status_code=404, detail="Player not found")
    data = await build_player_insights(user_id)
    data["trophies"] = []
    if await fetchval("SELECT to_regclass('tournaments') IS NOT NULL", default=False):
        data["trophies"] = [dict(r) for r in await fetch("""
            WITH mine AS (SELECT 'T' || team_id AS eid FROM tournament_team_members WHERE user_id=$1 AND status='accepted')
            SELECT id, name, finished_at, format,
                   (winner_id=$1 OR winner_id IN (SELECT eid FROM mine)) AS champion,
                   (third_id=$1 OR third_id IN (SELECT eid FROM mine)) AS third FROM tournaments
            WHERE status='finished' AND (winner_id=$1 OR runner_up_id=$1 OR third_id=$1
                  OR winner_id IN (SELECT eid FROM mine) OR runner_up_id IN (SELECT eid FROM mine))
            ORDER BY finished_at DESC
        """, user_id)]
    return data


async def build_player_insights(user_id: str) -> dict:
    """Insights for one player (used by public player pages and the member dashboard)."""
    matches = await load_club_matches()
    ins = compute_insights(user_id, matches)

    totals = await fetch("""
        SELECT COALESCE(SUM(wins),0)::int AS wins, COALESCE(SUM(losses),0)::int AS losses
        FROM aoe_queue_stats WHERE user_id=$1
    """, user_id)
    vc_seconds = await fetchval(f"WITH {VC_TOTALS_CTE} SELECT total_secs FROM vc WHERE user_id=$1", user_id, default=0)
    linked = await fetchval("SELECT COUNT(*) FROM aoe4_links WHERE user_id=$1", user_id, default=0)
    wins = max(totals[0]["wins"], ins["wins_in_matches"]) if totals else ins["wins_in_matches"]
    games = (totals[0]["wins"] + totals[0]["losses"]) if totals else 0
    games = max(games, ins["decided_matches"])

    metrics = {"wins": wins, "games": games, "best_streak": ins["best_streak"],
               "best_civ_wins": ins["best_civ_wins"], "civs_played": len(ins["civs"]),
               "duels": ins["duels"], "team_games": ins["team_games"],
               "voice_hours": int(float(vc_seconds or 0) // 3600), "linked": 1 if linked else 0}

    def top(table, n=5):
        ranked = sorted(table.items(), key=lambda kv: (-kv[1]["games"], -kv[1]["wins"]))
        return [(uid, stats) for uid, stats in ranked if uid not in HIDDEN_IDS][:n]

    top_rivals, top_mates = top(ins["rivals"]), top(ins["teammates"])
    names = await people([u for u, _ in top_rivals + top_mates])
    recent = [m for m in reversed(matches) if user_id in m["players"]][:15]
    return {
        "user_id": to_public_id(user_id),
        "best_streak": ins["best_streak"],
        "current_streak": ins["current_streak"],
        "form": ins["form"],
        "favourite_civ": ins["favourite_civ"],
        "civs": ins["civs"],
        "rivals": [{**names[u], **s} for u, s in top_rivals],
        "teammates": [{**names[u], **s} for u, s in top_mates],
        "achievements": build_achievements(metrics),
        "recent_matches": [{
            "id": m["id"], "queue_type": m["queue_type"], "result": m["result"],
            "outcome": player_outcome(m, user_id), "civ": m["civs"].get(user_id),
            "created_at": m["created_at"], "player_count": len(m["players"]),
        } for m in recent],
    }


@app.get("/api/civs")
async def civ_stats(queue: str = "all"):
    queue = (queue or "all").lower()
    matches = [m for m in await load_club_matches() if decided(m) and (queue == "all" or m["queue"] == queue)]
    stats = {}

    def entry(civ):
        return stats.setdefault(civ, {"civ": civ, "picks": 0, "wins": 0, "losses": 0, "bans": 0})

    counted = 0
    for m in matches:
        if m["civs"]:
            counted += 1
        for uid, civ in m["civs"].items():
            outcome = player_outcome(m, uid)
            if not civ or outcome is None:
                continue
            e = entry(civ)
            e["picks"] += 1
            e["wins" if outcome == "win" else "losses"] += 1
        for civ in m["bans"]:
            entry(civ)["bans"] += 1
    total_picks = sum(e["picks"] for e in stats.values())
    rows = []
    for e in stats.values():
        e["win_rate"] = round(100 * e["wins"] / e["picks"], 1) if e["picks"] else None
        e["pick_share"] = round(100 * e["picks"] / total_picks, 1) if total_picks else 0.0
        rows.append(e)
    rows.sort(key=lambda e: (-e["picks"], -e["wins"], e["civ"]))
    return {"queue": queue, "matches": len(matches), "matches_with_civs": counted,
            "total_picks": total_picks, "min_games_for_win_rate": 5, "civs": rows}


async def voice_now() -> dict:
    """Members in the server's public voice channels right now, grouped by channel.
    Empty until the bot version that records live voice presence is running."""
    exists = await fetchval("SELECT to_regclass('vc_live') IS NOT NULL", default=False)
    if not exists:
        return {"available": False, "total": 0, "channels": []}
    rows = await fetch("""
        SELECT user_id, username, avatar_url, channel_id, channel_name, joined_at
        FROM vc_live WHERE updated_at >= NOW() - INTERVAL '10 minutes'
        ORDER BY channel_name, joined_at
    """)
    channels = {}
    for r in rows:
        uid = str(r["user_id"])
        if uid in HIDDEN_IDS:
            continue
        ch = channels.setdefault(str(r["channel_id"]), {"name": r["channel_name"], "members": []})
        ch["members"].append({"user_id": to_public_id(uid), "username": r["username"],
                              "avatar_url": r["avatar_url"], "joined_at": r["joined_at"]})
    ordered = sorted(channels.values(), key=lambda c: (-len(c["members"]), c["name"].lower()))
    return {"available": True, "total": sum(len(c["members"]) for c in ordered), "channels": ordered}


@app.get("/api/activity")
async def activity():
    all_matches = await load_matches()
    matches = [m for m in all_matches if not m["tournament"]]
    now = datetime.now(timezone.utc)
    live_window, day, week = now - timedelta(hours=6), now - timedelta(days=1), now - timedelta(days=7)

    def aware(dt):
        return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    live = [m for m in all_matches if m["result"] == "pending" and aware(m["created_at"]) and aware(m["created_at"]) >= live_window]
    finished_today = [m for m in matches if decided(m) and aware(_when(m)) and aware(_when(m)) >= day]
    this_week = [m for m in matches if aware(m["created_at"]) and aware(m["created_at"]) >= week]
    active_players = {p for m in this_week for p in m["players"]}

    week_wins, week_civs, week_games, pairs = {}, {}, {}, {}
    week_decided = [m for m in this_week if decided(m)]
    for m in week_decided:
        for uid in m["team1"] + m["team2"]:
            week_games[uid] = week_games.get(uid, 0) + 1
            if player_outcome(m, uid) == "win":
                week_wins[uid] = week_wins.get(uid, 0) + 1
            civ = m["civs"].get(uid)
            if civ:
                week_civs[civ] = week_civs.get(civ, 0) + 1
        # Rivalries: every pair of players who were on opposite teams
        for a in m["team1"]:
            for b in m["team2"]:
                if a in HIDDEN_IDS or b in HIDDEN_IDS:
                    continue
                key = tuple(sorted((a, b)))
                r = pairs.setdefault(key, {"games": 0, "wins": {key[0]: 0, key[1]: 0}, "last": None})
                r["games"] += 1
                winner = a if player_outcome(m, a) == "win" else b
                r["wins"][winner] += 1
                r["last"] = _when(m)

    # Biggest upset: the win with the largest gap between the losing and winning teams'
    # average rating. Uses the AoE4World ratings the bot froze when the queue popped
    # (exact, at match time) when every player has one; otherwise the community rating
    # for that queue (current value, as the bot does not keep its history).
    elo_rows = await fetch("SELECT user_id, LOWER(queue_type) AS queue, ROUND(AVG(elo))::int AS elo FROM aoe_queue_stats GROUP BY user_id, LOWER(queue_type)")
    elo = {(str(r["user_id"]), r["queue"]): r["elo"] for r in elo_rows if r["elo"] is not None}
    upset = None
    for m in week_decided:
        winners, losers = (m["team1"], m["team2"]) if m["result"] == "team1" else (m["team2"], m["team1"])
        snap = {u: _rating_number(v) for u, v in (m["elo_snapshot"] or {}).items()}
        if winners and losers and all(snap.get(u) for u in winners + losers):
            w, l, basis = [snap[u] for u in winners], [snap[u] for u in losers], "aoe4world"
        else:
            w = [elo[(u, m["queue"])] for u in winners if (u, m["queue"]) in elo]
            l = [elo[(u, m["queue"])] for u in losers if (u, m["queue"]) in elo]
            basis = "community"
            if len(w) != len(winners) or len(l) != len(losers):
                continue
        if not w or not l:
            continue
        gap = round(sum(l) / len(l) - sum(w) / len(w))
        if gap >= 50 and (upset is None or gap > upset[1]):
            upset = (m, gap, basis)

    streaks = {}
    for m in matches:            # oldest first, so the final value is the current run of wins
        if not decided(m):
            continue
        for uid in m["team1"] + m["team2"]:
            streaks[uid] = streaks.get(uid, 0) + 1 if player_outcome(m, uid) == "win" else 0

    top_winner = max(((u, w) for u, w in week_wins.items() if u not in HIDDEN_IDS), key=lambda x: x[1], default=None)
    hot = max(((u, s) for u, s in streaks.items() if u not in HIDDEN_IDS and s >= 2), key=lambda x: x[1], default=None)
    top_civ = max(week_civs.items(), key=lambda x: x[1], default=None)
    busiest = max(((u, g) for u, g in week_games.items() if u not in HIDDEN_IDS), key=lambda x: x[1], default=None)
    rivalry = max(((k, v) for k, v in pairs.items() if v["games"] >= 2),
                  key=lambda kv: (kv[1]["games"], kv[1]["last"] or datetime.min.replace(tzinfo=timezone.utc)), default=None)

    live_views = {m["id"]: live_view(m["state"]) for m in live}
    ids = [p for m in live for p in m["team1"] + m["team2"] + m["players"]]
    ids += [p for v in live_views.values() if v for p in v["team1"] + v["team2"] + v["pool"]]
    ids += [u for u in (top_winner[0] if top_winner else None, hot[0] if hot else None) if u]
    ids += [busiest[0]] if busiest else []
    ids += list(rivalry[0]) if rivalry else []
    ids += (upset[0]["team1"] + upset[0]["team2"]) if upset else []
    names = await people(ids)
    return {
        "in_progress": [{
            "id": m["id"], "queue_type": m["queue_type"], "started_at": m["created_at"],
            "team1": [names[u] for u in (live_views[m["id"]] or m)["team1"] if u in names],
            "team2": [names[u] for u in (live_views[m["id"]] or m)["team2"] if u in names],
            "players": [names[u] for u in m["players"] if u in names],
            "player_count": len(m["players"]),
            "phase": live_views[m["id"]]["phase"] if live_views[m["id"]] else None,
            "phase_label": live_views[m["id"]]["phase_label"] if live_views[m["id"]] else None,
            "map": live_views[m["id"]]["map"] if live_views[m["id"]] else None,
            "pool_count": len(live_views[m["id"]]["pool"]) if live_views[m["id"]] else 0,
        } for m in reversed(live)],
        "voice_now": await voice_now(),
        "played_last_24h": len(finished_today),
        "active_players_7d": len(active_players),
        "week": {
            "most_wins": {**names[top_winner[0]], "wins": top_winner[1]} if top_winner else None,
            "hottest_streak": {**names[hot[0]], "streak": hot[1]} if hot else None,
            "most_played_civ": {"civ": top_civ[0], "picks": top_civ[1]} if top_civ else None,
            "most_active": {**names[busiest[0]], "games": busiest[1]} if busiest else None,
            "rivalry": {
                "players": [{**names[u], "wins": rivalry[1]["wins"][u]} for u in rivalry[0]],
                "games": rivalry[1]["games"],
            } if rivalry else None,
            "biggest_upset": {
                "match_id": upset[0]["id"], "queue_type": upset[0]["queue_type"], "rating_gap": upset[1],
                "rating_basis": upset[2],
                "winners": [names[u] for u in (upset[0]["team1"] if upset[0]["result"] == "team1" else upset[0]["team2"]) if u in names],
                "losers": [names[u] for u in (upset[0]["team2"] if upset[0]["result"] == "team1" else upset[0]["team1"]) if u in names],
            } if upset else None,
        },
    }


# ---------------------------------------------------------------------------
# Accounts: "Log in with Discord"
# There are no passwords. Discord confirms who the visitor is and gives us their
# Discord ID, the same ID all bot data uses, so a member's matches, voice time and
# AoE4 link belong to their account automatically. Logging in for the first time
# is the sign-up. Requested scopes: identify (ID, name, avatar) and guilds (only to
# check whether they are in our server; the list is not stored).
# ---------------------------------------------------------------------------
DISCORD_API = "https://discord.com/api/v10"
DISCORD_AUTHORIZE = "https://discord.com/oauth2/authorize"
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
DISCORD_INVITE_URL = os.getenv("DISCORD_INVITE_URL", "https://discord.gg/thecheeseclub").strip()
SESSION_COOKIE, STATE_COOKIE = "cc_session", "cc_oauth"
SESSION_SECONDS = 30 * 24 * 3600
_SESSION_KEY = hashlib.sha256(
    b"cheese-club-session:" + (os.getenv("SESSION_SECRET") or _ID_KEY.hex()).encode()).digest()
_search_log: dict = {}           # user id -> recent AoE4 search times (simple rate limit)
_club_guild_cache = {"id": None}


def login_enabled() -> bool:
    return bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET)


def _sign(data: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(_SESSION_KEY, raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{mac}"


def _unsign(token: Optional[str]) -> Optional[dict]:
    if not token or "." not in token:
        return None
    raw, mac = token.rsplit(".", 1)
    if not hmac.compare_digest(mac, hmac.new(_SESSION_KEY, raw.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) and data.get("exp", 0) > time.time() else None


def current_user(request: Request) -> Optional[dict]:
    return _unsign(request.cookies.get(SESSION_COOKIE))


def _safe_next(value: Optional[str], default: str = "/dashboard") -> str:
    value = (value or "").strip()
    return value if value.startswith("/") and not value.startswith("//") and "\\" not in value else default


def _cookie_args(request: Request, max_age: int) -> dict:
    return {"max_age": max_age, "httponly": True, "samesite": "lax", "path": "/",
            "secure": site_url(request).startswith("https://")}


def _avatar_url(user: dict) -> str:
    if user.get("avatar"):
        return f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png?size=128"
    return f"https://cdn.discordapp.com/embed/avatars/{(int(user['id']) >> 22) % 6}.png"


async def club_guild_id() -> Optional[str]:
    """The Cheese Club server's ID: DISCORD_GUILD_ID, or the server the bot syncs members from."""
    if DISCORD_GUILD_ID:
        return DISCORD_GUILD_ID
    if not _club_guild_cache["id"]:
        _club_guild_cache["id"] = await fetchval("""
            SELECT guild_id FROM discord_member_profiles GROUP BY guild_id ORDER BY COUNT(*) DESC LIMIT 1
        """, default=None)
    return _club_guild_cache["id"]


def require_login(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Please log in first.")
    return user


def require_same_site(request: Request):
    """Block cross-site requests that try to change a member's account (CSRF)."""
    from urllib.parse import urlsplit
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    source_host = urlsplit(source).netloc.lower()
    allowed = {urlsplit(site_url(request)).netloc.lower(),
               request.headers.get("x-forwarded-host", request.headers.get("host", "")).split(",")[0].strip().lower()}
    if not source_host or source_host not in allowed:
        raise HTTPException(status_code=403, detail="This request must come from the website.")


@app.get("/auth/login", include_in_schema=False)
async def auth_login(request: Request, next: str = "/dashboard"):
    if not login_enabled():
        return RedirectResponse("/login?error=unavailable", status_code=302)
    state = secrets.token_urlsafe(24)
    query = httpx.QueryParams({
        "client_id": DISCORD_CLIENT_ID, "response_type": "code", "scope": "identify guilds",
        "redirect_uri": site_url(request) + "/auth/callback", "state": state, "prompt": "none",
    })
    response = RedirectResponse(f"{DISCORD_AUTHORIZE}?{query}", status_code=302)
    response.set_cookie(STATE_COOKIE, _sign({"s": state, "n": _safe_next(next), "exp": time.time() + 600}),
                        **_cookie_args(request, 600))
    return response


@app.get("/auth/callback", include_in_schema=False)
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    saved = _unsign(request.cookies.get(STATE_COOKIE))
    if error:
        return RedirectResponse("/login?error=cancelled", status_code=302)
    if not saved or not code or not hmac.compare_digest(saved.get("s", ""), state):
        return RedirectResponse("/login?error=expired", status_code=302)
    try:
        token = await http_client.post(f"{DISCORD_API}/oauth2/token", auth=(DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET), data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": site_url(request) + "/auth/callback"})
        token.raise_for_status()
        headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
        me = await http_client.get(f"{DISCORD_API}/users/@me", headers=headers)
        me.raise_for_status()
        user = me.json()
        guild = await club_guild_id()
        in_guild = False
        guilds = await http_client.get(f"{DISCORD_API}/users/@me/guilds", headers=headers)
        if guilds.status_code == 200 and guild:
            in_guild = any(str(g.get("id")) == str(guild) for g in guilds.json())
    except Exception:
        log.exception("Discord login failed")
        return RedirectResponse("/login?error=discord", status_code=302)
    session = {"uid": str(user["id"]), "name": user.get("global_name") or user.get("username") or "Member",
               "avatar": _avatar_url(user), "g": in_guild, "exp": time.time() + SESSION_SECONDS}
    try:
        await mark_website_user(str(user["id"]), force=True)
    except Exception:
        log.exception("Could not record website user")
    response = RedirectResponse(saved.get("n") or "/dashboard", status_code=302)
    response.set_cookie(SESSION_COOKIE, _sign(session), **_cookie_args(request, SESSION_SECONDS))
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


@app.get("/auth/logout", include_in_schema=False)
async def auth_logout(next: str = "/"):
    response = RedirectResponse(_safe_next(next, "/"), status_code=302)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


async def _link_row(user_id: str):
    guild = await club_guild_id()
    rows = await fetch("""
        SELECT profile_id, aoe4_name, bypassed, linked_at FROM aoe4_links
        WHERE user_id=$1 AND ($2::text IS NULL OR guild_id=$2) ORDER BY linked_at DESC LIMIT 1
    """, user_id, guild)
    return rows[0] if rows else None


@app.get("/api/me")
async def api_me(request: Request):
    user = current_user(request)
    if not user:
        return {"logged_in": False, "login_enabled": login_enabled()}
    uid = user["uid"]
    await mark_website_user(uid)
    guild = await club_guild_id()
    present = await fetchval("""
        SELECT COUNT(*) FROM discord_member_profiles
        WHERE user_id=$1 AND is_present AND ($2::text IS NULL OR guild_id=$2)
    """, uid, guild, default=0)
    link = await _link_row(uid)
    hidden = await fetchval("SELECT hidden FROM website_member_settings WHERE user_id=$1", uid, default=False)
    return {
        "logged_in": True, "user_id": to_public_id(uid), "id_hint": id_hint(uid),
        "username": user.get("name"), "avatar_url": user.get("avatar"),
        "in_server": bool(present) or bool(user.get("g")), "invite_url": DISCORD_INVITE_URL,
        "hidden": bool(hidden),
        "aoe4": {"profile_id": link["profile_id"], "name": link["aoe4_name"], "bypassed": link["bypassed"],
                 "linked_at": link["linked_at"]} if link and link["profile_id"] else None,
    }


def _candidate(player: dict) -> dict:
    boards = player.get("leaderboards") or player.get("modes") or {}
    def rating(*keys):
        for k in keys:
            v = boards.get(k)
            if isinstance(v, dict) and v.get("rating"):
                return v["rating"]
        return None
    return {"profile_id": player.get("profile_id"), "name": player.get("name"), "country": player.get("country"),
            "solo_elo": rating("rm_solo", "rm_1v1"), "team_elo": rating("rm_team"),
            "last_game_at": player.get("last_game_at")}


@app.get("/api/me/aoe4/search")
async def api_aoe4_search(request: Request, name: str = ""):
    user = require_login(request)
    name = (name or "").strip()[:60]
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Type at least 2 characters.")
    now = time.time()
    recent = [t for t in _search_log.get(user["uid"], []) if now - t < 60]
    if len(recent) >= 15:
        raise HTTPException(status_code=429, detail="Too many searches. Wait a minute and try again.")
    _search_log[user["uid"]] = recent + [now]
    # A pasted AoE4World profile link or ID
    match = re.search(r"aoe4world\.com/players/(\d+)", name)
    pid = match.group(1) if match else (name if name.isdigit() and 3 <= len(name) <= 12 else None)
    try:
        if pid:
            r = await http_client.get(f"{AOE4WORLD_API}/players/{pid}")
            return {"results": [_candidate(r.json())] if r.status_code == 200 else []}
        # Same approach as the bot's /aoe4link: exact match first, then a wider search,
        # then the name without decorations such as stars or brackets.
        queries = [name]
        stripped = " ".join("".join(ch for ch in name if ch.isalnum() or ch.isspace()).split())
        if stripped and stripped.casefold() != name.casefold():
            queries.append(stripped)
        for q in queries:
            for params in ({"query": q, "exact": "true"}, {"query": q}):
                r = await http_client.get(f"{AOE4WORLD_API}/players/search", params=params)
                if r.status_code == 200 and (r.json().get("players") or []):
                    return {"results": [_candidate(p) for p in r.json()["players"][:8]]}
        return {"results": []}
    except Exception:
        raise HTTPException(status_code=503, detail="AoE4World didn't respond. Try again in a minute.")


@app.post("/api/me/aoe4")
async def api_aoe4_link(request: Request):
    user = require_login(request)
    require_same_site(request)
    body = await request.json()
    try:
        profile_id = int(body.get("profile_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Pick a profile from the list.")
    r = await http_client.get(f"{AOE4WORLD_API}/players/{profile_id}")
    if r.status_code != 200 or not r.json().get("profile_id"):
        raise HTTPException(status_code=404, detail="That AoE4World profile couldn't be found.")
    guild = await club_guild_id()
    if not guild:
        raise HTTPException(status_code=503, detail="Linking isn't available yet. Please try again later.")
    name = r.json().get("name") or "?"
    # Identical to the bot's own link command, so the bot sees it straight away
    await fetch("""
        INSERT INTO aoe4_links (guild_id, user_id, profile_id, aoe4_name, bypassed)
        VALUES ($1, $2, $3, $4, FALSE)
        ON CONFLICT (guild_id, user_id)
        DO UPDATE SET profile_id=$3, aoe4_name=$4, bypassed=FALSE, linked_at=NOW()
    """, guild, user["uid"], profile_id, name)
    _rating_cache.pop(str(profile_id), None)
    return {"ok": True, "profile_id": profile_id, "name": name}


@app.delete("/api/me/aoe4")
async def api_aoe4_unlink(request: Request):
    user = require_login(request)
    require_same_site(request)
    guild = await club_guild_id()
    # Keep an admin's queue bypass if there is one; otherwise remove the link entirely.
    await fetch("""
        UPDATE aoe4_links SET profile_id=NULL, aoe4_name=NULL
        WHERE guild_id=$1 AND user_id=$2 AND bypassed
    """, guild, user["uid"])
    await fetch("DELETE FROM aoe4_links WHERE guild_id=$1 AND user_id=$2 AND NOT bypassed", guild, user["uid"])
    return {"ok": True}


def _week_start(dt: datetime) -> datetime:
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    day = dt.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


@app.get("/api/me/dashboard")
async def api_dashboard(request: Request):
    """Everything on a member's Dashboard. Works even if they hid their public profile."""
    user = require_login(request)
    uid = user["uid"]
    ins = await build_player_insights(uid)
    totals = await fetch("""
        SELECT COALESCE(SUM(wins),0)::int AS wins, COALESCE(SUM(losses),0)::int AS losses
        FROM aoe_queue_stats WHERE user_id=$1
    """, uid)
    wins, losses = (totals[0]["wins"], totals[0]["losses"]) if totals else (0, 0)

    # Club rank: same order as the leaderboard's "All queues" view (wins, then games)
    ranking = await fetch("""
        SELECT user_id FROM aoe_queue_stats GROUP BY user_id
        HAVING SUM(wins + losses) >= 1
        ORDER BY SUM(wins) DESC, SUM(wins + losses) DESC
    """)
    ranked = [str(r["user_id"]) for r in ranking if str(r["user_id"]) not in HIDDEN_IDS or str(r["user_id"]) == uid]
    rank = ranked.index(uid) + 1 if uid in ranked else None

    # Last 10 weeks: wins and losses, and voice hours
    this_week = _week_start(datetime.now(timezone.utc))
    weeks = [this_week - timedelta(weeks=i) for i in range(9, -1, -1)]
    wl = {w: [0, 0] for w in weeks}
    for m in await load_club_matches():
        outcome = player_outcome(m, uid)
        when = _when(m)
        if outcome is None or not when:
            continue
        wk = _week_start(when)
        if wk in wl:
            wl[wk][0 if outcome == "win" else 1] += 1
    voice_rows = await fetch("""
        SELECT date_trunc('week', left_at AT TIME ZONE 'UTC') AS wk, SUM(duration_s)::float AS secs
        FROM vc_logs WHERE user_id=$1 AND left_at >= $2 GROUP BY wk
    """, uid, weeks[0])
    voice = {(r["wk"].replace(tzinfo=timezone.utc) if r["wk"].tzinfo is None else r["wk"]): r["secs"] for r in voice_rows}

    in_voice = None
    if await fetchval("SELECT to_regclass('vc_live') IS NOT NULL", default=False):
        in_voice = await fetchval("""
            SELECT channel_name FROM vc_live
            WHERE user_id=$1 AND updated_at >= NOW() - INTERVAL '10 minutes' LIMIT 1
        """, uid, default=None)

    last = next((m for m in ins["recent_matches"] if m["outcome"] in ("win", "loss")), None)
    in_progress = [a for a in ins["achievements"] if not a["earned"]]
    in_progress.sort(key=lambda a: a["progress"] / a["target"], reverse=True)
    return {
        "record": {"wins": wins, "losses": losses, "games": wins + losses},
        "current_streak": ins["current_streak"], "best_streak": ins["best_streak"],
        "rank": rank, "ranked_players": len(ranked),
        "weeks": [{"week": w.date().isoformat(), "wins": wl[w][0], "losses": wl[w][1],
                   "voice_hours": round((voice.get(w) or 0) / 3600, 1)} for w in weeks],
        "civs": ins["civs"],
        "last_match": last,
        "achievements_earned": sum(1 for a in ins["achievements"] if a["earned"]),
        "achievements_total": len(ins["achievements"]),
        "achievements_next": in_progress[:3],
        "in_voice": in_voice,
    }


_history_cache: dict = {}


async def aoe4world_history(profile_id) -> dict:
    """Solo and Team rating history from AoE4World (cached 30 minutes)."""
    key = str(profile_id)
    cached = _history_cache.get(key)
    if cached and time.monotonic() < cached[0]:
        return cached[1]
    result = {"solo": [], "team": []}
    try:
        r = await http_client.get(f"{AOE4WORLD_API}/players/{key}")
        if r.status_code == 200:
            modes = r.json().get("modes") or {}
            for name, mode in (("solo", "rm_solo"), ("team", "rm_team")):
                history = (modes.get(mode) or {}).get("rating_history") or {}
                points = []
                for k, entry in history.items():
                    if not isinstance(entry, dict) or entry.get("rating") is None:
                        continue
                    when = None
                    try:
                        when = datetime.fromisoformat(str(k).replace("Z", "+00:00"))
                    except ValueError:
                        if str(k).isdigit() and len(str(k)) == 10:
                            when = datetime.fromtimestamp(int(k), timezone.utc)
                    points.append({"t": when.isoformat() if when else None, "key": str(k), "rating": entry["rating"]})
                # Order by time when the keys are dates, otherwise by game ID
                points.sort(key=lambda p: p["t"] or "") if all(p["t"] for p in points) else \
                    points.sort(key=lambda p: int(p["key"]) if p["key"].isdigit() else 0)
                result[name] = [{"t": p["t"], "rating": p["rating"]} for p in points]
    except Exception:
        log.exception("Could not load AoE4World rating history")
    _history_cache[key] = (time.monotonic() + RATING_CACHE_SECONDS, result)
    return result


@app.get("/api/me/stats")
async def api_my_stats(request: Request, days: int = 90):
    user = require_login(request)
    uid = user["uid"]
    since = datetime.now(timezone.utc) - timedelta(days=days) if days and days > 0 else None
    queues, times = {}, []
    for m in await load_club_matches():
        outcome = player_outcome(m, uid)
        when = _when(m)
        if outcome is None or not when:
            continue
        when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        if since and when < since:
            continue
        q = queues.setdefault(m["queue"] or "other", {"queue": m["queue_type"], "games": 0, "wins": 0})
        q["games"] += 1
        q["wins"] += outcome == "win"
        times.append(when.isoformat())
    link = await _link_row(uid)
    history = {"solo": [], "team": []}
    if link and link["profile_id"]:
        history = await aoe4world_history(link["profile_id"])
        if since:
            history = {k: [p for p in v if not p["t"] or datetime.fromisoformat(p["t"]) >= since] for k, v in history.items()}
    order = {"1v1": 1, "2v2": 2, "3v3": 3, "4v4": 4}
    return {
        "days": days,
        "linked": bool(link and link["profile_id"]),
        "rating_history": history,
        "queues": sorted(queues.values(), key=lambda q: order.get(str(q["queue"]).lower(), 9)),
        "match_times": times[-1000:],
    }


# ---------------------------------------------------------------------------
# Queues. The Discord bot is in charge: it publishes who is waiting
# (aoe_queue_live) and carries out Join/Leave requests made here
# (aoe_queue_requests) with the same code as its Discord queue menu.
# ---------------------------------------------------------------------------
QUEUE_ORDER = ["1v1", "2v2", "3v3", "4v4", "4v3", "3v2"]   # same order as the Discord queue menu
_queue_request_times: dict = {}


async def _queue_tables_ready() -> bool:
    return bool(await fetchval("SELECT to_regclass('aoe_queue_live') IS NOT NULL AND to_regclass('aoe_queue_requests') IS NOT NULL", default=False))


async def queue_snapshot() -> dict:
    """Live queues as published by the bot, with display names (Discord IDs stay raw here)."""
    if not await _queue_tables_ready():
        return {"available": False}
    guild = await club_guild_id()
    rows = await fetch("""
        SELECT queues, require_voice, updated_at, updated_at >= NOW() - INTERVAL '60 seconds' AS online
        FROM aoe_queue_live WHERE ($1::text IS NULL OR guild_id=$1) ORDER BY updated_at DESC LIMIT 1
    """, guild)
    if not rows:
        return {"available": False}
    data = json_obj_safe(rows[0]["queues"])
    ids = [p["user_id"] for q in data.values() if isinstance(q, dict) for p in q.get("players", [])]
    names = await people(ids)
    queues = []
    for qt in QUEUE_ORDER + [q for q in data if q not in QUEUE_ORDER]:
        q = data.get(qt)
        if not isinstance(q, dict):
            continue
        players = [{**names.get(str(p["user_id"]), {}), "raw_id": str(p["user_id"]), "joined_at": p.get("joined_at")}
                   for p in q.get("players", [])]
        queues.append({"queue": qt, "size": q.get("size"), "players": players})
    return {"available": True, "online": bool(rows[0]["online"]), "require_voice": bool(rows[0]["require_voice"]),
            "updated_at": rows[0]["updated_at"], "queues": queues}


def json_obj_safe(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _public_queues(snap: dict) -> dict:
    out = dict(snap)
    if snap.get("available"):
        out["queues"] = [{**q, "players": [{k: v for k, v in p.items() if k != "raw_id"} for p in q["players"]]}
                         for q in snap["queues"]]
    return out


@app.get("/api/queue")
async def api_queue():
    """Who is waiting in each queue right now (public)."""
    return _public_queues(await queue_snapshot())


@app.get("/api/me/queue")
async def api_my_queue(request: Request):
    user = require_login(request)
    uid = user["uid"]
    snap = await queue_snapshot()
    my_queue = None
    if snap.get("available"):
        for q in snap["queues"]:
            for p in q["players"]:
                if p["raw_id"] == uid:
                    my_queue = {"queue": q["queue"], "joined_at": p["joined_at"], "count": len(q["players"]), "size": q["size"]}
    guild = await club_guild_id()
    present = await fetchval("""
        SELECT COUNT(*) FROM discord_member_profiles
        WHERE user_id=$1 AND is_present AND ($2::text IS NULL OR guild_id=$2)
    """, uid, guild, default=0)
    link = await _link_row(uid)
    in_voice = None
    if await fetchval("SELECT to_regclass('vc_live') IS NOT NULL", default=False):
        in_voice = await fetchval("""
            SELECT channel_name FROM vc_live WHERE user_id=$1 AND updated_at >= NOW() - INTERVAL '10 minutes' LIMIT 1
        """, uid, default=None)
    now = datetime.now(timezone.utc)
    match = next((m for m in reversed(await load_matches())
                  if m["result"] == "pending" and uid in m["players"]
                  and (m["created_at"] if m["created_at"].tzinfo else m["created_at"].replace(tzinfo=timezone.utc)) >= now - timedelta(hours=6)), None)
    pending = None
    if snap.get("available"):
        pending = await fetchval("""
            SELECT id FROM aoe_queue_requests WHERE user_id=$1 AND status='pending' ORDER BY id DESC LIMIT 1
        """, uid, default=None)
    return {
        "available": bool(snap.get("available")), "online": bool(snap.get("online")),
        "require_voice": bool(snap.get("require_voice", True)),
        "in_server": bool(present) or bool(user.get("g")),
        "linked": bool(link and (link["profile_id"] or link["bypassed"])),
        "in_voice": in_voice, "my_queue": my_queue, "pending_request": pending,
        "match": {"id": match["id"], "queue_type": match["queue_type"]} if match else None,
        "invite_url": DISCORD_INVITE_URL,
    }


@app.post("/api/me/queue")
async def api_queue_action(request: Request):
    user = require_login(request)
    require_same_site(request)
    body = await request.json()
    action = str(body.get("action") or "")
    queue_type = str(body.get("queue_type") or "").lower() or None
    if action not in ("join", "leave") or (action == "join" and queue_type not in QUEUE_ORDER):
        raise HTTPException(status_code=400, detail="Choose a queue to join.")
    if not await _queue_tables_ready():
        raise HTTPException(status_code=503, detail="Queueing from the website isn't switched on yet.")
    snap = await queue_snapshot()
    if not snap.get("online"):
        raise HTTPException(status_code=503, detail="The queue bot is offline right now. Please try again shortly.")
    now = time.time()
    if now - _queue_request_times.get(user["uid"], 0) < 2:
        raise HTTPException(status_code=429, detail="One moment, your last request is still being handled.")
    busy = await fetchval("SELECT id FROM aoe_queue_requests WHERE user_id=$1 AND status='pending' LIMIT 1", user["uid"], default=None)
    if busy:
        raise HTTPException(status_code=429, detail="One moment, your last request is still being handled.")
    _queue_request_times[user["uid"]] = now
    guild = await club_guild_id()
    request_id = await fetchval("""
        INSERT INTO aoe_queue_requests (guild_id, user_id, action, queue_type) VALUES ($1, $2, $3, $4) RETURNING id
    """, guild, user["uid"], action, queue_type)
    return {"request_id": request_id}


@app.get("/api/me/queue/requests/{request_id}")
async def api_queue_request_status(request: Request, request_id: int):
    user = require_login(request)
    rows = await fetch("SELECT status, message FROM aoe_queue_requests WHERE id=$1 AND user_id=$2", request_id, user["uid"])
    if not rows:
        raise HTTPException(status_code=404, detail="Request not found")
    return {"status": rows[0]["status"], "message": rows[0]["message"]}


# ---------------------------------------------------------------------------
# Queue alerts (sent by the bot) and the notification bell
# ---------------------------------------------------------------------------
_user_seen_marks: dict = {}
ALERT_WHEN = ("one_away", "two_away", "first_join")
ALERT_DEFAULTS = {"enabled": False, "queues": [], "alert_when": "one_away", "quiet_enabled": False,
                  "quiet_start": 0, "quiet_end": 540, "tz": "UTC", "via_dm": True, "via_web": True,
                  "via_browser": False}


async def mark_website_user(user_id: str, force: bool = False):
    now = time.monotonic()
    last = _user_seen_marks.get(user_id)
    if not force and last is not None and now - last < 300:
        return
    _user_seen_marks[user_id] = now
    await fetch("""
        INSERT INTO website_users (user_id) VALUES ($1)
        ON CONFLICT (user_id) DO UPDATE SET last_seen=NOW()
    """, user_id)


async def notify(user_id: str, kind: str, title: str, body: str = None, link: str = None, ref: str = None):
    await fetch("""
        INSERT INTO member_notifications (user_id, kind, ref, title, body, link)
        VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING
    """, user_id, kind, ref, title, body, link)


@app.get("/api/me/alerts")
async def api_get_alerts(request: Request):
    user = require_login(request)
    rows = await fetch("SELECT * FROM member_alert_settings WHERE user_id=$1", user["uid"])
    s = dict(ALERT_DEFAULTS)
    extra = {"saved": False, "muted_until": None, "last_alert_at": None, "dm_status": None,
             "test_requested_at": None, "test_done_at": None}
    if rows:
        r = dict(rows[0])
        s.update({k: r[k] for k in ALERT_DEFAULTS})
        extra.update({k: r[k] for k in ("muted_until", "last_alert_at", "dm_status", "test_requested_at", "test_done_at")})
        extra["saved"] = True
    return {**s, **extra, "queue_options": QUEUE_ORDER}


@app.post("/api/me/alerts")
async def api_save_alerts(request: Request):
    user = require_login(request)
    require_same_site(request)
    b = await request.json()
    queues = [q for q in QUEUE_ORDER if q in (b.get("queues") or [])]
    alert_when = b.get("alert_when") if b.get("alert_when") in ALERT_WHEN else "one_away"
    try:
        quiet_start = max(0, min(1439, int(b.get("quiet_start", 0))))
        quiet_end = max(0, min(1439, int(b.get("quiet_end", 540))))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Quiet hours need a start and an end time.")
    tz = str(b.get("tz") or "UTC")[:64]
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:
        tz = "UTC"
    enabled = bool(b.get("enabled"))
    if enabled and not queues:
        raise HTTPException(status_code=400, detail="Pick at least one queue, or switch alerts off.")
    via = {k: bool(b.get(k)) for k in ("via_dm", "via_web", "via_browser")}
    if enabled and not any(via.values()):
        raise HTTPException(status_code=400, detail="Choose at least one way to get alerts.")
    await fetch("""
        INSERT INTO member_alert_settings (user_id, enabled, queues, alert_when, quiet_enabled, quiet_start,
                                           quiet_end, tz, via_dm, via_web, via_browser, updated_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW())
        ON CONFLICT (user_id) DO UPDATE SET enabled=$2, queues=$3, alert_when=$4, quiet_enabled=$5,
            quiet_start=$6, quiet_end=$7, tz=$8, via_dm=$9, via_web=$10, via_browser=$11, updated_at=NOW()
    """, user["uid"], enabled, queues, alert_when, bool(b.get("quiet_enabled")), quiet_start, quiet_end,
        tz, via["via_dm"], via["via_web"], via["via_browser"])
    return {"ok": True}


@app.post("/api/me/alerts/test")
async def api_test_alert(request: Request):
    user = require_login(request)
    require_same_site(request)
    rows = await fetch("""
        UPDATE member_alert_settings SET test_requested_at=NOW() WHERE user_id=$1 RETURNING test_requested_at
    """, user["uid"])
    if not rows:
        raise HTTPException(status_code=400, detail="Save your alert settings first.")
    return {"ok": True, "requested_at": rows[0]["test_requested_at"]}


@app.post("/api/me/alerts/unmute")
async def api_unmute_alerts(request: Request):
    user = require_login(request)
    require_same_site(request)
    await fetch("UPDATE member_alert_settings SET muted_until=NULL WHERE user_id=$1", user["uid"])
    return {"ok": True}


@app.get("/api/me/notifications")
async def api_notifications(request: Request, limit: int = 20):
    user = require_login(request)
    limit = max(1, min(limit, 50))
    rows = await fetch("""
        SELECT id, kind, title, body, link, created_at, read_at FROM member_notifications
        WHERE user_id=$1 ORDER BY created_at DESC, id DESC LIMIT $2
    """, user["uid"], limit)
    unread = await fetchval("SELECT COUNT(*) FROM member_notifications WHERE user_id=$1 AND read_at IS NULL",
                            user["uid"], default=0)
    browser = await fetchval("""
        SELECT enabled AND via_browser FROM member_alert_settings WHERE user_id=$1
    """, user["uid"], default=False)
    return {"items": [dict(r) for r in rows], "unread": unread, "browser_popups": bool(browser)}


@app.post("/api/me/notifications/read")
async def api_notifications_read(request: Request):
    user = require_login(request)
    require_same_site(request)
    body = await request.json()
    ids = [int(i) for i in (body.get("ids") or []) if str(i).isdigit()]
    if body.get("all"):
        await fetch("UPDATE member_notifications SET read_at=NOW() WHERE user_id=$1 AND read_at IS NULL", user["uid"])
    elif ids:
        await fetch("""
            UPDATE member_notifications SET read_at=NOW() WHERE user_id=$1 AND id = ANY($2::bigint[]) AND read_at IS NULL
        """, user["uid"], ids)
    return {"ok": True}


async def _notification_loop():
    """Every 30 seconds: match-starting, match-result and achievement notices
    for members who use the website. Safe to repeat (each notice is made once)."""
    while True:
        await asyncio.sleep(30)
        try:
            await make_match_notifications()
        except Exception:
            log.exception("Notification job failed")


async def make_match_notifications():
    users = {str(r["user_id"]): r["first_login"] for r in await fetch("SELECT user_id, first_login FROM website_users")}
    if not users:
        return
    matches = await load_matches()
    now = datetime.now(timezone.utc)
    aware = lambda d: d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)
    played_recently = set()
    # Tournament games get their own notices (match ready, knocked out, champion)
    tournament_games = set()
    if await fetchval("SELECT to_regclass('tournament_games') IS NOT NULL", default=False):
        tournament_games = {r["aoe_match_id"] for r in await fetch(
            "SELECT aoe_match_id FROM tournament_games WHERE created_at > NOW() - INTERVAL '2 days'")}
    for m in matches:
        if m["id"] in tournament_games or m["tournament"]:
            for uid in set(m["team1"] + m["team2"]) & users.keys():
                played_recently.add(uid)            # still check for new achievements
            continue
        created, finished = aware(m["created_at"]), aware(m["finished_at"])
        if m["result"] == "pending" and created and created >= now - timedelta(hours=6):
            for uid in set(m["players"]) & users.keys():
                if created >= users[uid]:
                    await notify(uid, "match_started", f"🎮 Your {m['queue_type']} match #{m['id']} is starting",
                                 "Head to Discord for the draft, map veto and civ picks.", "/queue", ref=str(m["id"]))
        if decided(m) and finished and finished >= now - timedelta(hours=24):
            for uid in set(m["team1"] + m["team2"]) & users.keys():
                played_recently.add(uid)
                if finished < users[uid]:
                    continue
                won = player_outcome(m, uid) == "win"
                civ = m["civs"].get(uid)
                await notify(uid, "match_result",
                             f"🏆 You won match #{m['id']} ({m['queue_type']})!" if won else f"Match #{m['id']} ({m['queue_type']}): a loss this time",
                             (f"Played as {civ}. " if civ else "") + ("GG! See how your week is going." if won else "Shake it off. See your stats and go again."),
                             "/dashboard", ref=str(m["id"]))
    # New achievements, for members who played in the last day
    for uid in played_recently:
        ins = await build_player_insights(uid)
        earned = {a["id"]: a for a in ins["achievements"] if a["earned"]}
        seen = {r["achievement_id"] for r in await fetch("SELECT achievement_id FROM member_achievements_seen WHERE user_id=$1", uid)}
        first_time = not seen
        for aid, a in earned.items():
            if aid in seen:
                continue
            await fetch("INSERT INTO member_achievements_seen (user_id, achievement_id) VALUES ($1,$2) ON CONFLICT DO NOTHING", uid, aid)
            if not first_time:      # the first check only records what they already had
                await notify(uid, "achievement", f"{a['icon']} Achievement unlocked: {a['name']}", a["description"],
                             "/dashboard", ref=aid)
        if first_time and not earned:
            await fetch("INSERT INTO member_achievements_seen (user_id, achievement_id) VALUES ($1,'_baseline') ON CONFLICT DO NOTHING", uid)


# ---------------------------------------------------------------------------
# Tournaments (version 1: 1v1, single elimination)
# Lifecycle: signup -> checkin (opens automatically) -> live (starts at the
# scheduled time, or earlier by an admin) -> finished. The website seeds the
# bracket, tracks series scores and advances winners. The Discord bot starts
# each game once both players press Ready, and its normal match result is read
# back here.
# ---------------------------------------------------------------------------
_tournament_lock = asyncio.Lock()


def is_admin(user: Optional[dict]) -> bool:
    return bool(user and user.get("uid") in ADMIN_IDS)


def require_admin(request: Request) -> dict:
    user = require_login(request)
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Only Cheese Club admins can do that.")
    return user


def _aware(d):
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def _round_name(r: int, rounds: int, size: int) -> str:
    left = rounds - r
    if left == 0:
        return "Final"
    if left == 1:
        return "Semifinal"
    if left == 2:
        return "Quarterfinal"
    return f"Round of {size // (2 ** (r - 1))}"


def _seed_positions(size: int) -> list:
    """Standard bracket order, e.g. 8 -> [1, 8, 4, 5, 2, 7, 3, 6], so top seeds meet last."""
    order = [1, 2]
    while len(order) < size:
        n = len(order) * 2
        order = [x for s in order for x in (s, n + 1 - s)]
    return order


async def _player_rating(uid: str) -> int:
    link = await _link_row(uid)
    if link and link["profile_id"]:
        solo = (await aoe4world_ratings(link["profile_id"])).get("solo_elo")
        if solo:
            return int(solo)
    elo = await fetchval("SELECT elo FROM aoe_queue_stats WHERE user_id=$1 AND LOWER(queue_type)='1v1' ORDER BY elo DESC LIMIT 1",
                         uid, default=0)
    return int(elo or 0)


async def _tnotify(uid, kind, title, body, tid, ref):
    try:
        await notify(uid, kind, title, body, f"/tournament/{tid}", ref=ref)
    except Exception:
        log.exception("Tournament notification failed")


def is_team_fmt(t: dict) -> bool:
    return t.get("format") == "teamcup"


async def entrant_members(eid) -> list:
    """Discord IDs behind a bracket entrant: a player ID, or "T<team id>" for a team."""
    if not eid:
        return []
    if str(eid).startswith("T"):
        rows = await fetch("SELECT user_id FROM tournament_team_members WHERE team_id=$1 AND status='accepted'", int(str(eid)[1:]))
        return [r["user_id"] for r in rows]
    return [eid]


async def entrant_info(eids, uid=None) -> dict:
    """Display info for entrants: players as people(), teams as name + members."""
    eids = [e for e in eids if e]
    team_ids = [int(e[1:]) for e in eids if str(e).startswith("T")]
    teams, members = {}, {}
    if team_ids:
        for r in await fetch("SELECT id, name FROM tournament_teams WHERE id = ANY($1::int[])", team_ids):
            teams[r["id"]] = r["name"]
        for r in await fetch("SELECT team_id, user_id FROM tournament_team_members WHERE team_id = ANY($1::int[]) AND status='accepted'", team_ids):
            members.setdefault(r["team_id"], []).append(r["user_id"])
    ppl = await people([e for e in eids if not str(e).startswith("T")] + [u for v in members.values() for u in v])
    out = {}
    for e in eids:
        if str(e).startswith("T"):
            tid_ = int(e[1:]); ms = members.get(tid_, [])
            out[e] = {"username": teams.get(tid_, "Team"), "user_id": None, "team": True,
                      "members": [{**ppl[u], "me": u == uid} for u in ms if u in ppl], "me": bool(uid and uid in ms)}
        elif e in ppl:
            out[e] = {**ppl[e], "team": False, "me": e == uid}
    return out


async def _set_entrant_status(t: dict, eid, status: str):
    if str(eid).startswith("T"):
        await fetch("UPDATE tournament_teams SET status=$2 WHERE id=$1", int(str(eid)[1:]), status)
    else:
        await fetch("UPDATE tournament_players SET status=$3 WHERE tournament_id=$1 AND user_id=$2", t["id"], eid, status)


async def _notify_entrant(t: dict, eid, title: str, body: str, ref: str):
    for uid in await entrant_members(eid):
        await _tnotify(uid, "tournament", title, body, t["id"], ref)


async def _start_team_cup(t: dict) -> str:
    tid, size = t["id"], int(t["team_size"])
    teams = [dict(r) for r in await fetch("""
        SELECT tt.*, (SELECT COUNT(*) FROM tournament_team_members m WHERE m.team_id=tt.id AND m.status='accepted') AS accepted
        FROM tournament_teams tt WHERE tournament_id=$1 AND status IN ('registered','waitlist')
        ORDER BY (status='registered') DESC, created_at
    """, tid)]
    ok = [tm for tm in teams if tm["checked_in_at"] and tm["accepted"] == size][:t["max_players"]]
    for tm in teams:
        if tm not in ok:
            await fetch("UPDATE tournament_teams SET status='no_show' WHERE id=$1", tm["id"])
    if len(ok) < 2:
        await fetch("UPDATE tournaments SET status='cancelled', finished_at=NOW() WHERE id=$1", tid)
        for tm in ok:
            await _notify_entrant(t, f"T{tm['id']}", f"{t['name']} was cancelled", "Not enough teams checked in.", f"t{tid}-cancel")
        return "Fewer than 2 teams checked in, so the tournament was cancelled."
    ratings = {}
    for tm in ok:
        ms = await entrant_members(f"T{tm['id']}")
        rs = [await _player_rating(u) for u in ms]
        ratings[f"T{tm['id']}"] = round(sum(rs) / len(rs)) if rs else 0
    order = {f"T{tm['id']}": i for i, tm in enumerate(ok)}
    seeded = sorted(ratings, key=lambda e: (-ratings[e], order[e]))
    for i, e in enumerate(seeded, 1):
        await fetch("UPDATE tournament_teams SET status='playing', seed=$2, rating=$3 WHERE id=$1", int(e[1:]), i, ratings[e])
    return await _build_bracket(t, seeded, "teams")


async def _start_tournament(t: dict) -> str:
    """Seed and build the bracket. Returns a short message."""
    tid = t["id"]
    if t.get("format") == "teamcup":
        return await _start_team_cup(t)
    rows = await fetch("""
        SELECT user_id, status, checked_in_at FROM tournament_players WHERE tournament_id=$1
        ORDER BY (status='registered') DESC, signed_up_at
    """, tid)
    playing = [r["user_id"] for r in rows if r["checked_in_at"] and r["status"] in ("registered", "waitlist")][:t["max_players"]]
    for r in rows:
        if r["user_id"] not in playing and r["status"] in ("registered", "waitlist"):
            await fetch("UPDATE tournament_players SET status='no_show' WHERE tournament_id=$1 AND user_id=$2", tid, r["user_id"])
    need = 6 if t.get("format") == "random3v3" else 2
    if t.get("format") == "ffa":
        playing = playing[:8]
    if len(playing) < need:
        await fetch("UPDATE tournaments SET status='cancelled', finished_at=NOW() WHERE id=$1", tid)
        for uid in playing:
            await _tnotify(uid, "tournament", f"{t['name']} was cancelled", "Not enough players checked in.", tid, f"t{tid}-cancel")
        return f"Fewer than {need} players checked in, so the tournament was cancelled."
    ratings = {uid: await _player_rating(uid) for uid in playing}
    order = {uid: i for i, uid in enumerate(playing)}
    seeded = sorted(playing, key=lambda u: (-ratings[u], order[u]))
    for i, uid in enumerate(seeded, 1):
        await fetch("UPDATE tournament_players SET status='playing', seed=$3, rating=$4 WHERE tournament_id=$1 AND user_id=$2",
                    tid, uid, i, ratings[uid])
    if t.get("format") == "random3v3":
        await fetch("UPDATE tournaments SET status='live', started_at=NOW(), current_round=0 WHERE id=$1", tid)
        return f"Started with {len(seeded)} players. " + await _generate_round(t, 1)
    if t.get("format") == "ffa":
        await fetch("UPDATE tournaments SET status='live', started_at=NOW(), current_round=0 WHERE id=$1", tid)
        return f"Started with {len(seeded)} players. " + await _ffa_round(t, 1)
    return await _build_bracket(t, seeded, "players")


async def _build_bracket(t: dict, seeded: list, unit: str) -> str:
    """Single-elimination bracket for players or teams (entrant IDs), top seeds get byes."""
    tid = t["id"]
    size = 2
    while size < len(seeded):
        size *= 2
    rounds = size.bit_length() - 1
    ids = {}
    for r in range(rounds, 0, -1):                       # create later rounds first so we know next ids
        for s in range(1, size // (2 ** r) + 1):
            best_of = t["bo_final"] if r == rounds else t["bo_semi"] if r == rounds - 1 else t["bo_default"]
            nxt = ids.get((r + 1, (s + 1) // 2))
            ids[(r, s)] = await fetchval("""
                INSERT INTO tournament_matches (tournament_id, round, slot, round_name, best_of, next_match_id, next_slot)
                VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id
            """, tid, r, s, _round_name(r, rounds, size), best_of, nxt, 1 if s % 2 else 2)
    positions = _seed_positions(size)
    await fetch("UPDATE tournaments SET status='live', started_at=NOW() WHERE id=$1", tid)
    for s in range(1, size // 2 + 1):
        a, b = positions[2 * s - 2], positions[2 * s - 1]
        pa = seeded[a - 1] if a <= len(seeded) else None
        pb = seeded[b - 1] if b <= len(seeded) else None
        await fetch("UPDATE tournament_matches SET p1_id=$2, p1_seed=$3, p2_id=$4, p2_seed=$5 WHERE id=$1",
                    ids[(1, s)], pa, a if pa else None, pb, b if pb else None)
        if pa and pb:
            await _make_ready(ids[(1, s)], t)
        elif pa or pb:
            await _finish_match(ids[(1, s)], pa or pb, t, note="Bye")
    return f"Started with {len(seeded)} {unit}."


async def _make_ready(mid: int, t: dict):
    rows = await fetch("""
        UPDATE tournament_matches SET status='ready', ready_since=NOW(), updated_at=NOW()
        WHERE id=$1 AND status='pending' RETURNING p1_id, p2_id, round_name, best_of
    """, mid)
    if not rows:
        return
    m = rows[0]
    names = await entrant_info([m["p1_id"], m["p2_id"]])
    for eid, other in ((m["p1_id"], m["p2_id"]), (m["p2_id"], m["p1_id"])):
        await _notify_entrant(t, eid, f"🏆 Your {m['round_name']} is ready vs {names.get(other, {}).get('username', 'your opponent')}",
                              f"{t['name']} · best of {m['best_of']}. Press I'm ready on the tournament page.", f"m{mid}-ready")


async def _finish_match(mid: int, winner: str, t: dict, note: str = None):
    rows = await fetch("""
        UPDATE tournament_matches SET status='done', winner_id=$2, note=COALESCE($3, note), finished_at=NOW(), updated_at=NOW()
        WHERE id=$1 AND status <> 'done' RETURNING p1_id, p2_id, next_match_id, next_slot, round_name, p1_seed, p2_seed
    """, mid, winner, note)
    if not rows:
        return
    m = rows[0]
    await fetch("UPDATE tournament_games SET status='void' WHERE tmatch_id=$1 AND status='playing'", mid)
    loser = m["p2_id"] if winner == m["p1_id"] else m["p1_id"]
    seed = m["p1_seed"] if winner == m["p1_id"] else m["p2_seed"]
    if loser:
        await _set_entrant_status(t, loser, "eliminated")
    if m["next_match_id"]:
        if loser:
            await _notify_entrant(t, loser, f"Out of {t['name']}", f"Knocked out in the {m['round_name']}. Thanks for playing, GG!", f"m{mid}-out")
        col = "p1" if m["next_slot"] == 1 else "p2"
        nxt = await fetch(f"""
            UPDATE tournament_matches SET {col}_id=$2, {col}_seed=$3, updated_at=NOW() WHERE id=$1 RETURNING p1_id, p2_id
        """, m["next_match_id"], winner, seed)
        if nxt and nxt[0]["p1_id"] and nxt[0]["p2_id"]:
            await _make_ready(m["next_match_id"], t)
    else:
        await fetch("""
            UPDATE tournaments SET status='finished', winner_id=$2, runner_up_id=$3, finished_at=NOW() WHERE id=$1
        """, t["id"], winner, loser)
        await _set_entrant_status(t, winner, "champion")
        await _notify_entrant(t, winner, f"🏆 You won {t['name']}!", "Champion! You're in the Hall of Fame.", f"t{t['id']}-champ")
        if loser:
            await _notify_entrant(t, loser, f"🥈 Runner-up in {t['name']}", "So close! A great run. GG!", f"t{t['id']}-second")


async def tournament_tick():
    """Advance every tournament: open check-in, start on time, read game results."""
    async with _tournament_lock:
        now = datetime.now(timezone.utc)
        for t in [dict(r) for r in await fetch("SELECT * FROM tournaments WHERE status IN ('signup','checkin','live')")]:
            if t["status"] == "signup" and now >= t["starts_at"] - timedelta(minutes=t["checkin_minutes"]):
                await fetch("UPDATE tournaments SET status='checkin' WHERE id=$1", t["id"])
                t["status"] = "checkin"
            if t["status"] == "checkin" and not t["checkin_notified"]:
                await fetch("UPDATE tournaments SET checkin_notified=TRUE WHERE id=$1", t["id"])
                for r in await fetch("SELECT user_id FROM tournament_players WHERE tournament_id=$1 AND status IN ('registered','waitlist')", t["id"]):
                    await _tnotify(r["user_id"], "tournament", f"✅ Check-in is open for {t['name']}",
                                   f"Check in before it starts at {t['starts_at']:%H:%M} UTC, or your spot goes to the waiting list.",
                                   t["id"], f"t{t['id']}-checkin")
            if t["status"] == "checkin" and now >= t["starts_at"]:
                await _start_tournament(t)
                continue
            if t["status"] != "live":
                continue
            if t.get("format") == "random3v3":
                await _tick_random(t)
                continue
            if t.get("format") == "ffa":
                await fetch("""UPDATE tournament_ffa_matches SET status='ready' WHERE tournament_id=$1 AND status='starting'
                               AND updated_at < NOW() - INTERVAL '2 minutes'""", t["id"])
                continue
            # Games played in Discord: read their results
            games = await fetch("""
                SELECT g.id, g.tmatch_id, m.result, m.team1_ids, m.team2_ids
                FROM tournament_games g JOIN aoe_matches m ON m.id = g.aoe_match_id
                JOIN tournament_matches tm ON tm.id = g.tmatch_id
                WHERE tm.tournament_id=$1 AND g.status='playing' AND m.result <> 'pending'
            """, t["id"])
            for g in games:
                tm = (await fetch("SELECT * FROM tournament_matches WHERE id=$1", g["tmatch_id"]))[0]
                if g["result"] in ("team1", "team2"):
                    side = {str(x) for x in ((g["team1_ids"] if g["result"] == "team1" else g["team2_ids"]) or [])}
                    m1, m2 = set(await entrant_members(tm["p1_id"])), set(await entrant_members(tm["p2_id"]))
                    if not side & (m1 | m2):
                        continue
                    winner = tm["p1_id"] if len(side & m1) >= len(side & m2) else tm["p2_id"]
                    await fetch("UPDATE tournament_games SET status='done', winner_id=$2 WHERE id=$1", g["id"], winner)
                    col = "p1_score" if winner == tm["p1_id"] else "p2_score"
                    upd = (await fetch(f"""
                        UPDATE tournament_matches SET {col}={col}+1, updated_at=NOW() WHERE id=$1 RETURNING p1_score, p2_score, best_of
                    """, tm["id"]))[0]
                    need = upd["best_of"] // 2 + 1
                    if max(upd["p1_score"], upd["p2_score"]) >= need:
                        await _finish_match(tm["id"], winner, t)
                    else:   # next game: both are still ready, so the bot starts it straight away
                        await fetch("UPDATE tournament_matches SET status='ready', updated_at=NOW() WHERE id=$1 AND status='playing'", tm["id"])
                else:       # cancelled in Discord: the game doesn't count; both press Ready again
                    await fetch("UPDATE tournament_games SET status='cancelled' WHERE id=$1", g["id"])
                    await fetch("""
                        UPDATE tournament_matches SET status='ready', p1_ready_at=NULL, p2_ready_at=NULL, ready_ids='{}', updated_at=NOW()
                        WHERE id=$1 AND status='playing'
                    """, tm["id"])
            # A start the bot claimed but never finished (e.g. it restarted): try again
            await fetch("""
                UPDATE tournament_matches SET status='ready' WHERE tournament_id=$1 AND status='starting'
                  AND updated_at < NOW() - INTERVAL '2 minutes'
                  AND NOT EXISTS (SELECT 1 FROM tournament_games g WHERE g.tmatch_id=tournament_matches.id AND g.status='playing')
            """, t["id"])


async def _tournament_loop():
    while True:
        await asyncio.sleep(10)
        try:
            await tournament_tick()
        except Exception:
            log.exception("Tournament job failed")


# ---------------------------------------------------------------------------
# Random Team 3v3: individual players, new random teammates every round,
# individual points. Teams are random but balanced by rating, avoid repeating
# teammates (and, less strongly, opponents), and sit-outs (byes) rotate fairly.
# Standings are always calculated from the saved results, so a corrected result
# updates everything automatically.
# ---------------------------------------------------------------------------
DEFAULT_MAPS = ["African Waters", "Archipelago", "Dry Arabia", "Forts", "Gorge",
                "Hedgemaze", "Rocky River", "Sunkenlands", "West Lake"]
TEAM_BALANCE_TOLERANCE = 60


def _rt_cost(teams, mates, opps, rating):
    c = 0.0
    for i in range(0, len(teams), 2):
        a, b = teams[i], teams[i + 1]
        for t in (a, b):
            for x, y in ((t[0], t[1]), (t[0], t[2]), (t[1], t[2])):
                c += 10 * mates.get(frozenset((x, y)), 0) ** 2
        for x in a:
            for y in b:
                c += opps.get(frozenset((x, y)), 0)
        gap = abs(sum(rating[p] for p in a) - sum(rating[p] for p in b)) / 3
        c += max(0, gap - TEAM_BALANCE_TOLERANCE) / 10
    return c


def make_random_teams(players, mates, opps, rating, rng, iterations=4000):
    """Shuffle, then try random swaps that lower the cost (several restarts).
    Returns a list of teams of three; consecutive pairs play each other."""
    best, best_cost = None, float("inf")
    for _ in range(6):
        pl = list(players)
        rng.shuffle(pl)
        teams = [pl[i:i + 3] for i in range(0, len(pl), 3)]
        c = _rt_cost(teams, mates, opps, rating)
        if len(teams) > 1:
            for _ in range(iterations // 6):
                i, j = rng.sample(range(len(teams)), 2)
                a, b = rng.randrange(3), rng.randrange(3)
                teams[i][a], teams[j][b] = teams[j][b], teams[i][a]
                nc = _rt_cost(teams, mates, opps, rating)
                if nc <= c:
                    c = nc
                else:
                    teams[i][a], teams[j][b] = teams[j][b], teams[i][a]
        if c < best_cost:
            best, best_cost = [t[:] for t in teams], c
    return best


def pick_byes(players, bye_counts, last_round_byes, n, rng):
    """Fewest sit-outs first; not twice in a row if avoidable; ties at random."""
    return set(sorted(players, key=lambda p: (bye_counts.get(p, 0), p in last_round_byes, rng.random()))[:n])


async def _log_admin(tid, admin_id, action, detail):
    await fetch("INSERT INTO tournament_admin_log (tournament_id, admin_id, action, detail) VALUES ($1,$2,$3,$4)",
                tid, admin_id, action, json.dumps(detail, default=str))


async def _generate_round(t: dict, r: int) -> str:
    import random as _random
    rng = _random.Random()
    tid = t["id"]
    rows = await fetch("SELECT user_id, rating FROM tournament_players WHERE tournament_id=$1 AND status='playing'", tid)
    rating = {x["user_id"]: int(x["rating"] or 0) for x in rows}
    active = list(rating)
    if len(active) < 6:
        await _finish_random(t, note="Not enough players left for another round.")
        return "Not enough players left, so the tournament was finished."
    mates, opps = {}, {}
    for m in await fetch("SELECT team1, team2 FROM tournament_team_matches WHERE tournament_id=$1 AND status <> 'void'", tid):
        for team in (m["team1"], m["team2"]):
            for i in range(len(team)):
                for j in range(i + 1, len(team)):
                    k = frozenset((team[i], team[j])); mates[k] = mates.get(k, 0) + 1
        for x in m["team1"]:
            for y in m["team2"]:
                k = frozenset((x, y)); opps[k] = opps.get(k, 0) + 1
    bye_rows = await fetch("SELECT round, user_id FROM tournament_round_byes WHERE tournament_id=$1 AND substituted_into IS NULL", tid)
    bye_counts, last = {}, set()
    for b in bye_rows:
        bye_counts[b["user_id"]] = bye_counts.get(b["user_id"], 0) + 1
        if b["round"] == r - 1:
            last.add(b["user_id"])
    byes = pick_byes(active, bye_counts, last, len(active) % 6, rng)
    teams = make_random_teams([u for u in active if u not in byes], mates, opps, rating, rng)
    maps = list(t.get("round_maps") or [])
    chosen = maps[r - 1] if len(maps) >= r and maps[r - 1] and maps[r - 1] != "random" else None
    map_name = chosen or rng.choice(list(t.get("map_pool") or []) or DEFAULT_MAPS)
    names = await people(active)
    for n, i in enumerate(range(0, len(teams), 2), 1):
        a = sorted(teams[i], key=lambda u: -rating[u])          # highest rating first = captain
        b = sorted(teams[i + 1], key=lambda u: -rating[u])
        await fetch("""
            INSERT INTO tournament_team_matches (tournament_id, round, match_no, team1, team2, team1_avg, team2_avg, map)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        """, tid, r, n, a, b, round(sum(rating[u] for u in a) / 3), round(sum(rating[u] for u in b) / 3), map_name)
        for side, other in ((a, b), (b, a)):
            for uid in side:
                mates_txt = " and ".join(names[u]["username"] for u in side if u != uid)
                await _tnotify(uid, "tournament", f"🎲 Round {r}: you're with {mates_txt}",
                               f"vs {', '.join(names[u]['username'] for u in other)} · Map: {map_name}. Press I'm ready when you're in voice.",
                               tid, f"r{r}-team")
    for uid in byes:
        await fetch("INSERT INTO tournament_round_byes (tournament_id, round, user_id) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING", tid, r, uid)
        await _tnotify(uid, "tournament", f"🪑 You sit out round {r}", f"You get {float(t['points_bye']):g} points and may be called in as a substitute.",
                       tid, f"r{r}-bye")
    await fetch("UPDATE tournaments SET current_round=$2 WHERE id=$1", tid, r)
    return f"Round {r}: {len(teams) // 2} matches, {len(byes)} sitting out."


async def random_standings(t: dict) -> list:
    tid, pw, pb = t["id"], float(t["points_win"]), float(t["points_bye"])
    players = await fetch("""
        SELECT user_id, rating, status FROM tournament_players WHERE tournament_id=$1
          AND status IN ('playing','removed','champion','eliminated')
    """, tid)
    st = {p["user_id"]: {"user_id": p["user_id"], "rating": int(p["rating"] or 0), "status": p["status"], "played": 0,
                         "wins": 0, "losses": 0, "byes": 0, "form": {}} for p in players}
    done = await fetch("SELECT round, team1, team2, winner_team FROM tournament_team_matches WHERE tournament_id=$1 AND status='done'", tid)
    for m in done:
        win, lose = (m["team1"], m["team2"]) if m["winner_team"] == 1 else (m["team2"], m["team1"])
        for uid in win:
            if uid in st:
                st[uid]["played"] += 1; st[uid]["wins"] += 1; st[uid]["form"][m["round"]] = "w"
        for uid in lose:
            if uid in st:
                st[uid]["played"] += 1; st[uid]["losses"] += 1; st[uid]["form"][m["round"]] = "l"
    for b in await fetch("SELECT round, user_id FROM tournament_round_byes WHERE tournament_id=$1 AND substituted_into IS NULL", tid):
        if b["user_id"] in st:
            st[b["user_id"]]["byes"] += 1; st[b["user_id"]]["form"][b["round"]] = "b"
    for s in st.values():
        s["points"] = s["wins"] * pw + s["byes"] * pb
    for s in st.values():
        s["opp_points"] = 0.0
    for m in done:
        for side, other in ((m["team1"], m["team2"]), (m["team2"], m["team1"])):
            opp = sum(st[u]["points"] for u in other if u in st)
            for uid in side:
                if uid in st:
                    st[uid]["opp_points"] += opp
    rows = sorted(st.values(), key=lambda s: (-s["points"], -s["opp_points"], s["byes"], -s["rating"]))
    rounds = int(t.get("current_round") or 0)
    for i, s in enumerate(rows, 1):
        s["rank"] = i
        s["win_rate"] = round(100 * s["wins"] / s["played"]) if s["played"] else None
        s["form"] = [s["form"].get(r, "-") for r in range(1, rounds + 1)]
    return rows


async def _finish_random(t: dict, note: str = None):
    rows = await random_standings(t)
    top = [r["user_id"] for r in rows[:3]]
    await fetch("""
        UPDATE tournaments SET status='finished', finished_at=NOW(), winner_id=$2, runner_up_id=$3, third_id=$4 WHERE id=$1
    """, t["id"], top[0] if top else None, top[1] if len(top) > 1 else None, top[2] if len(top) > 2 else None)
    if top:
        await fetch("UPDATE tournament_players SET status='champion' WHERE tournament_id=$1 AND user_id=$2", t["id"], top[0])
    medals = ["🏆 You won", "🥈 Runner-up in", "🥉 Third place in"]
    for r in rows:
        if r["rank"] <= 3:
            await _tnotify(r["user_id"], "tournament", f"{medals[r['rank'] - 1]} {t['name']}!",
                           f"{r['points']:g} points from {r['wins']} wins. GG!", t["id"], f"t{t['id']}-place")
        else:
            await _tnotify(r["user_id"], "tournament", f"{t['name']} is over: you finished #{r['rank']}",
                           f"{r['points']:g} points from {r['wins']} wins. Thanks for playing!", t["id"], f"t{t['id']}-place")


async def _tick_random(t: dict):
    tid = t["id"]
    games = await fetch("""
        SELECT tm.id, tm.round, tm.team1, tm.team2, a.result, a.team1_ids, a.team2_ids
        FROM tournament_team_matches tm JOIN aoe_matches a ON a.id = tm.aoe_match_id
        WHERE tm.tournament_id=$1 AND tm.status='playing' AND a.result <> 'pending'
    """, tid)
    pw = float(t["points_win"])
    for g in games:
        if g["result"] in ("team1", "team2"):
            won = {str(x) for x in ((g["team1_ids"] if g["result"] == "team1" else g["team2_ids"]) or [])}
            side = 1 if len(won & set(g["team1"])) >= len(won & set(g["team2"])) else 2
            await fetch("""
                UPDATE tournament_team_matches SET status='done', winner_team=$2, finished_at=NOW(), updated_at=NOW()
                WHERE id=$1 AND status='playing'
            """, g["id"], side)
            for uid in (g["team1"] if side == 1 else g["team2"]):
                await _tnotify(uid, "tournament", f"✅ Round {g['round']} win: +{pw:g} point{'s' if pw != 1 else ''}",
                               t["name"], tid, f"tm{g['id']}-res")
        else:   # cancelled in Discord: doesn't count, everyone presses Ready again
            await fetch("""
                UPDATE tournament_team_matches SET status='ready', ready_ids='{}', aoe_match_id=NULL, updated_at=NOW()
                WHERE id=$1 AND status='playing'
            """, g["id"])
    await fetch("""
        UPDATE tournament_team_matches SET status='ready' WHERE tournament_id=$1 AND status='starting'
          AND updated_at < NOW() - INTERVAL '2 minutes'
    """, tid)
    r = int(t["current_round"] or 0)
    if r >= 1:
        left = await fetchval("""
            SELECT COUNT(*) FROM tournament_team_matches WHERE tournament_id=$1 AND round=$2 AND status NOT IN ('done','void')
        """, tid, r, default=1)
        total = await fetchval("SELECT COUNT(*) FROM tournament_team_matches WHERE tournament_id=$1 AND round=$2", tid, r, default=0)
        if total and not left:
            if r < int(t["rounds"]):
                await _generate_round(t, r + 1)
            else:
                await _finish_random(t)


def _t_public(t: dict) -> dict:
    keys = ("id", "name", "description", "mode", "format", "max_players", "starts_at", "checkin_minutes",
            "bo_default", "bo_semi", "bo_final", "rules", "prizes", "status", "started_at", "finished_at", "created_at",
            "rounds", "current_round", "round_maps", "map_pool", "team_size", "ffa_points")
    out = {k: t.get(k) for k in keys}
    out["points_win"] = float(t.get("points_win") or 1)
    out["points_bye"] = float(t.get("points_bye") or 0)
    out["checkin_opens_at"] = t["starts_at"] - timedelta(minutes=t["checkin_minutes"])
    return out


@app.get("/api/tournaments")
async def api_tournaments(request: Request):
    rows = [dict(r) for r in await fetch("""
        SELECT t.*, CASE WHEN t.format='teamcup'
                    THEN (SELECT COUNT(*) FROM tournament_teams tt WHERE tt.tournament_id=t.id AND tt.status IN ('registered','playing','eliminated','champion'))
                    ELSE (SELECT COUNT(*) FROM tournament_players p WHERE p.tournament_id=t.id AND p.status IN ('registered','playing','eliminated','champion')) END AS players,
               (SELECT COUNT(*) FROM tournament_players p WHERE p.tournament_id=t.id AND p.status='waitlist') AS waitlist
        FROM tournaments t WHERE t.status <> 'cancelled' OR t.finished_at > NOW() - INTERVAL '2 days'
        ORDER BY t.starts_at DESC LIMIT 60
    """)] if await fetchval("SELECT to_regclass('tournaments') IS NOT NULL", default=False) else []
    ids = [r[k] for r in rows for k in ("winner_id", "runner_up_id", "third_id") if r.get(k)]
    avatars = [r["user_id"] for t in rows if t["status"] in ("signup", "checkin")
               for r in await fetch("SELECT user_id FROM tournament_players WHERE tournament_id=$1 AND status IN ('registered','waitlist') ORDER BY signed_up_at LIMIT 8", t["id"])]
    names = await people([i for i in ids if not str(i).startswith("T")] + avatars)
    names.update(await entrant_info([i for i in ids if str(i).startswith("T")]))
    out = []
    for t in rows:
        item = {**_t_public(t), "players": t["players"], "waitlist": t["waitlist"],
                "winner": names.get(t["winner_id"]) if t["winner_id"] else None,
                "runner_up": names.get(t["runner_up_id"]) if t["runner_up_id"] else None,
                "third": names.get(t["third_id"]) if t.get("third_id") else None}
        if t["status"] in ("signup", "checkin"):
            first = await fetch("SELECT user_id FROM tournament_players WHERE tournament_id=$1 AND status IN ('registered','waitlist') ORDER BY signed_up_at LIMIT 8", t["id"])
            item["faces"] = [names[r["user_id"]] for r in first if r["user_id"] in names]
        if t["status"] == "live" and t.get("format") == "random3v3":
            item["current_round"] = f"Round {t['current_round']} of {t['rounds']}"
        elif t["status"] == "live":
            cur = await fetch("SELECT round_name FROM tournament_matches WHERE tournament_id=$1 AND status <> 'done' ORDER BY round LIMIT 1", t["id"])
            item["current_round"] = cur[0]["round_name"] if cur else None
        out.append(item)
    return {"tournaments": out, "is_admin": is_admin(current_user(request))}


async def _tournament_or_404(tid: int) -> dict:
    rows = await fetch("SELECT * FROM tournaments WHERE id=$1", tid)
    if not rows:
        raise HTTPException(status_code=404, detail="Tournament not found")
    return dict(rows[0])


@app.get("/api/tournaments/config/maps")
async def api_tournament_maps():
    return {"maps": DEFAULT_MAPS}


@app.get("/api/tournaments/{tid}")
async def api_tournament(request: Request, tid: int):
    t = await _tournament_or_404(tid)
    user = current_user(request)
    uid = user["uid"] if user else None
    players = [dict(r) for r in await fetch("""
        SELECT * FROM tournament_players WHERE tournament_id=$1 AND status <> 'withdrawn'
        ORDER BY seed NULLS LAST, (status='waitlist'), signed_up_at
    """, tid)]
    matches = [dict(r) for r in await fetch("SELECT * FROM tournament_matches WHERE tournament_id=$1 ORDER BY round, slot", tid)]
    games = [dict(r) for r in await fetch("""
        SELECT g.tmatch_id, g.game_no, g.winner_id, g.status, g.aoe_match_id, a.civ_data
        FROM tournament_games g JOIN tournament_matches m ON m.id=g.tmatch_id
        LEFT JOIN aoe_matches a ON a.id = g.aoe_match_id WHERE m.tournament_id=$1 ORDER BY g.id
    """, tid)]
    ids = [p["user_id"] for p in players] + [m[k] for m in matches for k in ("p1_id", "p2_id") if m[k]]
    names = await people(ids)
    pub = lambda u: {**names[u], "me": u == uid} if u and u in names else None
    ent_ids = list({m[k] for m in matches for k in ("p1_id", "p2_id", "winner_id") if m[k]}
                   | {t[k] for k in ("winner_id", "runner_up_id", "third_id") if t.get(k)})
    ents = await entrant_info(ent_ids, uid)
    pe = lambda e: ents.get(e) if e else None
    mem = {e: await entrant_members(e) for e in ent_ids}
    my = next((p for p in players if p["user_id"] == uid), None)
    my_match = next((m for m in matches if uid and (uid in mem.get(m["p1_id"], []) or uid in mem.get(m["p2_id"], []))
                     and m["status"] != "done" and m["p1_id"] and m["p2_id"]), None)
    eligible = None
    if user:
        link = await _link_row(uid)
        present = await fetchval("SELECT COUNT(*) FROM discord_member_profiles WHERE user_id=$1 AND is_present", uid, default=0)
        eligible = {"in_server": bool(present) or bool(user.get("g")), "linked": bool(link and (link["profile_id"] or link["bypassed"]))}
    def match_out(m):
        gs = [g for g in games if g["tmatch_id"] == m["id"]]
        civs = []
        for g in gs:
            data = json_obj_safe(g["civ_data"])
            civs.append({"game": g["game_no"], "status": g["status"], "aoe_match_id": g["aoe_match_id"],
                         "winner_me": g["winner_id"] == uid if uid else False,
                         "p1_civ": canonical_civ(data.get(m["p1_id"])) if m["p1_id"] else None,
                         "p2_civ": canonical_civ(data.get(m["p2_id"])) if m["p2_id"] else None})
        return {"id": m["id"], "round": m["round"], "slot": m["slot"], "round_name": m["round_name"], "best_of": m["best_of"],
                "p1": pe(m["p1_id"]), "p2": pe(m["p2_id"]), "p1_seed": m["p1_seed"], "p2_seed": m["p2_seed"],
                "p1_entrant": m["p1_id"] if is_team_fmt(t) else None, "p2_entrant": m["p2_id"] if is_team_fmt(t) else None,
                "p1_score": m["p1_score"], "p2_score": m["p2_score"], "winner": pe(m["winner_id"]),
                "p1_ready_n": len(set(m["ready_ids"]) & set(mem.get(m["p1_id"], []))) if m["p1_id"] else 0,
                "p2_ready_n": len(set(m["ready_ids"]) & set(mem.get(m["p2_id"], []))) if m["p2_id"] else 0,
                "p1_size": len(mem.get(m["p1_id"], [])), "p2_size": len(mem.get(m["p2_id"], [])),
                "me_ready": bool(uid and uid in m["ready_ids"]),
                "winner_slot": 1 if m["winner_id"] and m["winner_id"] == m["p1_id"] else 2 if m["winner_id"] else None,
                "status": m["status"], "note": m["note"], "p1_ready": bool(m["p1_ready_at"]), "p2_ready": bool(m["p2_ready_at"]),
                "games": civs}
    random_data = None
    if t.get("format") == "random3v3":
        tms = [dict(r) for r in await fetch("""
            SELECT tm.*, a.civ_data FROM tournament_team_matches tm LEFT JOIN aoe_matches a ON a.id = tm.aoe_match_id
            WHERE tm.tournament_id=$1 ORDER BY round, match_no
        """, tid)]
        byes = [dict(r) for r in await fetch("SELECT * FROM tournament_round_byes WHERE tournament_id=$1 ORDER BY round", tid)]
        more = await people(list({u for m in tms for u in m["team1"] + m["team2"]} | {b["user_id"] for b in byes}))
        names.update(more)
        standings = await random_standings(t)
        def tm_out(m):
            team_ids = m["team1"] + m["team2"]
            civs = json_obj_safe(m.get("civ_data"))
            side = lambda ids: [{**(pub(u) or {}), "civ": canonical_civ(civs.get(u)) if civs else None, "ready": u in m["ready_ids"]} for u in ids]
            return {"id": m["id"], "round": m["round"], "match_no": m["match_no"], "map": m["map"],
                    "team1": side(m["team1"]), "team2": side(m["team2"]), "team1_avg": m["team1_avg"], "team2_avg": m["team2_avg"],
                    "status": m["status"], "winner_team": m["winner_team"], "note": m["note"], "aoe_match_id": m["aoe_match_id"],
                    "ready_count": len(set(m["ready_ids"]) & set(team_ids)), "me_in": uid in team_ids if uid else False,
                    "me_ready": uid in m["ready_ids"] if uid else False,
                    "my_team": (1 if uid in m["team1"] else 2) if uid and uid in team_ids else None}
        cur = int(t.get("current_round") or 0)
        rounds = []
        for r in range(1, cur + 1):
            rm = [tm_out(m) for m in tms if m["round"] == r]
            rounds.append({"round": r, "map": rm[0]["map"] if rm else None, "matches": rm,
                           "byes": [{**(pub(b["user_id"]) or {}), "substituted_into": b["substituted_into"]} for b in byes if b["round"] == r]})
        mine = next((tm_out(m) for m in tms if m["round"] == cur and uid and uid in m["team1"] + m["team2"]
                     and m["status"] not in ("done", "void")), None)
        my_bye = bool(uid and any(b["round"] == cur and b["user_id"] == uid and not b["substituted_into"] for b in byes))
        random_data = {
            "rounds": rounds, "my_match": mine, "my_bye": my_bye,
            "standings": [{**(pub(s["user_id"]) or {}), **{k: s[k] for k in ("rank", "points", "played", "wins", "losses",
                          "byes", "win_rate", "form", "opp_points", "status")}} for s in standings],
            "third": pe(t.get("third_id")) or pub(t.get("third_id")),
        }
    return {
        "random": random_data,
        "tournament": _t_public(t),
        "teams": await _teams_out(t, uid) if is_team_fmt(t) else None,
        "ffa": await _ffa_out(t, uid) if t.get("format") == "ffa" else None,
        "winner": pe(t["winner_id"]) or pub(t["winner_id"]), "runner_up": pe(t["runner_up_id"]) or pub(t["runner_up_id"]),
        "third": pe(t.get("third_id")) or pub(t.get("third_id")),
        "players": [{**(pub(p["user_id"]) or {}), "status": p["status"], "seed": p["seed"], "rating": p["rating"],
                     "checked_in": bool(p["checked_in_at"])} for p in players],
        "matches": [match_out(m) for m in matches],
        "me": {"logged_in": bool(user), "status": my["status"] if my else None, "checked_in": bool(my and my["checked_in_at"]),
               "match": match_out(my_match) if my_match else None,
               "my_slot": (1 if uid in mem.get(my_match["p1_id"], []) else 2) if my_match else None,
               "eligible": eligible},
        "is_admin": is_admin(user), "now": datetime.now(timezone.utc),
    }


@app.post("/api/tournaments")
async def api_create_tournament(request: Request):
    user = require_admin(request)
    require_same_site(request)
    b = await request.json()
    name = str(b.get("name") or "").strip()[:80]
    if len(name) < 3:
        raise HTTPException(status_code=400, detail="Give the tournament a name.")
    try:
        starts = datetime.fromisoformat(str(b.get("starts_at")).replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Pick a start date and time.")
    starts = _aware(starts)
    if starts < datetime.now(timezone.utc) + timedelta(minutes=5):
        raise HTTPException(status_code=400, detail="The start time must be in the future.")
    def num(k, lo, hi, d):
        try:
            return max(lo, min(hi, int(b.get(k, d))))
        except (TypeError, ValueError):
            return d
    bo = lambda k, d: num(k, 1, 7, d) if num(k, 1, 7, d) % 2 else d
    fmt = b.get("format") if b.get("format") in ("random3v3", "teamcup", "ffa") else "single"
    if fmt == "ffa" and not 2 <= num("max_players", 2, 64, 8) <= 8:
        raise HTTPException(status_code=400, detail="An FFA lobby holds 2 to 8 players.")
    if fmt == "random3v3" and num("max_players", 2, 64, 8) < 6:
        raise HTTPException(status_code=400, detail="Random Team 3v3 needs room for at least 6 players.")
    def pts(k, lo, hi, d):
        try:
            v = float(b.get(k, d))
        except (TypeError, ValueError):
            return d
        return max(lo, min(hi, round(v * 2) / 2))
    rounds = num("rounds", 1, 10, 5)
    round_maps = [m if m in DEFAULT_MAPS else "random" for m in (b.get("round_maps") or [])][:rounds]
    pool = [m for m in (b.get("map_pool") or []) if m in DEFAULT_MAPS] or list(DEFAULT_MAPS)
    tid = await fetchval("""
        INSERT INTO tournaments (guild_id, name, description, max_players, starts_at, checkin_minutes,
                                 bo_default, bo_semi, bo_final, rules, prizes, created_by)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id
    """, await club_guild_id(), name, str(b.get("description") or "")[:600], num("max_players", 2, 64, 8), starts,
        num("checkin_minutes", 10, 120, 30), bo("bo_default", 1), bo("bo_semi", 3), bo("bo_final", 5),
        str(b.get("rules") or "")[:3000], str(b.get("prizes") or "")[:500], user["uid"])
    await fetch("""
        UPDATE tournaments SET format=$2, mode=$3, rounds=$4, points_win=$5, points_bye=$6, round_maps=$7, map_pool=$8 WHERE id=$1
    """, tid, fmt, {"random3v3": "3v3", "ffa": "FFA"}.get(fmt, f"{num('team_size', 2, 4, 2)}v{num('team_size', 2, 4, 2)}" if fmt == "teamcup" else "1v1"),
        num("rounds", 1, 5, 1) if fmt == "ffa" else rounds, pts("points_win", 0.5, 5, 1.0),
        pts("points_bye", 0, 2, 0.5), round_maps, pool)
    if fmt == "teamcup":
        await fetch("UPDATE tournaments SET team_size=$2 WHERE id=$1", tid, num("team_size", 2, 4, 2))
    if fmt == "ffa":
        try:
            table = [max(0, min(100, int(x))) for x in str(b.get("ffa_points") or "8,6,4,3,2,1,0,0").replace(" ", "").split(",") if x != ""][:8]
        except ValueError:
            table = [8, 6, 4, 3, 2, 1, 0, 0]
        await fetch("UPDATE tournaments SET ffa_points=$2 WHERE id=$1", tid, (table + [0] * 8)[:8])
    await _log_admin(tid, user["uid"], "create", {k: v for k, v in b.items()})
    return {"id": tid}


@app.post("/api/tournaments/{tid}/signup")
async def api_tournament_signup(request: Request, tid: int):
    user = require_login(request)
    require_same_site(request)
    async with _tournament_lock:
        t = await _tournament_or_404(tid)
        if t["status"] not in ("signup", "checkin"):
            raise HTTPException(status_code=400, detail="Sign-ups for this tournament are closed.")
        if is_team_fmt(t):
            raise HTTPException(status_code=400, detail="This is a team tournament: register a team instead.")
        uid = user["uid"]
        link = await _link_row(uid)
        present = await fetchval("SELECT COUNT(*) FROM discord_member_profiles WHERE user_id=$1 AND is_present", uid, default=0)
        if not (present or user.get("g")):
            raise HTTPException(status_code=400, detail="Join The Cheese Club Discord server first.")
        if not (link and (link["profile_id"] or link["bypassed"])):
            raise HTTPException(status_code=400, detail="Link your AoE4 account first.")
        taken = await fetchval("SELECT COUNT(*) FROM tournament_players WHERE tournament_id=$1 AND status='registered'", tid, default=0)
        status = "registered" if taken < t["max_players"] else "waitlist"
        await fetch("""
            INSERT INTO tournament_players (tournament_id, user_id, status) VALUES ($1,$2,$3)
            ON CONFLICT (tournament_id, user_id) DO UPDATE SET status=$3, signed_up_at=NOW(), checked_in_at=NULL
            WHERE tournament_players.status IN ('withdrawn','no_show')
        """, tid, uid, status)
        return {"status": status}


@app.post("/api/tournaments/{tid}/withdraw")
async def api_tournament_withdraw(request: Request, tid: int):
    user = require_login(request)
    require_same_site(request)
    async with _tournament_lock:
        t = await _tournament_or_404(tid)
        if t["status"] not in ("signup", "checkin"):
            raise HTTPException(status_code=400, detail="You can't withdraw once the tournament has started.")
        if is_team_fmt(t):
            uid = user["uid"]
            tm = await fetch("""SELECT tt.* FROM tournament_teams tt JOIN tournament_team_members m ON m.team_id=tt.id
                                WHERE tt.tournament_id=$1 AND m.user_id=$2 AND m.status='accepted'
                                  AND tt.status IN ('forming','registered','waitlist') LIMIT 1""", tid, uid)
            if not tm:
                raise HTTPException(status_code=400, detail="You're not in a team for this tournament.")
            tm = tm[0]
            if tm["captain_id"] == uid:
                await fetch("UPDATE tournament_teams SET status='withdrawn' WHERE id=$1", tm["id"])
                await _notify_entrant(t, f"T{tm['id']}", f"{tm['name']} withdrew from {t['name']}", "Your captain withdrew the team.", f"team{tm['id']}-withdrawn")
                await _promote_waitlist_team(t)
                return {"ok": True}
            await fetch("UPDATE tournament_team_members SET status='declined', responded_at=NOW() WHERE team_id=$1 AND user_id=$2", tm["id"], uid)
            await fetch("UPDATE tournament_teams SET status='forming', checked_in_at=NULL WHERE id=$1", tm["id"])
            me = (await people([uid]))[uid]["username"]
            await _tnotify(tm["captain_id"], "tournament", f"{me} left {tm['name']}", "Invite someone else on the tournament page.", tid, f"team{tm['id']}-{uid}-left")
            await _promote_waitlist_team(t)
            return {"ok": True}
        was = await fetchval("""
            UPDATE tournament_players SET status='withdrawn' WHERE tournament_id=$1 AND user_id=$2
              AND status IN ('registered','waitlist') RETURNING 'x'
        """, tid, user["uid"], default=None)
        if was:   # the first person on the waiting list moves up
            nxt = await fetch("""
                SELECT user_id FROM tournament_players WHERE tournament_id=$1 AND status='waitlist' ORDER BY signed_up_at LIMIT 1
            """, tid)
            taken = await fetchval("SELECT COUNT(*) FROM tournament_players WHERE tournament_id=$1 AND status='registered'", tid, default=0)
            if nxt and taken < t["max_players"]:
                await fetch("UPDATE tournament_players SET status='registered' WHERE tournament_id=$1 AND user_id=$2", tid, nxt[0]["user_id"])
                await _tnotify(nxt[0]["user_id"], "tournament", f"🎉 You're in {t['name']}!", "A spot opened up and you've moved off the waiting list.", tid, f"t{tid}-promoted")
        return {"ok": True}


@app.post("/api/tournaments/{tid}/checkin")
async def api_tournament_checkin(request: Request, tid: int):
    user = require_login(request)
    require_same_site(request)
    t = await _tournament_or_404(tid)
    if t["status"] != "checkin":
        raise HTTPException(status_code=400, detail="Check-in isn't open right now.")
    if is_team_fmt(t):
        tm = await fetch("SELECT * FROM tournament_teams WHERE tournament_id=$1 AND captain_id=$2 AND status IN ('registered','waitlist')", tid, user["uid"])
        if not tm:
            raise HTTPException(status_code=400, detail="Only the captain of a complete team can check in.")
        await fetch("UPDATE tournament_teams SET checked_in_at=NOW() WHERE id=$1", tm[0]["id"])
        return {"ok": True}
    done = await fetchval("""
        UPDATE tournament_players SET checked_in_at=NOW() WHERE tournament_id=$1 AND user_id=$2
          AND status IN ('registered','waitlist') RETURNING 'x'
    """, tid, user["uid"], default=None)
    if not done:
        raise HTTPException(status_code=400, detail="Sign up first.")
    return {"ok": True}


@app.post("/api/tournaments/{tid}/ready")
async def api_tournament_ready(request: Request, tid: int):
    user = require_login(request)
    require_same_site(request)
    b = await request.json()
    ready = bool(b.get("ready", True))
    rows = await fetch("SELECT * FROM tournament_matches WHERE tournament_id=$1 AND id=$2", tid, int(b.get("match_id") or 0))
    if not rows or user["uid"] not in (await entrant_members(rows[0]["p1_id"])) + (await entrant_members(rows[0]["p2_id"])):
        raise HTTPException(status_code=404, detail="That isn't your match.")
    m = rows[0]
    if m["status"] != "ready":
        raise HTTPException(status_code=400, detail="This match isn't waiting for players right now.")
    uid = user["uid"]
    ids = [x for x in m["ready_ids"] if x != uid] + ([uid] if ready else [])
    m1, m2 = await entrant_members(m["p1_id"]), await entrant_members(m["p2_id"])
    # A side is ready when every one of its players is ready (one player in a 1v1 Cup)
    r1 = datetime.now(timezone.utc) if m1 and set(m1) <= set(ids) else None
    r2 = datetime.now(timezone.utc) if m2 and set(m2) <= set(ids) else None
    await fetch("UPDATE tournament_matches SET ready_ids=$2, p1_ready_at=$3, p2_ready_at=$4, updated_at=NOW() WHERE id=$1 AND status='ready'",
                m["id"], ids, r1, r2)
    return {"ok": True}


async def _random_admin(t: dict, action: str, b: dict) -> dict:
    tid = t["id"]
    if t["status"] != "live" and action != "remove_player":
        raise HTTPException(status_code=400, detail="The tournament isn't running.")
    m = None
    if b.get("match_id"):
        rows = await fetch("SELECT * FROM tournament_team_matches WHERE tournament_id=$1 AND id=$2", tid, int(b["match_id"]))
        m = dict(rows[0]) if rows else None
    if action == "team_result":
        if not m:
            raise HTTPException(status_code=404, detail="Match not found.")
        w = int(b.get("winner_team") or 0)
        if w not in (0, 1, 2):
            raise HTTPException(status_code=400, detail="Pick Team 1, Team 2 or No result.")
        await fetch("""
            UPDATE tournament_team_matches SET status=$2, winner_team=$3, note=$4, finished_at=NOW(), updated_at=NOW() WHERE id=$1
        """, m["id"], "done" if w else "void", w or None,
            ("Result corrected by an admin" if m["status"] == "done" else "Result set by an admin") if w else "No result (admin)")
        return {"message": "Result saved. Standings are updated."}
    if action == "substitute":
        if not m or m["status"] != "ready":
            raise HTTPException(status_code=400, detail="Substitutes can only be made before a match starts.")
        out_u, in_u = from_public_id(str(b.get("out_user") or "")), from_public_id(str(b.get("in_user") or ""))
        if out_u not in m["team1"] + m["team2"]:
            raise HTTPException(status_code=400, detail="Pick a player who is in this match.")
        bye = await fetch("""SELECT 1 FROM tournament_round_byes WHERE tournament_id=$1 AND round=$2 AND user_id=$3
                             AND substituted_into IS NULL""", tid, m["round"], in_u)
        if not bye:
            raise HTTPException(status_code=400, detail="The substitute must be someone sitting out this round.")
        t1 = [in_u if u == out_u else u for u in m["team1"]]
        t2 = [in_u if u == out_u else u for u in m["team2"]]
        await fetch("""UPDATE tournament_team_matches SET team1=$2, team2=$3, ready_ids=array_remove(ready_ids, $4),
                       note=$5, announced=FALSE, updated_at=NOW() WHERE id=$1""",
                    m["id"], t1, t2, out_u, "Substitute in")
        await fetch("UPDATE tournament_round_byes SET substituted_into=$4 WHERE tournament_id=$1 AND round=$2 AND user_id=$3",
                    tid, m["round"], in_u, m["id"])
        await _tnotify(in_u, "tournament", f"🔁 You're subbing in for round {m['round']}!", "Press I'm ready on the tournament page.",
                       tid, f"r{m['round']}-sub")
        return {"message": "Substitute made. The new player has been notified."}
    if action == "regenerate_round":
        r = int(t["current_round"] or 0)
        busy = await fetchval("""SELECT COUNT(*) FROM tournament_team_matches WHERE tournament_id=$1 AND round=$2
                                 AND (status <> 'ready' OR cardinality(ready_ids) > 0)""", tid, r, default=0)
        if busy:
            raise HTTPException(status_code=400, detail="Some players already pressed Ready or a match has started, so this round can't be redone.")
        await fetch("DELETE FROM tournament_team_matches WHERE tournament_id=$1 AND round=$2", tid, r)
        await fetch("DELETE FROM tournament_round_byes WHERE tournament_id=$1 AND round=$2", tid, r)
        await fetch("DELETE FROM member_notifications WHERE kind='tournament' AND ref = ANY($1::text[]) AND link=$2",
                    [f"r{r}-team", f"r{r}-bye"], f"/tournament/{tid}")
        t["current_round"] = r - 1
        return {"message": "New teams: " + await _generate_round(t, r)}
    if action == "remove_player":
        uid = from_public_id(str(b.get("user") or ""))
        if t["status"] in ("signup", "checkin"):
            await fetch("UPDATE tournament_players SET status='withdrawn' WHERE tournament_id=$1 AND user_id=$2", tid, uid)
            return {"message": "Player removed."}
        pending = await fetchval("""SELECT COUNT(*) FROM tournament_team_matches WHERE tournament_id=$1 AND status NOT IN ('done','void')
                                    AND $2 = ANY(team1 || team2)""", tid, uid, default=0)
        if pending:
            raise HTTPException(status_code=400, detail="This player is in a match that hasn't finished. Make a substitute or set that match's result first.")
        await fetch("UPDATE tournament_players SET status='removed' WHERE tournament_id=$1 AND user_id=$2", tid, uid)
        return {"message": "Player removed from the remaining rounds. Their points so far are kept."}
    raise HTTPException(status_code=400, detail="Unknown action.")


async def _teams_out(t: dict, uid) -> dict:
    teams = [dict(r) for r in await fetch("""
        SELECT * FROM tournament_teams WHERE tournament_id=$1 AND status NOT IN ('withdrawn')
        ORDER BY seed NULLS LAST, (status='waitlist'), created_at
    """, t["id"])]
    members = [dict(r) for r in await fetch("""
        SELECT * FROM tournament_team_members WHERE tournament_id=$1 AND status <> 'removed' ORDER BY invited_at
    """, t["id"])]
    names = await people(list({m["user_id"] for m in members} | {tm["captain_id"] for tm in teams}))
    out, mine, invites = [], None, []
    for tm in teams:
        ms = [m for m in members if m["team_id"] == tm["id"]]
        item = {"id": tm["id"], "name": tm["name"], "status": tm["status"], "checked_in": bool(tm["checked_in_at"]),
                "seed": tm["seed"], "rating": tm["rating"],
                "captain": {**names.get(tm["captain_id"], {}), "me": tm["captain_id"] == uid},
                "members": [{**names.get(m["user_id"], {}), "status": m["status"], "me": m["user_id"] == uid,
                             "captain": m["user_id"] == tm["captain_id"]} for m in ms]}
        out.append(item)
        if uid and any(m["user_id"] == uid and m["status"] == "accepted" for m in ms) and tm["status"] not in ("no_show",):
            mine = item
        if uid and any(m["user_id"] == uid and m["status"] == "invited" for m in ms) and tm["status"] in ("forming", "registered", "waitlist"):
            invites.append(item)
    return {"size": int(t["team_size"]), "list": out, "mine": mine, "invites": invites}


async def _eligible(user: dict) -> Optional[str]:
    uid = user["uid"]
    present = await fetchval("SELECT COUNT(*) FROM discord_member_profiles WHERE user_id=$1 AND is_present", uid, default=0)
    if not (present or user.get("g")):
        return "Join The Cheese Club Discord server first."
    link = await _link_row(uid)
    if not (link and (link["profile_id"] or link["bypassed"])):
        return "Link your AoE4 account first."
    return None


async def _team_busy(tid: int, uid: str) -> bool:
    return bool(await fetchval("""
        SELECT COUNT(*) FROM tournament_team_members m JOIN tournament_teams tt ON tt.id = m.team_id
        WHERE m.tournament_id=$1 AND m.user_id=$2 AND m.status IN ('accepted','invited')
          AND tt.status NOT IN ('withdrawn','no_show')
    """, tid, uid, default=0))


async def _team_complete_check(t: dict, team_id: int):
    """When every member has accepted, the team is registered (or waitlisted if full)."""
    tm = (await fetch("SELECT * FROM tournament_teams WHERE id=$1", team_id))[0]
    if tm["status"] != "forming":
        return
    accepted = await fetchval("SELECT COUNT(*) FROM tournament_team_members WHERE team_id=$1 AND status='accepted'", team_id, default=0)
    if accepted < int(t["team_size"]):
        return
    taken = await fetchval("SELECT COUNT(*) FROM tournament_teams WHERE tournament_id=$1 AND status='registered'", t["id"], default=0)
    status = "registered" if taken < t["max_players"] else "waitlist"
    await fetch("UPDATE tournament_teams SET status=$2 WHERE id=$1", team_id, status)
    await _tnotify(tm["captain_id"], "tournament", f"✅ {tm['name']} is complete!",
                   "Everyone accepted. " + ("You're in the tournament." if status == "registered" else "You're on the waiting list."),
                   t["id"], f"team{team_id}-complete")


async def _promote_waitlist_team(t: dict):
    taken = await fetchval("SELECT COUNT(*) FROM tournament_teams WHERE tournament_id=$1 AND status='registered'", t["id"], default=0)
    if taken >= t["max_players"]:
        return
    nxt = await fetch("SELECT id, name FROM tournament_teams WHERE tournament_id=$1 AND status='waitlist' ORDER BY created_at LIMIT 1", t["id"])
    if nxt:
        await fetch("UPDATE tournament_teams SET status='registered' WHERE id=$1", nxt[0]["id"])
        await _notify_entrant(t, f"T{nxt[0]['id']}", f"🎉 {nxt[0]['name']} is in {t['name']}!", "A spot opened up and your team moved off the waiting list.",
                              f"team{nxt[0]['id']}-promoted")


@app.post("/api/tournaments/{tid}/team")
async def api_team_register(request: Request, tid: int):
    user = require_login(request)
    require_same_site(request)
    b = await request.json()
    async with _tournament_lock:
        t = await _tournament_or_404(tid)
        if not is_team_fmt(t):
            raise HTTPException(status_code=400, detail="This tournament doesn't use teams.")
        if t["status"] not in ("signup", "checkin"):
            raise HTTPException(status_code=400, detail="Sign-ups for this tournament are closed.")
        problem = await _eligible(user)
        if problem:
            raise HTTPException(status_code=400, detail=problem)
        name = " ".join(str(b.get("name") or "").split())[:40]
        if len(name) < 2:
            raise HTTPException(status_code=400, detail="Give your team a name.")
        if await fetchval("SELECT COUNT(*) FROM tournament_teams WHERE tournament_id=$1 AND LOWER(name)=LOWER($2) AND status NOT IN ('withdrawn','no_show')", tid, name, default=0):
            raise HTTPException(status_code=400, detail="Another team already has that name.")
        uid = user["uid"]
        mates = []
        for pid in (b.get("members") or []):
            m = from_public_id(str(pid))
            if m and m != uid and m not in mates:
                mates.append(m)
        if len(mates) != int(t["team_size"]) - 1:
            raise HTTPException(status_code=400, detail=f"Pick {int(t['team_size']) - 1} teammate{'s' if int(t['team_size']) > 2 else ''}.")
        for m in [uid] + mates:
            if await _team_busy(tid, m):
                who = "You're" if m == uid else f"{(await people([m]))[m]['username']} is"
                raise HTTPException(status_code=400, detail=f"{who} already in a team for this tournament.")
        team_id = await fetchval("INSERT INTO tournament_teams (tournament_id, name, captain_id) VALUES ($1,$2,$3) RETURNING id", tid, name, uid)
        await fetch("INSERT INTO tournament_team_members (team_id, tournament_id, user_id, status, responded_at) VALUES ($1,$2,$3,'accepted',NOW())", team_id, tid, uid)
        cap = (await people([uid]))[uid]["username"]
        for m in mates:
            await fetch("INSERT INTO tournament_team_members (team_id, tournament_id, user_id) VALUES ($1,$2,$3)", team_id, tid, m)
            await _tnotify(m, "tournament", f"👥 {cap} invited you to {name}", f"For {t['name']}. Accept or decline on the tournament page.", tid, f"team{team_id}-invite")
        return {"team_id": team_id}


@app.post("/api/tournaments/{tid}/team-respond")
async def api_team_respond(request: Request, tid: int):
    user = require_login(request)
    require_same_site(request)
    b = await request.json()
    accept, team_id, uid = bool(b.get("accept")), int(b.get("team_id") or 0), user["uid"]
    async with _tournament_lock:
        t = await _tournament_or_404(tid)
        row = await fetch("SELECT * FROM tournament_team_members WHERE team_id=$1 AND user_id=$2 AND status='invited'", team_id, uid)
        tm = await fetch("SELECT * FROM tournament_teams WHERE id=$1 AND tournament_id=$2", team_id, tid)
        if not row or not tm:
            raise HTTPException(status_code=404, detail="That invite isn't open any more.")
        if t["status"] not in ("signup", "checkin"):
            raise HTTPException(status_code=400, detail="Sign-ups for this tournament are closed.")
        if accept:
            problem = await _eligible(user)
            if problem:
                raise HTTPException(status_code=400, detail=problem)
            if await fetchval("""SELECT COUNT(*) FROM tournament_team_members m JOIN tournament_teams tt ON tt.id=m.team_id
                                  WHERE m.tournament_id=$1 AND m.user_id=$2 AND m.status='accepted' AND tt.status NOT IN ('withdrawn','no_show')""",
                              tid, uid, default=0):
                raise HTTPException(status_code=400, detail="You're already in another team for this tournament.")
        await fetch("UPDATE tournament_team_members SET status=$3, responded_at=NOW() WHERE team_id=$1 AND user_id=$2",
                    team_id, uid, "accepted" if accept else "declined")
        if accept:   # decline every other invite in this tournament
            await fetch("""UPDATE tournament_team_members SET status='declined', responded_at=NOW()
                           WHERE tournament_id=$1 AND user_id=$2 AND status='invited' AND team_id<>$3""", tid, uid, team_id)
        me = (await people([uid]))[uid]["username"]
        await _tnotify(tm[0]["captain_id"], "tournament", f"{'✅' if accept else '❌'} {me} {'joined' if accept else 'declined'} {tm[0]['name']}",
                       "" if accept else "Pick someone else on the tournament page.", tid, f"team{team_id}-{uid}-{'yes' if accept else 'no'}")
        await _team_complete_check(t, team_id)
        return {"ok": True}


@app.post("/api/tournaments/{tid}/team-replace")
async def api_team_replace(request: Request, tid: int):
    user = require_login(request)
    require_same_site(request)
    b = await request.json()
    async with _tournament_lock:
        t = await _tournament_or_404(tid)
        tm = await fetch("SELECT * FROM tournament_teams WHERE id=$1 AND tournament_id=$2", int(b.get("team_id") or 0), tid)
        if not tm or tm[0]["captain_id"] != user["uid"]:
            raise HTTPException(status_code=403, detail="Only the team captain can change the team.")
        if t["status"] not in ("signup", "checkin") or tm[0]["status"] != "forming":
            raise HTTPException(status_code=400, detail="This team can't be changed now.")
        old, new = from_public_id(str(b.get("old_user") or "")), from_public_id(str(b.get("new_user") or ""))
        row = await fetch("SELECT status FROM tournament_team_members WHERE team_id=$1 AND user_id=$2", tm[0]["id"], old)
        if not row or row[0]["status"] not in ("invited", "declined"):
            raise HTTPException(status_code=400, detail="Pick a teammate who hasn't accepted (or who declined).")
        if not new or await _team_busy(tid, new):
            raise HTTPException(status_code=400, detail="That player is already in a team for this tournament.")
        await fetch("UPDATE tournament_team_members SET status='removed' WHERE team_id=$1 AND user_id=$2", tm[0]["id"], old)
        await fetch("""INSERT INTO tournament_team_members (team_id, tournament_id, user_id) VALUES ($1,$2,$3)
                       ON CONFLICT (team_id, user_id) DO UPDATE SET status='invited', invited_at=NOW(), responded_at=NULL""", tm[0]["id"], tid, new)
        cap = (await people([user["uid"]]))[user["uid"]]["username"]
        await _tnotify(new, "tournament", f"👥 {cap} invited you to {tm[0]['name']}", f"For {t['name']}. Accept or decline on the tournament page.",
                       tid, f"team{tm[0]['id']}-invite-{new}")
        return {"ok": True}


# ---------------- FFA ----------------
async def _ffa_round(t: dict, r: int) -> str:
    import random as _random
    tid = t["id"]
    players = [x["user_id"] for x in await fetch("SELECT user_id FROM tournament_players WHERE tournament_id=$1 AND status='playing'", tid)]
    maps = list(t.get("round_maps") or [])
    chosen = maps[r - 1] if len(maps) >= r and maps[r - 1] and maps[r - 1] != "random" else None
    map_name = chosen or _random.choice(list(t.get("map_pool") or []) or DEFAULT_MAPS)
    await fetch("INSERT INTO tournament_ffa_matches (tournament_id, round, players, map) VALUES ($1,$2,$3,$4)", tid, r, players, map_name)
    await fetch("UPDATE tournaments SET current_round=$2 WHERE id=$1", tid, r)
    rounds = int(t.get("rounds") or 1)
    for uid in players:
        await _tnotify(uid, "tournament", f"🏁 {'Round ' + str(r) + ' lobby' if rounds > 1 else 'Your FFA match'} is ready",
                       f"{t['name']} · {len(players)} players · Map: {map_name}. Press I'm ready on the tournament page.", tid, f"ffa{r}-ready")
    return f"Round {r}: {len(players)} players on {map_name}."


def _ffa_points(t: dict, place: int) -> int:
    pts = list(t.get("ffa_points") or [8, 6, 4, 3, 2, 1, 0, 0])
    return int(pts[place - 1]) if 1 <= place <= len(pts) else 0


async def ffa_standings(t: dict) -> list:
    players = await fetch("""SELECT user_id, rating, status FROM tournament_players WHERE tournament_id=$1
                             AND status IN ('playing','removed','champion','eliminated')""", t["id"])
    st = {p["user_id"]: {"user_id": p["user_id"], "rating": int(p["rating"] or 0), "status": p["status"], "points": 0, "played": 0,
                         "wins": 0, "podiums": 0, "places": []} for p in players}
    for m in await fetch("SELECT round, placements FROM tournament_ffa_matches WHERE tournament_id=$1 AND status='done' ORDER BY round", t["id"]):
        for uid, place in json_obj_safe(m["placements"]).items():
            if uid in st:
                s = st[uid]; place = int(place)
                s["points"] += _ffa_points(t, place); s["played"] += 1; s["places"].append(place)
                s["wins"] += place == 1; s["podiums"] += place <= 3
    rows = sorted(st.values(), key=lambda s: (-s["points"], -s["wins"], -s["podiums"],
                                              (sum(s["places"]) / len(s["places"])) if s["places"] else 99, -s["rating"]))
    for i, s in enumerate(rows, 1):
        s["rank"] = i
        s["avg_place"] = round(sum(s["places"]) / len(s["places"]), 1) if s["places"] else None
    return rows


async def _ffa_finalize(t: dict, mid: int, placements: dict, note: str = None):
    rows = await fetch("""UPDATE tournament_ffa_matches SET status='done', placements=$2, note=COALESCE($3, note), finished_at=NOW(), updated_at=NOW()
                          WHERE id=$1 RETURNING round""", mid, json.dumps(placements), note)
    if not rows:
        return
    r = rows[0]["round"]
    for uid, place in placements.items():
        await _tnotify(uid, "tournament", f"🏁 You finished #{place}: +{_ffa_points(t, int(place))} points", t["name"], t["id"], f"ffa{r}-place")
    t = await _tournament_or_404(t["id"])
    if t["status"] == "finished":
        await _finish_ffa(t, quiet=True)          # a corrected result: recalculate the podium
    elif r >= int(t.get("rounds") or 1):
        await _finish_ffa(t)
    elif int(t["current_round"]) == r:
        await _ffa_round(t, r + 1)


async def _finish_ffa(t: dict, quiet: bool = False):
    rows = await ffa_standings(t)
    top = [r["user_id"] for r in rows[:3]]
    await fetch("""UPDATE tournaments SET status='finished', finished_at=COALESCE(finished_at, NOW()), winner_id=$2, runner_up_id=$3, third_id=$4
                   WHERE id=$1""", t["id"], top[0] if top else None, top[1] if len(top) > 1 else None, top[2] if len(top) > 2 else None)
    await fetch("UPDATE tournament_players SET status='playing' WHERE tournament_id=$1 AND status='champion'", t["id"])
    if top:
        await fetch("UPDATE tournament_players SET status='champion' WHERE tournament_id=$1 AND user_id=$2", t["id"], top[0])
    if quiet:
        return
    medals = ["🏆 You won", "🥈 Runner-up in", "🥉 Third place in"]
    for r in rows:
        title = f"{medals[r['rank'] - 1]} {t['name']}!" if r["rank"] <= 3 else f"{t['name']} is over: you finished #{r['rank']}"
        await _tnotify(r["user_id"], "tournament", title, f"{r['points']} points. GG!", t["id"], f"t{t['id']}-place")


def _ffa_needed(n: int) -> int:
    """Confirmations needed (the reporter counts as one): both players in a 2-player FFA, otherwise half, at least 2."""
    return max(2, -(-n // 2))


async def _ffa_out(t: dict, uid) -> dict:
    ms = [dict(r) for r in await fetch("SELECT * FROM tournament_ffa_matches WHERE tournament_id=$1 ORDER BY round", t["id"])]
    names = await people(list({u for m in ms for u in m["players"]}))
    out = []
    for m in ms:
        pl = json_obj_safe(m["placements"]) if m["placements"] else {}
        order = sorted(pl, key=lambda u: int(pl[u])) if pl else []
        out.append({"id": m["id"], "round": m["round"], "map": m["map"], "status": m["status"], "note": m["note"],
                    "players": [{**names.get(u, {}), "me": u == uid, "ready": u in m["ready_ids"]} for u in m["players"]],
                    "ready_count": len(set(m["ready_ids"]) & set(m["players"])),
                    "results": [{**names.get(u, {}), "me": u == uid, "place": int(pl[u]), "points": _ffa_points(t, int(pl[u]))} for u in order],
                    "reported_by": names.get(m["reported_by"]) if m["reported_by"] else None,
                    "confirmations": len(m["confirmations"]), "needed": _ffa_needed(len(m["players"])),
                    "me_in": bool(uid and uid in m["players"]), "me_ready": bool(uid and uid in m["ready_ids"]),
                    "me_confirmed": bool(uid and uid in m["confirmations"]), "me_disputed": bool(uid and uid in m["disputes"])})
    standings = await ffa_standings(t)
    sn = await people([s["user_id"] for s in standings])
    return {"matches": out, "points": list(t.get("ffa_points") or []),
            "standings": [{**sn.get(s["user_id"], {}), "me": s["user_id"] == uid,
                           **{k: s[k] for k in ("rank", "points", "played", "wins", "podiums", "avg_place", "status")}} for s in standings]}


async def _ffa_match(tid: int, mid: int) -> dict:
    rows = await fetch("SELECT * FROM tournament_ffa_matches WHERE tournament_id=$1 AND id=$2", tid, mid)
    if not rows:
        raise HTTPException(status_code=404, detail="Match not found.")
    return dict(rows[0])


def _parse_order(m: dict, order) -> dict:
    ids = [from_public_id(str(x)) for x in (order or [])]
    if sorted(ids) != sorted(m["players"]) or len(set(ids)) != len(ids):
        raise HTTPException(status_code=400, detail="Put every player in exactly one place.")
    return {u: i for i, u in enumerate(ids, 1)}


@app.post("/api/tournaments/{tid}/ffa-ready")
async def api_ffa_ready(request: Request, tid: int):
    user = require_login(request)
    require_same_site(request)
    b = await request.json()
    m = await _ffa_match(tid, int(b.get("match_id") or 0))
    if user["uid"] not in m["players"] or m["status"] != "ready":
        raise HTTPException(status_code=400, detail="This lobby isn't waiting for you right now.")
    if b.get("ready", True):
        await fetch("UPDATE tournament_ffa_matches SET ready_ids=array_append(array_remove(ready_ids,$2),$2), updated_at=NOW() WHERE id=$1 AND status='ready'", m["id"], user["uid"])
    else:
        await fetch("UPDATE tournament_ffa_matches SET ready_ids=array_remove(ready_ids,$2), updated_at=NOW() WHERE id=$1", m["id"], user["uid"])
    return {"ok": True}


@app.post("/api/tournaments/{tid}/ffa-report")
async def api_ffa_report(request: Request, tid: int):
    user = require_login(request)
    require_same_site(request)
    b = await request.json()
    async with _tournament_lock:
        t = await _tournament_or_404(tid)
        m = await _ffa_match(tid, int(b.get("match_id") or 0))
        if user["uid"] not in m["players"]:
            raise HTTPException(status_code=403, detail="Only players in this match can report the result.")
        if m["status"] not in ("playing", "reported"):
            raise HTTPException(status_code=400, detail="This match can't be reported right now.")
        pl = _parse_order(m, b.get("order"))
        await fetch("""UPDATE tournament_ffa_matches SET status='reported', placements=$2, reported_by=$3, confirmations=$4, disputes='{}', updated_at=NOW()
                       WHERE id=$1""", m["id"], json.dumps(pl), user["uid"], [user["uid"]])
        me = (await people([user["uid"]]))[user["uid"]]["username"]
        for u in m["players"]:
            if u != user["uid"]:
                await _tnotify(u, "tournament", f"🏁 {me} reported the placements", "Please confirm or dispute them on the tournament page.",
                               tid, f"ffa{m['id']}-report-{int(time.time())}")
        if _ffa_needed(len(m["players"])) <= 1:
            await _ffa_finalize(t, m["id"], pl)
        return {"ok": True}


@app.post("/api/tournaments/{tid}/ffa-confirm")
async def api_ffa_confirm(request: Request, tid: int):
    user = require_login(request)
    require_same_site(request)
    b = await request.json()
    async with _tournament_lock:
        t = await _tournament_or_404(tid)
        m = await _ffa_match(tid, int(b.get("match_id") or 0))
        uid = user["uid"]
        if uid not in m["players"] or m["status"] != "reported":
            raise HTTPException(status_code=400, detail="There's nothing to confirm right now.")
        if b.get("agree"):
            confs = list(dict.fromkeys(list(m["confirmations"]) + [uid]))
            await fetch("UPDATE tournament_ffa_matches SET confirmations=$2, updated_at=NOW() WHERE id=$1", m["id"], confs)
            if len(confs) >= _ffa_needed(len(m["players"])) and not m["disputes"]:
                await _ffa_finalize(t, m["id"], json_obj_safe(m["placements"]), note="Confirmed by players")
            return {"ok": True}
        await fetch("UPDATE tournament_ffa_matches SET status='disputed', disputes=array_append(disputes,$2), updated_at=NOW() WHERE id=$1", m["id"], uid)
        for a in ADMIN_IDS:
            await _tnotify(a, "tournament", f"⚠️ FFA result disputed in {t['name']}", "An admin needs to set the placements.", tid, f"ffa{m['id']}-dispute")
        return {"ok": True}


@app.post("/api/tournaments/{tid}/team-ready")
async def api_team_ready(request: Request, tid: int):
    user = require_login(request)
    require_same_site(request)
    b = await request.json()
    uid, ready = user["uid"], bool(b.get("ready", True))
    rows = await fetch("SELECT * FROM tournament_team_matches WHERE tournament_id=$1 AND id=$2", tid, int(b.get("match_id") or 0))
    if not rows or uid not in rows[0]["team1"] + rows[0]["team2"]:
        raise HTTPException(status_code=404, detail="That isn't your match.")
    if rows[0]["status"] != "ready":
        raise HTTPException(status_code=400, detail="This match isn't waiting for players right now.")
    if ready:
        await fetch("""UPDATE tournament_team_matches SET ready_ids = array_append(array_remove(ready_ids, $2), $2), updated_at=NOW()
                       WHERE id=$1 AND status='ready'""", rows[0]["id"], uid)
    else:
        await fetch("UPDATE tournament_team_matches SET ready_ids = array_remove(ready_ids, $2), updated_at=NOW() WHERE id=$1",
                    rows[0]["id"], uid)
    return {"ok": True}


@app.post("/api/tournaments/{tid}/admin")
async def api_tournament_admin(request: Request, tid: int):
    admin = require_admin(request)
    require_same_site(request)
    b = await request.json()
    action = b.get("action")
    async with _tournament_lock:
        t = await _tournament_or_404(tid)
        await _log_admin(tid, admin["uid"], action or "?", {k: v for k, v in b.items() if k != "action"})
        if action == "delete":
            if " ".join(str(b.get("confirm_name") or "").split()).lower() != " ".join(t["name"].split()).lower():
                raise HTTPException(status_code=400, detail="Type the tournament's exact name to delete it.")
            await fetch("DELETE FROM member_notifications WHERE link=$1", f"/tournament/{tid}")
            await fetch("DELETE FROM tournaments WHERE id=$1", tid)
            return {"message": "Tournament deleted.", "deleted": True}
        if t.get("format") == "ffa" and action in ("ffa_result", "ffa_approve"):
            m = await _ffa_match(tid, int(b.get("match_id") or 0))
            if action == "ffa_approve":
                if m["status"] not in ("reported", "disputed") or not m["placements"]:
                    raise HTTPException(status_code=400, detail="There's no reported result to approve.")
                await _ffa_finalize(t, m["id"], json_obj_safe(m["placements"]), note="Approved by an admin")
                return {"message": "Result approved."}
            if m["status"] in ("ready", "starting"):
                await fetch("UPDATE tournament_ffa_matches SET status='playing' WHERE id=$1", m["id"])
            pl = _parse_order(m, b.get("order"))
            await _ffa_finalize(t, m["id"], pl, note="Corrected by an admin" if m["status"] == "done" else "Set by an admin")
            return {"message": "Placements saved. Standings are updated."}
        if t.get("format") == "random3v3" and action in ("team_result", "substitute", "regenerate_round", "remove_player"):
            return await _random_admin(t, action, b)
        if action == "start":
            if t["status"] not in ("signup", "checkin"):
                raise HTTPException(status_code=400, detail="This tournament has already started.")
            if t["status"] == "signup":
                # starting early: everyone signed up counts as checked in
                await fetch("UPDATE tournament_players SET checked_in_at=COALESCE(checked_in_at, NOW()) WHERE tournament_id=$1 AND status IN ('registered','waitlist')", tid)
            return {"message": await _start_tournament(t)}
        if action == "open_checkin":
            if t["status"] != "signup":
                raise HTTPException(status_code=400, detail="Check-in is already open or over.")
            await fetch("UPDATE tournaments SET status='checkin' WHERE id=$1", tid)
            return {"message": "Check-in is open."}
        if action == "cancel":
            if t["status"] in ("finished", "cancelled"):
                raise HTTPException(status_code=400, detail="This tournament is already over.")
            await fetch("UPDATE tournaments SET status='cancelled', finished_at=NOW() WHERE id=$1", tid)
            await fetch("UPDATE tournament_games SET status='void' WHERE status='playing' AND tmatch_id IN (SELECT id FROM tournament_matches WHERE tournament_id=$1)", tid)
            return {"message": "Tournament cancelled."}
        if action == "set_winner":
            m = await fetch("SELECT * FROM tournament_matches WHERE tournament_id=$1 AND id=$2", tid, int(b.get("match_id") or 0))
            win = str(b.get("winner") or "") if is_team_fmt(t) else from_public_id(str(b.get("winner") or ""))
            if not m or m[0]["status"] == "done" or win not in (m[0]["p1_id"], m[0]["p2_id"]):
                raise HTTPException(status_code=400, detail="Pick one of the two players in an unfinished match.")
            await _finish_match(m[0]["id"], win, t, note="Decided by an admin")
            return {"message": "Winner set."}
        if action == "remove_player":
            uid = str(b.get("user") or "") if is_team_fmt(t) else from_public_id(str(b.get("user") or ""))
            if t["status"] in ("signup", "checkin"):
                if is_team_fmt(t):
                    await fetch("UPDATE tournament_teams SET status='withdrawn' WHERE id=$1 AND tournament_id=$2", int(uid[1:]), tid)
                    await _promote_waitlist_team(t)
                    return {"message": "Team removed."}
                await fetch("UPDATE tournament_players SET status='withdrawn' WHERE tournament_id=$1 AND user_id=$2", tid, uid)
                return {"message": "Player removed."}
            if t.get("format") == "ffa":
                await fetch("UPDATE tournament_players SET status='removed' WHERE tournament_id=$1 AND user_id=$2", tid, uid)
                return {"message": "Player removed from the remaining rounds. Place them last in any unfinished lobby."}
            m = await fetch("""
                SELECT * FROM tournament_matches WHERE tournament_id=$1 AND status <> 'done' AND $2 IN (p1_id, p2_id)
                  AND p1_id IS NOT NULL AND p2_id IS NOT NULL LIMIT 1
            """, tid, uid)
            if not m:
                raise HTTPException(status_code=400, detail="That player has no match waiting to be played.")
            other = m[0]["p2_id"] if uid == m[0]["p1_id"] else m[0]["p1_id"]
            await _finish_match(m[0]["id"], other, t, note="Forfeit")
            return {"message": "Player removed; their opponent advances."}
    raise HTTPException(status_code=400, detail="Unknown action.")


@app.post("/api/me/privacy")
async def api_privacy(request: Request):
    user = require_login(request)
    require_same_site(request)
    body = await request.json()
    hidden = bool(body.get("hidden"))
    await fetch("""
        INSERT INTO website_member_settings (user_id, hidden, updated_at) VALUES ($1, $2, NOW())
        ON CONFLICT (user_id) DO UPDATE SET hidden=$2, updated_at=NOW()
    """, user["uid"], hidden)
    await refresh_hidden()
    return {"ok": True, "hidden": hidden}


# ---------------------------------------------------------------------------
# Pages
# Pages are served with link-preview tags filled in: %%SITE_URL%% becomes the
# site's address, and player pages get the player's name and record.
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory="static"), name="static")

SITE_URL = os.getenv("SITE_URL", "").rstrip("/")
_page_cache: dict = {}

PAGES = {
    "/": "index.html",
    "/about": "about.html",
    "/civs": "civs.html",
    "/community": "community.html",
    "/leaderboard": "leaderboard.html",
    "/matches": "matches.html",
    "/voice-activity": "voice-activity.html",
    "/privacy": "privacy.html",
    "/tournament": "tournament.html",
    "/tournament/new": "tournament_new.html",
    "/contact": "contact.html",
    "/login": "login.html",
    "/account": "account.html",
    "/dashboard": "dashboard.html",
    "/stats": "stats.html",
    "/settings": "settings.html",
    "/queue": "queue.html",
}


def site_url(request: Request) -> str:
    if SITE_URL:
        return SITE_URL
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc)).split(",")[0].strip()
    return f"{proto}://{host}"


def _template(filename: str) -> str:
    path = f"static/{filename}"
    mtime = os.path.getmtime(path)
    cached = _page_cache.get(path)
    if not cached or cached[0] != mtime:
        with open(path, encoding="utf-8") as fh:
            cached = (mtime, fh.read())
        _page_cache[path] = cached
    return cached[1]


def render_page(filename: str, request: Request, **values) -> HTMLResponse:
    text = _template(filename).replace("%%SITE_URL%%", site_url(request))
    for key, value in values.items():
        text = text.replace(f"%%{key}%%", html.escape(str(value), quote=True))
    return HTMLResponse(text)


def _page_route(filename: str):
    async def page(request: Request):
        return render_page(filename, request)
    return page


for _path, _file in PAGES.items():
    app.add_api_route(_path, _page_route(_file), methods=["GET"], include_in_schema=False)


@app.get("/tournament/{tid}", include_in_schema=False)
async def tournament_page(request: Request, tid: str):
    if not tid.isdigit():
        raise HTTPException(status_code=404)
    title, desc = "Tournament — The Cheese Club", "A Cheese Club tournament: sign up, check in and follow the bracket live."
    try:
        t = await _tournament_or_404(int(tid))
        title = f"{t['name']} — The Cheese Club"
        desc = {"signup": "Sign-ups are open.", "checkin": "Check-in is open.", "live": "Live now: follow the bracket.",
                "finished": "Final results and bracket.", "cancelled": "This tournament was cancelled."}.get(t["status"], desc)
        desc = f"{t['mode']} tournament · {desc}"
    except Exception:
        pass
    return render_page("tournament_detail.html", request, OG_TITLE=title, OG_DESCRIPTION=desc, OG_PATH=f"/tournament/{tid}")


@app.get("/signup", include_in_schema=False)
async def signup_redirect():
    # Signing up happens the first time someone logs in with Discord.
    return RedirectResponse("/login", status_code=301)


@app.get("/player/{player_id}", include_in_schema=False)
async def player_page(player_id: str, request: Request):
    title, description = "Player profile — The Cheese Club", "Community match record, civilizations and achievements."
    user_id = from_public_id(player_id)
    if user_id and user_id not in HIDDEN_IDS and pool is not None:
        try:
            info = await people([user_id])
            name = info[user_id]["username"]
            totals = await fetch("""
                SELECT COALESCE(SUM(wins),0)::int AS wins, COALESCE(SUM(losses),0)::int AS losses
                FROM aoe_queue_stats WHERE user_id=$1
            """, user_id)
            ins = compute_insights(user_id, await load_club_matches())
            w, l = (totals[0]["wins"], totals[0]["losses"]) if totals else (0, 0)
            title = f"{name} — The Cheese Club"
            description = f"{w} wins and {l} losses in Cheese Club community matches."
            if ins["favourite_civ"]:
                description += f" Favourite civilization: {ins['favourite_civ']}."
        except Exception:
            log.exception("Could not build link preview for player page")
    return render_page("player.html", request, OG_TITLE=title, OG_DESCRIPTION=description,
                       OG_PATH=f"/player/{player_id}")
