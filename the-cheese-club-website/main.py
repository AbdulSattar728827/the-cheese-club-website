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
        await refresh_hidden()
        refresher = asyncio.create_task(_hidden_refresh_loop())
    yield
    if pool:
        refresher.cancel()
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
            })
        _match_cache.update(at=time.monotonic(), rows=matches)
        return matches


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
    matches = await load_matches()
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
    matches = [m for m in await load_matches() if decided(m) and (queue == "all" or m["queue"] == queue)]
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
    matches = await load_matches()
    now = datetime.now(timezone.utc)
    live_window, day, week = now - timedelta(hours=6), now - timedelta(days=1), now - timedelta(days=7)

    def aware(dt):
        return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    live = [m for m in matches if m["result"] == "pending" and aware(m["created_at"]) and aware(m["created_at"]) >= live_window]
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


def _safe_next(value: Optional[str], default: str = "/account") -> str:
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
async def auth_login(request: Request, next: str = "/account"):
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
    response = RedirectResponse(saved.get("n") or "/account", status_code=302)
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
    "/contact": "contact.html",
    "/login": "login.html",
    "/account": "account.html",
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
            ins = compute_insights(user_id, await load_matches())
            w, l = (totals[0]["wins"], totals[0]["losses"]) if totals else (0, 0)
            title = f"{name} — The Cheese Club"
            description = f"{w} wins and {l} losses in Cheese Club community matches."
            if ins["favourite_civ"]:
                description += f" Favourite civilization: {ins['favourite_civ']}."
        except Exception:
            log.exception("Could not build link preview for player page")
    return render_page("player.html", request, OG_TITLE=title, OG_DESCRIPTION=description,
                       OG_PATH=f"/player/{player_id}")
