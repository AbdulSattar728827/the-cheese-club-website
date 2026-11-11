import os
import re
import json
import time
import hmac
import asyncio
import hashlib
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

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
    yield
    if pool:
        await pool.close()
    await http_client.aclose()


app = FastAPI(title="The Cheese Club API", version="0.2.0", lifespan=lifespan)


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
    active_matches = await fetchval("SELECT COUNT(*) FROM aoe_matches WHERE result = 'pending'")
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
    return await add_official_ratings(rows)


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
    return [publicize(r) for r in rows]


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
    return [publicize(r) for r in rows]


@app.get("/api/matches/{match_id}")
async def match_detail(match_id: int):
    """Return a complete, JSON-safe match detail record for the website modal."""
    rows = await fetch("""
        SELECT id, queue_type, player_ids, team1_ids, team2_ids,
               result, civ_data, to_jsonb(aoe_matches)->'civ_bans' AS civ_bans, created_at, finished_at
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
    all_ids = list(dict.fromkeys(team1 + team2 + player_ids))

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
        elo_rows = await fetch("""
            SELECT user_id, ROUND(AVG(elo))::int AS elo
            FROM aoe_queue_stats WHERE user_id = ANY($1::text[])
            GROUP BY user_id
        """, all_ids)
        for uid in all_ids:
            players[uid] = {"user_id": to_public_id(uid), "id_hint": id_hint(uid),
                            "username": None, "avatar_url": None, "elo": None}
        for r in profile_rows:
            players[str(r["user_id"])].update(username=r["username"], avatar_url=r["avatar_url"])
        for r in vc_rows:
            item = players[str(r["user_id"])]
            item["username"] = item["username"] or r["username"]
        for r in elo_rows:
            players[str(r["user_id"])]["elo"] = r["elo"]
        for uid, item in players.items():
            item["username"] = item["username"] or f"Player {uid[-4:]}"
            civ_value = civs.get(uid)
            if isinstance(civ_value, dict):
                civ_value = civ_value.get("name") or civ_value.get("civilization") or civ_value.get("civ")
            item["civ"] = str(civ_value) if civ_value else "Not recorded"

    return {
        "id": row["id"],
        "queue_type": row["queue_type"],
        "result": row["result"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "player_count": len(all_ids),
        "civ_bans": civ_bans,
        "team1": [players[x] for x in team1 if x in players],
        "team2": [players[x] for x in team2 if x in players],
    }


@app.get("/api/matches")
async def matches(limit: int = 100, search: str = ""):
    """List matches, optionally searching by match ID, Discord name or AoE4 name."""
    limit = max(1, min(limit, 250))
    search = clean_search(search, 100)
    rows = await fetch("""
        SELECT m.id, m.queue_type, m.player_ids, m.result, m.created_at, m.finished_at
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
    if user_id is None:
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
# Pages
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory="static"), name="static")

PAGES = {
    "/": "index.html",
    "/community": "community.html",
    "/leaderboard": "leaderboard.html",
    "/matches": "matches.html",
    "/voice-activity": "voice-activity.html",
    "/tournament": "tournament.html",
    "/contact": "contact.html",
    "/login": "login.html",
    "/signup": "signup.html",
}


def _page_route(filename: str):
    async def page():
        return FileResponse(f"static/{filename}")
    return page


for _path, _file in PAGES.items():
    app.add_api_route(_path, _page_route(_file), methods=["GET"], include_in_schema=False)
