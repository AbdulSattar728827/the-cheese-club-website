# The Cheese Club Website (MVP)

A public, read-only community website connected to the existing Railway PostgreSQL database.

## Included
- Public homepage with The Cheese Club branding
- AoE IV player leaderboard (overall and queue-specific)
- Voice-channel leaderboard
- Recent and active match listing
- Player profile API
- API health endpoint
- Automatic browser refresh every 30 seconds

## Deploy as a NEW Railway service
1. Push this folder to a NEW GitHub repository.
2. In Railway, open the existing project and choose **Add Service → GitHub Repo**.
3. Select the new website repository.
4. Open the new website service's **Variables**.
5. Add `DATABASE_URL` as a Railway Variable Reference to the existing Postgres service's `DATABASE_URL`.
   - Do not copy/paste or expose the actual database password in chat.
   - Do not change the existing bot or Postgres variables.
6. Deploy. Railway will use the Procfile / railway.json start command.
7. Generate a public domain in the new website service's Settings → Networking.

## Important safety notes
- This MVP only reads existing tables; it does not create, alter, or delete database tables.
- Do not deploy this website by replacing the Discord bot service.
- The public API intentionally has no write endpoints.
- Queue waiting lists are currently held in bot memory in the uploaded bot code, so they are not yet available through this website. Queue persistence and safe website join/leave require a separate bot integration phase.
- Player names/avatars are currently represented by Discord user IDs where the database stores IDs only. A member directory sync can be added in the next phase.
- Tournament code currently sets up/tears down Discord channels; a full tournament registration/bracket system is a future feature.

## Local run
```bash
python -m venv .venv
# activate the environment
pip install -r requirements.txt
export DATABASE_URL='your-postgres-connection-string'
uvicorn main:app --reload
```

## API routes
- `GET /api/health`
- `GET /api/overview`
- `GET /api/leaderboards/aoe?queue=all`
- `GET /api/leaderboards/aoe?queue=1v1`
- `GET /api/leaderboards/vc`
- `GET /api/matches`
- `GET /api/players/{discord_user_id}`
