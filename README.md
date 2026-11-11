# SoupTask

# Soup job queue

Python, FastAPI, Postgres, 8 workers.

## Run
docker compose up --build

API: http://localhost:8000 (docs at /docs)

## Quick check (PowerShell)
Invoke-WebRequest -Method Post -Uri http://localhost:8000/jobs -Headers @{"Idempotency-Key"="test-1"} -ContentType "application/json" -Body '{"payload": {}}'
docker compose exec db psql -U soup -c "select count(*) from effects"

## Files
- DESIGN.md: design note
- RUNS/: raw output of every test run, including failed ones
- AI_LOG.md: how I used AI