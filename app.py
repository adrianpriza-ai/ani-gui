import os
import json
import sqlite3
import subprocess
import threading
import time

import uvicorn
import webview
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse

from scraper import search_anime, get_episodes, get_best_stream, get_anilist_info

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.expanduser("~/.ani-gui/history.db")
UI_PATH  = os.path.join(BASE_DIR, "ui.html")

# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            anime_id        TEXT NOT NULL,
            anime_title     TEXT NOT NULL,
            episode         TEXT NOT NULL,
            progress        REAL DEFAULT 0,
            duration        REAL DEFAULT 0,
            thumbnail       TEXT DEFAULT '',
            last_watched    DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(anime_id, episode)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI()
init_db()

# ── UI ────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    with open(UI_PATH, "r") as f:
        return f.read()

# ── Search ────────────────────────────────────────────────────────────────────

@app.get("/api/search")
async def api_search(q: str, type: str = "sub"):
    raw = search_anime(q, type)
    results = []
    for r in raw[:12]:
        anilist = get_anilist_info(r.get("englishName") or r.get("name", ""))
        results.append({
            "id":        r.get("_id"),
            "title":     r.get("englishName") or r.get("name"),
            "raw_title": r.get("name"),
            "eps_avail": r.get("availableEpisodes", {}),
            "thumbnail": r.get("thumbnail"),
            "score":     r.get("score"),
            "anilist":   anilist,
        })
    return JSONResponse(results)

# ── Episodes ──────────────────────────────────────────────────────────────────

@app.get("/api/episodes")
async def api_episodes(id: str, type: str = "sub"):
    return JSONResponse({"episodes": get_episodes(id, type)})

# ── Stream ────────────────────────────────────────────────────────────────────

@app.get("/api/stream")
async def api_stream(id: str, episode: str, type: str = "sub"):
    stream = get_best_stream(id, episode, type)
    if stream:
        return JSONResponse(stream)
    return JSONResponse({"error": "No stream found"}, status_code=404)

# ── History ───────────────────────────────────────────────────────────────────

@app.get("/api/history")
async def api_history_get():
    conn = db()
    rows = conn.execute(
        "SELECT * FROM history ORDER BY last_watched DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return JSONResponse([dict(r) for r in rows])

@app.post("/api/history")
async def api_history_upsert(req: Request):
    data = await req.json()
    conn = db()
    conn.execute("""
        INSERT INTO history (anime_id, anime_title, episode, progress, duration, thumbnail)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(anime_id, episode) DO UPDATE SET
            progress     = excluded.progress,
            duration     = excluded.duration,
            last_watched = CURRENT_TIMESTAMP
    """, (
        data["anime_id"], data["anime_title"], data["episode"],
        data.get("progress", 0), data.get("duration", 0),
        data.get("thumbnail", ""),
    ))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})

@app.delete("/api/history/{item_id}")
async def api_history_delete(item_id: int):
    conn = db()
    conn.execute("DELETE FROM history WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})

# ── Settings ──────────────────────────────────────────────────────────────────

DEFAULTS = {
    "hw_accel":      False,
    "sub_lang":      "sub",
    "player":        "web",   # "web" | "mpv"
}

@app.get("/api/settings")
async def api_settings_get():
    conn = db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    saved = {r["key"]: json.loads(r["value"]) for r in rows}
    return JSONResponse({**DEFAULTS, **saved})

@app.post("/api/settings")
async def api_settings_save(req: Request):
    data = await req.json()
    conn = db()
    for k, v in data.items():
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)",
            (k, json.dumps(v))
        )
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})

# ── MPV launcher ──────────────────────────────────────────────────────────────

@app.post("/api/mpv")
async def api_mpv(req: Request):
    data = await req.json()
    url = data.get("url", "")
    if not url:
        return JSONResponse({"error": "No URL"}, status_code=400)
    try:
        subprocess.Popen(["mpv", url], start_new_session=True)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ── Entry point ───────────────────────────────────────────────────────────────

def _run_server():
    uvicorn.run(app, host="127.0.0.1", port=6969, log_level="error")

if __name__ == "__main__":
    threading.Thread(target=_run_server, daemon=True).start()
    time.sleep(1)   # let FastAPI start up

    window = webview.create_window(
        title="ani-gui",
        url="http://127.0.0.1:6969",
        width=1280,
        height=800,
        min_size=(900, 600),
    )
    webview.start(debug=False)
