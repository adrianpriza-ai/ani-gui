import os
import json
from urllib.parse import quote, unquote
import sqlite3
import subprocess
import threading
import time

import uvicorn
import webview
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse, Response
import httpx


# ── Proxy headers (same as ani-cli / scraper) ─────────────────────────────────
_PROXY_H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
    "Referer":    "https://allanime.to",
}
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
    import asyncio, concurrent.futures
    raw = search_anime(q, type)

    loop = asyncio.get_event_loop()
    def _enrich(r):
        al = get_anilist_info(r.get("englishName") or r.get("name", ""))
        return {
            "id":        r.get("_id"),
            "title":     r.get("englishName") or r.get("name"),
            "raw_title": r.get("name"),
            "eps_avail": r.get("availableEpisodes", {}),
            "thumbnail": r.get("thumbnail"),
            "score":     r.get("score"),
            "anilist":   al,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_enrich, raw[:12]))
    return JSONResponse(results)

@app.get("/api/trending")
async def api_trending(type: str = "sub"):
    """Fetch trending anime from AniList for the Home view."""
    import asyncio, concurrent.futures
    gql = """
    query {
        Page(perPage: 12) {
            media(type: ANIME, sort: TRENDING_DESC, status_not: NOT_YET_RELEASED) {
                title { romaji english }
                coverImage { large extraLarge }
                averageScore episodes status seasonYear
                description(asHtml: false)
                genres
            }
        }
    }"""
    try:
        r = requests.post("https://graphql.anilist.co",
                          json={"query": gql}, timeout=12)
        media_list = r.json().get("data", {}).get("Page", {}).get("media", [])
    except Exception:
        return JSONResponse([])

    results = []
    for m in media_list:
        results.append({
            "id":        None,
            "title":     m.get("title", {}).get("english") or m.get("title", {}).get("romaji", ""),
            "thumbnail": m.get("coverImage", {}).get("large", ""),
            "score":     m.get("averageScore"),
            "anilist":   {
                "title":       m.get("title", {}),
                "coverImage":  m.get("coverImage", {}),
                "averageScore": m.get("averageScore"),
                "episodes":    m.get("episodes"),
                "status":      m.get("status"),
                "seasonYear":  m.get("seasonYear"),
                "description": m.get("description", ""),
                "genres":      m.get("genres", []),
            },
        })
    return JSONResponse(results)

# ── Episodes ──────────────────────────────────────────────────────────────────

@app.get("/api/episodes")
async def api_episodes(id: str, type: str = "sub"):
    return JSONResponse({"episodes": get_episodes(id, type)})

# ── Stream ────────────────────────────────────────────────────────────────────

@app.get("/api/stream")
async def api_stream(request: Request, id: str, episode: str, type: str = "sub", title: str = ""):
    stream = get_best_stream(id, episode, type, anime_title=title)
    if not stream or "error" in stream:
        return JSONResponse({"error": "No stream found"}, status_code=404)
    # Wrap raw CDN URL in our proxy so the browser doesn't need to set Referer
    raw_url = stream["url"]
    proxy_url = str(request.base_url) + "api/proxy?url=" + quote(raw_url, safe="")
    return JSONResponse({"url": proxy_url, "type": stream["type"], "raw": raw_url})

@app.get("/api/proxy")
async def api_proxy(request: Request, url: str):
    """
    Async proxy menggunakan httpx — setiap Range request (seek) dapat
    koneksi sendiri, tidak pernah block event loop atau stuck di belakang
    stream sebelumnya.
    """
    real_url = unquote(url)
    h        = {**_PROXY_H}
    rang     = request.headers.get("range")
    if rang:
        h["Range"] = rang

    # ── HLS manifest: rewrite segment URLs ────────────────────────────────────
    if ".m3u8" in real_url:
        try:
            async with httpx.AsyncClient(follow_redirects=True) as c:
                r    = await c.get(real_url, headers=h, timeout=15)
            base = real_url.rsplit("/", 1)[0] + "/"
            lines = []
            for line in r.text.splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    seg  = s if s.startswith("http") else base + s
                    line = "/api/proxy?url=" + quote(seg, safe="")
                lines.append(line)
            return Response("\n".join(lines),
                            media_type="application/vnd.apple.mpegurl",
                            headers={"Access-Control-Allow-Origin": "*"})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    # ── Direct / MP4 stream: each seek = fresh async connection ───────────────
    async def _body():
        async with httpx.AsyncClient(follow_redirects=True) as c:
            async with c.stream("GET", real_url, headers=h, timeout=30) as r:
                async for chunk in r.aiter_bytes(65536):
                    yield chunk

    try:
        async with httpx.AsyncClient(follow_redirects=True) as c:
            head = await c.head(real_url, headers={**_PROXY_H}, timeout=10)
        ct  = head.headers.get("content-type", "video/mp4")
        out = {"Access-Control-Allow-Origin": "*", "Accept-Ranges": "bytes"}
        for k in ("content-length", "content-range"):
            if head.headers.get(k):
                out[k.title()] = head.headers[k]
        status = 206 if rang else 200
        return StreamingResponse(_body(), status_code=status,
                                 media_type=ct, headers=out)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

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
    data    = await req.json()
    url     = data.get("url", "")
    referer = data.get("referer", "https://allanime.to")
    ua      = data.get("user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0")
    if not url:
        return JSONResponse({"error": "No URL"}, status_code=400)
    try:
        # MPV requires --flag=value, not --flag value
        subprocess.Popen([
            "mpv",
            f"--referrer={referer}",
            f"--user-agent={ua}",
            "--force-window=yes",
            url,
        ], start_new_session=True)
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
