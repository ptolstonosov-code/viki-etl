"""
ARM-side HTTP API: the always-on Python service that receives files,
runs known parsers, and (on unknown formats) wakes the LLM service.

Endpoints:
  POST /ingest        — file upload (multipart/form-data) → records JSON
  POST /ingest/raw    — raw bytes body → records JSON
  GET  /status        — health, parsers list, queue stats
  GET  /formats       — list of all fingerprint buckets (known + learned + quarantine)
  POST /trigger-tick  — manually invoke autoparser maintenance round

Runs as a FastAPI app behind uvicorn on :8080.
Memory footprint: ~150 MB. Always up.
LLM service: started on-demand via systemctl from autoparser.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse

# Allow importing core.* from /opt/llm-etl
ROOT = Path(os.environ.get("LLM_ETL_ROOT", "/opt/llm-etl"))
sys.path.insert(0, str(ROOT))

from core.autoparser.orchestrator import AutoparserOrchestrator  # noqa: E402
from core.autoparser.llm_service import LLMService  # noqa: E402
from core.parsers import discover_learned, list_parsers  # noqa: E402
from core.db_writer_lite import SQLiteWriter, init_db  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("api_service")

# ── Configuration ────────────────────────────────────────────────────────────

DATA_DIR = ROOT / "data"
PARSERS_DIR = ROOT / "parsers"
DB_PATH = DATA_DIR / "etl.db"
SCHEMA_SQL = ROOT / "config" / "schema.sql"
DATA_DIR.mkdir(parents=True, exist_ok=True)
PARSERS_DIR.mkdir(parents=True, exist_ok=True)

# Initialise database (create schema if first run)
init_result = init_db(DB_PATH, SCHEMA_SQL)
logger.info("DB at %s — %s", DB_PATH, init_result)

# DB writer (used to actually persist parser/LLM output)
db = SQLiteWriter(DB_PATH)

# Load learned parsers from disk
discover_learned(PARSERS_DIR)

# LLM service (lazy)
llm = LLMService(
    binary=ROOT / "llama-server",
    model=ROOT / "models" / "active.gguf",
    port=8080 + 100,        # 8180 — LLM service is on a different port
    threads=int(os.environ.get("LLM_THREADS", "4")),
    idle_timeout=int(os.environ.get("LLM_IDLE_TIMEOUT", "60")),
    use_systemd=True,
    systemd_unit="llm-etl-llm.service",
)

# Load system prompt (full schema)
SYSTEM_PROMPT_PATH = ROOT / "system_prompt.txt"
if SYSTEM_PROMPT_PATH.exists():
    SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
else:
    SYSTEM_PROMPT = "You are a parser. Convert the following data to JSON records."

orchestrator = AutoparserOrchestrator(
    data_dir=DATA_DIR,
    parsers_dir=PARSERS_DIR,
    llm=llm,
    system_prompt=SYSTEM_PROMPT,
)


# ── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(title="LLM ETL — ARM API", version="1.0")


def _persist(result: dict) -> dict:
    """Write records into SQLite, attach write stats to the result."""
    recs = result.get("records") or []
    if not recs:
        result["db"] = {"inserted": 0, "skipped": 0, "errors": []}
        return result
    write_stats = db.write(recs)
    result["db"] = write_stats
    return result


@app.post("/ingest")
async def ingest(file: UploadFile = File(...)):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    try:
        result = orchestrator.ingest_file(raw, filename=file.filename)
        return _persist(result)
    except Exception as e:
        logger.exception("ingest failed")
        raise HTTPException(500, str(e))


@app.post("/ingest/raw")
async def ingest_raw(request: Request):
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "empty body")
    filename = request.headers.get("X-Filename")
    try:
        result = orchestrator.ingest_file(raw, filename=filename)
        return _persist(result)
    except Exception as e:
        logger.exception("ingest_raw failed")
        raise HTTPException(500, str(e))


@app.get("/status")
async def status():
    parsers = list_parsers()
    return {
        "service": "llm-etl-api",
        "parsers": [
            {"name": p.name, "priority": p.priority, "enabled": p.enabled}
            for p in parsers
        ],
        "llm_running": llm.is_running(),
        "data_dir": str(DATA_DIR),
        "db_path": str(DB_PATH),
        "samples_total": len(list((DATA_DIR / "samples").glob("*/meta.json"))),
        "row_counts": db.row_counts(),
    }


@app.get("/formats")
async def formats():
    buckets = orchestrator.collector.list_buckets()
    return {"buckets": buckets}


@app.post("/trigger-tick")
async def trigger_tick():
    """Manually trigger autoparser maintenance (normally runs on timer)."""
    actions = orchestrator.tick()
    return {"actions": actions}


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Background tick loop ─────────────────────────────────────────────────────

import asyncio


async def _tick_loop():
    """Run orchestrator.tick() every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        try:
            actions = orchestrator.tick()
            if any(actions.values()):
                logger.info("tick: %s", actions)
        except Exception as e:
            logger.exception("tick failed: %s", e)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_tick_loop())
    logger.info("API ready on :8080  |  loaded %d parsers", len(list_parsers()))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
