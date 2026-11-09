import os
import asyncio
import time
import httpx
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

DATABASE_URL = os.getenv("DATABASE_URL")
pool: Optional[asyncpg.Pool] = None
AOE4WORLD_API = "https://aoe4world.com/api/v0"
_rating_cache = {}
_rating_lock = asyncio.Lock()
_rating_semaphore = asyncio.Semaphore(5)
RATING_CACHE_SECONDS = 1800

async def aoe4world_ratings(profile_id):
    """Fetch and cache current official AoE4World Solo and Team Ranked ratings."""
    if not profile_id:
        return {"solo_elo": None, "team_elo": None}
    key = str(profile_id)
    now = time.monotonic()
    cached = _rating_cache.get(key)
    if cached and now - cached[0] < RATING_CACHE_SECONDS:
        return cached[1]
    async with _rating_semaphore:
        cached = _rating_cache.get(key)
        if cached and now - cached[0] < RATING_CACHE_SECONDS:
            return cached[1]
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(f"{AOE4WORLD_API}/players/{key}")
                response.raise_for_status()
                data = response.json()
            modes = data.get("modes") or {}
            solo = modes.get("rm_solo") or {}
            team = modes.get("rm_team") or {}
            result = {
                "solo_elo": solo.get("rating"),
                "team_elo": team.get("rating"),
            }
            _rating_cache[key] = (now, result)
            return result
        except Exception:
            # Do not replace a previously good cached value with a failure.
            return cached[1] if cached else {"solo_elo": None, "team_elo": None}

async def add_official_ratings(rows):
    async def enrich(row):
        item = dict(row)
        ratings = await aoe4world_ratings(item.get("profile_id"))
        item.update(ratings)
        return item
    return await asyncio.gather(*(enrich(row) for row in rows))

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    if DATABASE_URL:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=15)
        async with pool.acquire() as conn:
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

app = FastAPI(title="The Cheese Club API", version="0.1.0", lifespan=lifespan)

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
    total_vc = await fetchval("SELECT COUNT(*) FROM vc_stats")
    total_aoe_players = await fetchval("SELECT COUNT(DISTINCT user_id) FROM aoe_queue_stats")
    total_matches = await fetchval("SELECT COUNT(*) FROM aoe_matches WHERE result <> 'pending'")
    active_matches = await fetchval("SELECT COUNT(*) FROM aoe_matches WHERE result = 'pending'")
    vc_seconds = await fetchval("SELECT COALESCE(SUM(total_secs),0) FROM vc_stats")
    return {
        "vc_players": total_vc,
        "aoe_players": total_aoe_players,
        "completed_matches": total_matches,
        "active_matches": active_matches,
        "total_vc_seconds": float(vc_seconds or 0),
    }

