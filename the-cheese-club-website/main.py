import os
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

DATABASE_URL = os.getenv("DATABASE_URL")
pool: Optional[asyncpg.Pool] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    if DATABASE_URL:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=15)
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
async def aoe_leaderboard(queue: str = "all", limit: int = 50):
    limit = max(1, min(limit, 100))
    if queue == "all":
        rows = await fetch("""
            SELECT user_id,
                   SUM(wins)::int AS wins,
                   SUM(losses)::int AS losses,
                   SUM(no_results)::int AS no_results,
                   ROUND(AVG(elo))::int AS elo,
                   SUM(wins + losses)::int AS games
            FROM aoe_queue_stats
            GROUP BY user_id
            ORDER BY SUM(wins)::int DESC, ROUND(AVG(elo))::int DESC
            LIMIT $1
        """, limit)
    else:
        rows = await fetch("""
            SELECT user_id, wins, losses, no_results, elo, (wins + losses)::int AS games
            FROM aoe_queue_stats
            WHERE queue_type = $1
            ORDER BY elo DESC, wins DESC
            LIMIT $2
        """, queue, limit)
    return [dict(r) for r in rows]

@app.get("/api/leaderboards/vc")
async def vc_leaderboard(limit: int = 50):
    limit = max(1, min(limit, 100))
    rows = await fetch("""
        SELECT user_id, total_secs
        FROM vc_stats
        ORDER BY total_secs DESC
        LIMIT $1
    """, limit)
    return [dict(r) for r in rows]

@app.get("/api/matches")
async def matches(limit: int = 20):
    limit = max(1, min(limit, 100))
    rows = await fetch("""
        SELECT id, queue_type, player_ids, team1_ids, team2_ids,
               result, civ_data, created_at, finished_at
        FROM aoe_matches
        ORDER BY created_at DESC
        LIMIT $1
    """, limit)
    return [dict(r) for r in rows]

@app.get("/api/players/{user_id}")
async def player_profile(user_id: str):
    stats = await fetch("""
        SELECT queue_type, wins, losses, no_results, elo
        FROM aoe_queue_stats
        WHERE user_id = $1
        ORDER BY queue_type
    """, user_id)
    vc = await fetch("""
        SELECT total_secs FROM vc_stats WHERE user_id = $1
        ORDER BY total_secs DESC LIMIT 1
    """, user_id)
    links = await fetch("""
        SELECT aoe4_name, profile_id, bypassed, linked_at
        FROM aoe4_links WHERE user_id = $1 LIMIT 1
    """, user_id)
    recent = await fetch("""
        SELECT id, queue_type, player_ids, team1_ids, team2_ids, result,
               civ_data, created_at, finished_at
        FROM aoe_matches
        WHERE $1 = ANY(player_ids)
        ORDER BY created_at DESC LIMIT 10
    """, user_id)
    return {
        "user_id": user_id,
        "aoe4_link": dict(links[0]) if links else None,
        "queue_stats": [dict(r) for r in stats],
        "vc_seconds": float(vc[0]["total_secs"]) if vc else 0,
        "recent_matches": [dict(r) for r in recent],
    }

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def home():
    return FileResponse("static/index.html")