@app.get("/api/leaderboards/aoe")
async def aoe_leaderboard(queue: str = "all", limit: int = 100, scope: str = "public"):
    limit = max(1, min(limit, 500))
    min_games = 5 if scope == "community" else 1
    if queue == "all":
        rows = await fetch("""
            SELECT user_id,
                   COALESCE(
                       (SELECT aoe4_name FROM aoe4_links l WHERE l.user_id=aoe_queue_stats.user_id ORDER BY linked_at DESC LIMIT 1),
                       (SELECT username FROM discord_member_profiles p WHERE p.user_id=aoe_queue_stats.user_id AND p.is_bot=FALSE ORDER BY updated_at DESC LIMIT 1),
                       (SELECT username FROM vc_logs v WHERE v.user_id=aoe_queue_stats.user_id ORDER BY left_at DESC LIMIT 1),
                       'Player ' || RIGHT(aoe_queue_stats.user_id,4)
                   ) AS aoe4_name,
                   (SELECT profile_id FROM aoe4_links l WHERE l.user_id=aoe_queue_stats.user_id ORDER BY linked_at DESC LIMIT 1) AS profile_id,
                   (SELECT avatar_url FROM discord_member_profiles p WHERE p.user_id=aoe_queue_stats.user_id AND p.is_bot=FALSE ORDER BY updated_at DESC LIMIT 1) AS avatar_url,
                   SUM(wins)::int AS wins,
                   SUM(losses)::int AS losses,
                   SUM(no_results)::int AS no_results,
                   ROUND(AVG(elo))::int AS elo,
                   SUM(wins + losses)::int AS games
            FROM aoe_queue_stats
            GROUP BY user_id
            HAVING SUM(wins + losses) >= $2
            ORDER BY SUM(wins)::int DESC, SUM(wins + losses) DESC
            LIMIT $1
        """, limit, min_games)
    else:
        rows = await fetch("""
            SELECT user_id,
                   COALESCE(
                       (SELECT aoe4_name FROM aoe4_links l WHERE l.user_id=aoe_queue_stats.user_id ORDER BY linked_at DESC LIMIT 1),
                       (SELECT username FROM discord_member_profiles p WHERE p.user_id=aoe_queue_stats.user_id AND p.is_bot=FALSE ORDER BY updated_at DESC LIMIT 1),
                       (SELECT username FROM vc_logs v WHERE v.user_id=aoe_queue_stats.user_id ORDER BY left_at DESC LIMIT 1),
                       'Player ' || RIGHT(aoe_queue_stats.user_id,4)
                   ) AS aoe4_name,
                   (SELECT profile_id FROM aoe4_links l WHERE l.user_id=aoe_queue_stats.user_id ORDER BY linked_at DESC LIMIT 1) AS profile_id,
                   (SELECT avatar_url FROM discord_member_profiles p WHERE p.user_id=aoe_queue_stats.user_id AND p.is_bot=FALSE ORDER BY updated_at DESC LIMIT 1) AS avatar_url,
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
    rows = await fetch("""
        WITH profile AS (
            SELECT DISTINCT ON (user_id) user_id, username, avatar_url
            FROM discord_member_profiles WHERE is_bot=FALSE
            ORDER BY user_id, updated_at DESC
        ), last_vc_name AS (
            SELECT DISTINCT ON (user_id) user_id, username
            FROM vc_logs ORDER BY user_id, left_at DESC
        )
        SELECT v.user_id, SUM(v.total_secs)::float AS total_secs,
               COALESCE(MAX(profile.username), MAX(last_vc_name.username),
                        'Player ' || RIGHT(v.user_id, 4)) AS username,
               MAX(profile.avatar_url) AS avatar_url
        FROM vc_stats v
        LEFT JOIN profile ON profile.user_id=v.user_id
        LEFT JOIN last_vc_name ON last_vc_name.user_id=v.user_id
        GROUP BY v.user_id
        HAVING SUM(v.total_secs) >= $2
        ORDER BY SUM(v.total_secs) DESC LIMIT $1
    """, limit, min_seconds)
    return [dict(r) for r in rows]


@app.get("/api/members")
async def members(limit: int = 250, search: str = ""):
    limit = max(1, min(limit, 500))
    search = (search or "").strip()[:80]
    rows = await fetch("""
        WITH aoe AS (
            SELECT user_id, SUM(wins)::int AS wins, SUM(losses)::int AS losses,
                   SUM(no_results)::int AS no_results, ROUND(AVG(elo))::int AS elo,
                   SUM(wins + losses)::int AS games
            FROM aoe_queue_stats GROUP BY user_id
        ),
        vc AS (
            SELECT user_id, SUM(total_secs)::float AS total_secs
            FROM vc_stats GROUP BY user_id
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
          AND ($1='' OR COALESCE(profile.username,last_vc_name.username,links.aoe4_name,ids.user_id)
                    ILIKE '%' || $1 || '%' OR ids.user_id ILIKE '%' || $1 || '%')
        ORDER BY (COALESCE(aoe.games,0)>0) DESC, COALESCE(aoe.elo,0) DESC,
                 COALESCE(vc.total_secs,0) DESC, username ASC
        LIMIT $2
    """, search, limit)
    return [dict(r) for r in rows]

@app.get("/api/matches/{match_id}")
async def match_detail(match_id: int):
    """Return a complete, JSON-safe match detail record for the website modal."""
    import json
    rows = await fetch("""
        SELECT id, guild_id, queue_type, player_ids, team1_ids, team2_ids,
               result, civ_data, to_jsonb(aoe_matches)->'civ_bans' AS civ_bans, created_at, finished_at
        FROM aoe_matches WHERE id=$1 LIMIT 1
    """, match_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Match not found")
    match = dict(rows[0])
    def json_object(value):
        if isinstance(value, str):
            try: return json.loads(value)
            except (TypeError, ValueError): return {}
        return value if isinstance(value, dict) else {}
    match["civ_data"] = json_object(match.get("civ_data"))
    match["civ_bans"] = json_object(match.get("civ_bans"))
    team1 = [str(x) for x in (match.get("team1_ids") or [])]
    team2 = [str(x) for x in (match.get("team2_ids") or [])]
    player_ids = [str(x) for x in (match.get("player_ids") or [])]
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
        for row in profile_rows:
            players[str(row["user_id"])] = {"user_id": str(row["user_id"]), "username": row["username"], "avatar_url": row["avatar_url"]}
        for row in vc_rows:
            item = players.setdefault(str(row["user_id"]), {"user_id": str(row["user_id"]), "avatar_url": None})
            if not item.get("username"): item["username"] = row["username"]
        for row in elo_rows:
            item = players.setdefault(str(row["user_id"]), {"user_id": str(row["user_id"]), "avatar_url": None})
            item["elo"] = row["elo"]
        civs = match["civ_data"]
        for uid in all_ids:
            item = players.setdefault(uid, {"user_id": uid, "avatar_url": None})
            item.setdefault("username", f"Player {uid[-4:]}")
            civ_value = civs.get(uid)
            if isinstance(civ_value, dict):
                civ_value = civ_value.get("name") or civ_value.get("civilization") or civ_value.get("civ")
            item["civ"] = str(civ_value) if civ_value else "Not recorded"
    match["team1"] = [players[x] for x in team1 if x in players]
    match["team2"] = [players[x] for x in team2 if x in players]
    match["players"] = players
    return match


@app.get("/api/matches")
async def matches(limit: int = 100, search: str = ""):
    """List matches, optionally searching by match ID, Discord name or AoE4 name."""
    limit = max(1, min(limit, 250))
    search = (search or "").strip()[:100]
    rows = await fetch("""
        SELECT m.id, m.queue_type, m.player_ids, m.team1_ids, m.team2_ids,
               m.result, m.civ_data, m.created_at, m.finished_at
        FROM aoe_matches AS m
        WHERE ($2 = ''
            OR m.id::text ILIKE '%' || $2 || '%'
            OR EXISTS (
                SELECT 1 FROM discord_member_profiles AS p
                WHERE p.user_id = ANY(m.player_ids)
                  AND p.username ILIKE '%' || $2 || '%'
            )
            OR EXISTS (
                SELECT 1 FROM vc_logs AS v
                WHERE v.user_id = ANY(m.player_ids)
                  AND v.username ILIKE '%' || $2 || '%'
            )
            OR EXISTS (
                SELECT 1 FROM aoe4_links AS l
                WHERE l.user_id = ANY(m.player_ids)
                  AND l.aoe4_name ILIKE '%' || $2 || '%'
            )
        )
        ORDER BY m.created_at DESC
        LIMIT $1
    """, limit, search)
    return [dict(r) for r in rows]

@app.get("/api/players/{user_id}")
async def player_profile(user_id: str):
    stats = await fetch("""
        SELECT queue_type, SUM(wins)::int AS wins, SUM(losses)::int AS losses,
               SUM(no_results)::int AS no_results, ROUND(AVG(elo))::int AS elo
        FROM aoe_queue_stats WHERE user_id=$1 GROUP BY queue_type ORDER BY queue_type
    """, user_id)
    vc_seconds = await fetchval(
        "SELECT COALESCE(SUM(total_secs),0) FROM vc_stats WHERE user_id=$1",
        user_id, default=0
    )
    links = await fetch("""
        SELECT aoe4_name, profile_id, bypassed, linked_at FROM aoe4_links
        WHERE user_id=$1 ORDER BY linked_at DESC LIMIT 1
    """, user_id)
    profile = await fetch("""
        SELECT username, avatar_url FROM discord_member_profiles
        WHERE user_id=$1 AND is_bot=FALSE ORDER BY updated_at DESC LIMIT 1
    """, user_id)
    recent = await fetch("""
        SELECT id, queue_type, player_ids, team1_ids, team2_ids, result,
               civ_data, created_at, finished_at FROM aoe_matches
        WHERE $1 = ANY(player_ids) ORDER BY created_at DESC LIMIT 10
    """, user_id)
    official_ratings = await aoe4world_ratings(links[0]["profile_id"] if links else None)
    return {
        "user_id": user_id,
        **official_ratings,
        "username": profile[0]["username"] if profile else (links[0]["aoe4_name"] if links else f"Player {user_id[-4:]}"),
        "avatar_url": profile[0]["avatar_url"] if profile else None,
        "aoe4_link": dict(links[0]) if links else None,
        "queue_stats": [dict(r) for r in stats],
        "vc_seconds": float(vc_seconds or 0),
        "recent_matches": [dict(r) for r in recent],
    }

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def home():
    return FileResponse("static/index.html")

@app.get("/community")
async def community_page():
    return FileResponse("static/community.html")


@app.get("/leaderboard")
async def leaderboard_page():
    return FileResponse("static/leaderboard.html")

@app.get("/matches")
async def matches_page():
    return FileResponse("static/matches.html")

@app.get("/voice-activity")
async def voice_activity_page():
    return FileResponse("static/voice-activity.html")

@app.get("/tournament")
async def tournament_page():
    return FileResponse("static/tournament.html")

@app.get("/contact")
async def contact_page():
    return FileResponse("static/contact.html")

@app.get("/login")
async def login_page():
    return FileResponse("static/login.html")

@app.get("/signup")
async def signup_page():
    return FileResponse("static/signup.html")
