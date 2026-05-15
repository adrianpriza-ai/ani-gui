"""
ani-gui scraper — faithful Python port of ani-cli's scraping logic

ani-cli flow (mirrored exactly):
  search_anime()    → POST GraphQL → list of shows
  get_episodes()    → POST GraphQL → sorted episode list  
  get_episode_url() → APQ/POST → decode sources → 5 providers in parallel → best quality
"""
import base64
import collections
import concurrent.futures
import hashlib
import re
import time

# ── In-memory log buffer (read by /api/debug/log → shown in dev tab) ──────────
_log_buffer: collections.deque = collections.deque(maxlen=200)

def _log(level: str, msg: str) -> None:
    entry = f"[{time.strftime('%H:%M:%S')}] {level}: {msg}"
    _log_buffer.append(entry)

def get_log_lines() -> list[str]:
    return list(_log_buffer)

def clear_logs() -> None:
    _log_buffer.clear()

# ── HTTP session: cloudscraper (Cloudflare bypass) → fallback to requests ─────
# AllAnime is behind Cloudflare anti-bot; plain requests gets a TLS fingerprint
# mismatch → 403. cloudscraper impersonates Chrome including JS challenge solving.
# Install: pip install cloudscraper
try:
    import cloudscraper as _cs_mod
    _session = _cs_mod.create_scraper(browser={"browser": "chrome", "platform": "linux", "mobile": False})
    _log("INFO", "Using cloudscraper session (Cloudflare bypass active)")
except ImportError:
    import requests as _req_mod
    _session = _req_mod.Session()
    _log("WARN", "cloudscraper not installed — falling back to requests (may get 403 from Cloudflare). Run: pip install cloudscraper")

import requests  # still used for AniList (not CF-protected)

try:
    from Crypto.Cipher import AES
    from Crypto.Util import Counter as _PyCtr
    _HAS_AES = True
except ImportError:
    _HAS_AES = False
    _log("WARN", "pycryptodome not installed — tobeparsed decryption disabled. Run: pip install pycryptodome")

# ── Setup — same variables as ani-cli ─────────────────────────────────────────

AGENT         = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0"
ALLANIME_REFR = "https://allmanga.to"
ALLANIME_BASE = "allanime.day"
ALLANIME_API  = f"https://api.{ALLANIME_BASE}"
ALLANIME_KEY  = hashlib.sha256(b"Xot36i3lK3:v1").hexdigest()
ANILIST_API   = "https://graphql.anilist.co"

_H = {"User-Agent": AGENT, "Referer": ALLANIME_REFR}

# ── Hex decode table — from ani-cli's provider_init sed table ─────────────────

_HEX = {
    '79':'A','7a':'B','7b':'C','7c':'D','7d':'E','7e':'F','7f':'G',
    '70':'H','71':'I','72':'J','73':'K','74':'L','75':'M','76':'N','77':'O',
    '68':'P','69':'Q','6a':'R','6b':'S','6c':'T','6d':'U','6e':'V','6f':'W',
    '60':'X','61':'Y','62':'Z',
    '59':'a','5a':'b','5b':'c','5c':'d','5d':'e','5e':'f','5f':'g',
    '50':'h','51':'i','52':'j','53':'k','54':'l','55':'m','56':'n','57':'o',
    '48':'p','49':'q','4a':'r','4b':'s','4c':'t','4d':'u','4e':'v','4f':'w',
    '40':'x','41':'y','42':'z',
    '08':'0','09':'1','0a':'2','0b':'3','0c':'4','0d':'5','0e':'6','0f':'7',
    '00':'8','01':'9',
    '15':'-','16':'.','67':'_','46':'~','02':':','17':'/','07':'?',
    '1b':'#','63':'[','65':']','78':'@','19':'!','1c':'$','1e':'&',
    '10':'(','11':')','12':'*','13':'+','14':',','03':';','05':'=','1d':'%',
}

def _decode_provider_path(hex_str: str) -> str:
    """Decode hex-encoded provider path using ani-cli's cipher table."""
    h   = hex_str.lower()
    out = ''.join(_HEX.get(h[i:i+2], '') for i in range(0, len(h)-1, 2))
    return out.replace('/clock', '/clock.json')

# ── b64url → hex — mirrors ani-cli's b64url_to_hex() ─────────────────────────

def _b64url_to_hex(s: str) -> str:
    pad = {2: '==', 3: '='}.get(len(s) % 4, '')
    return base64.b64decode((s + pad).replace('-', '+').replace('_', '/')).hex()

# ── AES helper — used by both decode_tobeparsed and get_filemoon_links ─────────

def _aes_ctr_decrypt(key_hex: str, iv_hex_with_ctr: str, data: bytes) -> str:
    ctr    = _PyCtr.new(128, initial_value=int(iv_hex_with_ctr, 16))
    cipher = AES.new(bytes.fromhex(key_hex), AES.MODE_CTR, counter=ctr)
    return cipher.decrypt(data).decode('utf-8', errors='ignore')

# ── decode_tobeparsed — mirrors ani-cli's decode_tobeparsed() ─────────────────

def _decode_tobeparsed(blob: str) -> dict[str, str]:
    """
    AES-256-CTR decrypt AllAnime's 'tobeparsed' blob.
    Returns {provider_name: hex_encoded_path}, same as ani-cli's $resp dict.
    """
    if not _HAS_AES:
        return {}
    try:
        raw    = base64.b64decode(blob + '==')
        iv_hex = raw[1:13].hex()                 # skip=1 count=12
        ctr    = iv_hex + '00000002'
        ct_len = len(raw) - 13 - 16             # skip 13, strip 16 auth tag
        plain  = _aes_ctr_decrypt(ALLANIME_KEY, ctr, raw[13:13+ct_len])

        result = {}
        # tr '{}' '\n' then sed -nE 's|..."sourceUrl":"--hex"..."sourceName":"name"...|name:hex|p'
        for chunk in re.sub(r'[{}]', '\n', plain).splitlines():
            name = re.search(r'"sourceName"\s*:\s*"([^"]+)"', chunk)
            url  = re.search(r'"sourceUrl"\s*:\s*"--([0-9a-fA-F]+)"', chunk)
            if name and url:
                result[name.group(1)] = url.group(1)
        return result
    except Exception:
        return {}

# ── get_links — mirrors ani-cli's get_links() ─────────────────────────────────

def _get_links(provider_path: str) -> list[str]:
    """
    mirrors ani-cli's get_links():
    - Full URL (https://fast4speed CDN etc.) → direct video, return immediately
    - Relative path (/apivtwo/...)          → fetch from allanime.day, parse links
    Returns list of "quality >url" strings sorted best-first.
    """
    # Full URL → direct stream link (fast4speed CDN, etc.)
    # ani-cli handles this as: case *fast4speed*) video_link="$1" ;;
    if provider_path.startswith('http'):
        try:
            r     = _session.head(provider_path, headers=_H, timeout=10, allow_redirects=True)
            final = r.url
            ct    = r.headers.get('content-type', '')
            if any(t in ct for t in ('video/', 'octet-stream')):
                return [f"1080 >{final}"]
            if any(ext in final for ext in ('.mp4', '.m3u8', '.mkv')):
                return [f"1080 >{final}"]
        except Exception:
            pass
        return [f"1080 >{provider_path}"]

    # Relative path → fetch from allanime.day
    url = f"https://{ALLANIME_BASE}{provider_path}"
    try:
        resp = _session.get(url, headers=_H, timeout=12).text

        # sed -nE 's|.*link":"([^"]*)".*"resolutionStr":"([^"]*)".*|\2 >\1|p'
        raw_links = []
        for seg in resp.replace('},{', '}\n{').split('\n'):
            lm = re.search(r'"link"\s*:\s*"([^"]+)"', seg)
            rm = re.search(r'"resolutionStr"\s*:\s*"([^"]+)"', seg)
            hm = re.search(r'"hls","url":"([^"]+)"[^}]*"hardsub_lang":"en-US"', seg)
            if lm and rm:
                raw_links.append(f"{rm.group(1)} >{lm.group(1).replace(r'\/', '/')}")
            elif hm:
                raw_links.append(hm.group(1).replace(r'\/', '/'))

        if not raw_links:
            return []

        full = '\n'.join(raw_links)

        # Wixmp repackager — multi-quality mp4
        # sed 's|repackager.wixmp.com/||g;s|\.urlset.*||g'
        if 'repackager.wixmp.com' in full:
            base_url = (raw_links[0].split('>')[-1]
                        .replace('repackager.wixmp.com/', ''))
            base_url = re.sub(r'\.urlset.*', '', base_url)
            quals_m  = re.search(r'/,([^/]+),/mp4', base_url)
            if quals_m:
                return sorted(
                    [f"{q} >{re.sub(r',[^/]+', q, base_url)}"
                     for q in quals_m.group(1).split(',') if q],
                    reverse=True
                )

        # master.m3u8 — fetch manifest and parse stream resolutions
        if 'master.m3u8' in full:
            master = (raw_links[0].split('>')[-1]).strip()
            rel    = re.sub(r'[^/]*$', '', master)    # strip filename
            try:
                m3u8 = _session.get(master, headers=_H, timeout=10).text
                if '#EXTM3U' in m3u8:
                    result, lines = [], m3u8.splitlines()
                    for i, ln in enumerate(lines):
                        if '#EXT-X-STREAM-INF' in ln and 'I-FRAME' not in ln:
                            rm2 = re.search(r'x(\d+)', ln)
                            if rm2 and i+1 < len(lines):
                                seg = lines[i+1].strip()
                                if not seg.startswith('http'):
                                    seg = rel + seg
                                result.append(f"{rm2.group(1)} >{seg}")
                    if result:
                        return sorted(result,
                            key=lambda x: int(x.split(' ')[0]) if x.split(' ')[0].isdigit() else 0,
                            reverse=True)
            except Exception:
                pass

        return raw_links
    except Exception:
        return []

# ── get_filemoon_links — mirrors ani-cli's get_filemoon_links() ───────────────

def _get_filemoon_links(path: str) -> list[str]:
    """
    Filemoon provider: fetch page → extract iv/payload/key_parts → AES-256-CTR decrypt.
    """
    if not _HAS_AES:
        return []
    url = f"https://{ALLANIME_BASE}{path}"
    try:
        resp = _session.get(url, headers=_H, timeout=12).text
        flat = re.sub(r'\s+', '', resp)   # tr -d '\n '

        iv      = (re.search(r'"iv"\s*:\s*"([^"]+)"', flat) or [None,None])[1] or ''
        payload = (re.search(r'"payload"\s*:\s*"([^"]+)"', flat) or [None,None])[1] or ''
        kp      = re.search(r'"key_parts"\s*:\s*\["([^"]+)"\s*,\s*"([^"]+)"\]', flat)
        if not (iv and payload and kp):
            return []

        key_hex = _b64url_to_hex(kp.group(1)) + _b64url_to_hex(kp.group(2))
        iv_hex  = _b64url_to_hex(iv) + '00000002'
        pad     = {2:'==', 3:'='}.get(len(payload) % 4, '')
        raw     = base64.b64decode((payload+pad).replace('-','+').replace('_','/'))
        plain   = _aes_ctr_decrypt(key_hex, iv_hex, raw[:len(raw)-16])

        result  = []
        # sed -nE 's|.*"url":"([^"]*)".*"height":([0-9]+).*|\2 >\1|p'
        for m in re.finditer(r'"url"\s*:\s*"([^"]+)"[^}]*"height"\s*:\s*(\d+)', plain):
            u = m.group(1).replace('\\u0026','&').replace('\\u003D','=')
            result.append(f"{m.group(2)} >{u}")
        for m in re.finditer(r'"height"\s*:\s*(\d+)[^}]*"url"\s*:\s*"([^"]+)"', plain):
            u = m.group(2).replace('\\u0026','&').replace('\\u003D','=')
            result.append(f"{m.group(1)} >{u}")

        return sorted(result,
            key=lambda x: int(x.split(' ')[0]) if x.split(' ')[0].isdigit() else 0,
            reverse=True)
    except Exception:
        return []

# ── generate_link — mirrors ani-cli's generate_link() ────────────────────────
# case $1 in
#   1) wixmp/Default   2) youtube/Yt-mp4   3) sharepoint/S-mp4
#   5) filemoon/Fm-mp4   *) hianime/Luf-Mp4

_PROV_KEY = {1:'Default', 2:'Yt-mp4', 3:'S-mp4', 4:'Luf-Mp4', 5:'Fm-mp4'}

def _generate_link(resp: dict[str, str], provider_num: int) -> list[str]:
    hex_path = resp.get(_PROV_KEY.get(provider_num, 'Luf-Mp4'), '')
    if not hex_path:
        return []
    decoded = _decode_provider_path(hex_path)
    if not decoded:
        return []
    if provider_num == 5:
        return _get_filemoon_links(decoded)
    return _get_links(decoded)

# ── select_quality — mirrors ani-cli's select_quality() ──────────────────────

def _select_quality(links: list[str], quality: str = 'best') -> str | None:
    if not links:
        return None
    if quality == 'best':
        chosen = links[0]
    elif quality == 'worst':
        numbered = [l for l in links if re.match(r'^\d{3,4}', l)]
        chosen   = (numbered[-1] if numbered else links[-1])
    else:
        chosen = next((l for l in links if quality in l), links[0])
    return chosen.split('>')[-1].strip()

# ── get_episode_url — mirrors ani-cli's get_episode_url() ────────────────────

def get_episode_url(anime_id: str, ep_no: str,
                    mode: str = 'sub', quality: str = 'best') -> dict | None:
    """
    Full stream extraction — faithful to ani-cli's get_episode_url():
      1. APQ GET with youtu-chan.com headers (faster)
      2. Fall back to POST GraphQL
      3. If 'tobeparsed' → AES decrypt
         Else → parse --hex sourceUrls directly
      4. Run all 5 providers in parallel (mirrors ani-cli's & background jobs)
      5. cat all results | sort -g -r -s → select_quality
    """
    gql = ('query ($showId: String!, $translationType: VaildTranslationTypeEnumType!, '
           '$episodeString: String!) { episode( showId: $showId translationType: $translationType '
           'episodeString: $episodeString ) { episodeString sourceUrls }}')

    q_hash = 'd405d0edd690624b66baba3068e0edc3ac90f1597d898a1ec8db4e5c43c00fec'
    # URL-encode like ani-cli's sed (same characters replaced)
    def _enc(s):
        return (s.replace('"','%22').replace(':','%3A').replace('{','%7B')
                 .replace('}','%7D').replace(',','%2C').replace(' ','%20'))

    q_vars = f'{{"showId":"{anime_id}","translationType":"{mode}","episodeString":"{ep_no}"}}'
    q_ext  = f'{{"persistedQuery":{{"version":1,"sha256Hash":"{q_hash}"}}}}'
    apq    = f"{ALLANIME_API}/api?variables={_enc(q_vars)}&extensions={_enc(q_ext)}"

    api_resp = ''
    _log("INFO", f"Stream request: id={anime_id} ep={ep_no} mode={mode}")

    # Try APQ first (ani-cli: curl -e "https://youtu-chan.com" ... "Origin: https://youtu-chan.com")
    try:
        r = _session.get(apq, headers={
            'User-Agent': AGENT,
            'Origin':     'https://youtu-chan.com',
            'Referer':    'https://youtu-chan.com',
            'Content-Type': 'application/json',
        }, timeout=12)
        _log("INFO", f"APQ status={r.status_code} tobeparsed={'tobeparsed' in r.text} sourceUrl={'sourceUrl' in r.text} len={len(r.text)}")
        if r.ok:
            api_resp = r.text
        else:
            _log("WARN", f"APQ non-ok: {r.status_code} body={r.text[:120]}")
    except Exception as e:
        _log("ERROR", f"APQ failed: {e}")

    # Fall back to POST if APQ gave nothing or no tobeparsed
    if not api_resp or ('tobeparsed' not in api_resp and 'sourceUrl' not in api_resp):
        _log("INFO", "APQ had no usable data → trying POST fallback")
        try:
            r = _session.post(f"{ALLANIME_API}/api",
                json={'variables': {'showId': anime_id, 'translationType': mode,
                                    'episodeString': ep_no},
                      'query': gql},
                headers={**_H, 'Content-Type': 'application/json'}, timeout=12)
            _log("INFO", f"POST status={r.status_code} tobeparsed={'tobeparsed' in r.text} sourceUrl={'sourceUrl' in r.text}")
            if r.ok:
                api_resp = r.text
            else:
                _log("WARN", f"POST non-ok: {r.status_code} body={r.text[:120]}")
        except Exception as e:
            _log("ERROR", f"POST failed: {e}")

    if not api_resp:
        _log("ERROR", "No API response — check network / cloudscraper installation")
        return None

    # Unescape JSON unicode (ani-cli: sed 's|\\u002F|\/|g')
    api_resp = api_resp.replace('\\u002F', '/')

    # Build resp dict {provider_name: hex_path} — mirrors ani-cli's $resp variable
    resp: dict[str, str] = {}

    if 'tobeparsed' in api_resp:
        m = re.search(r'"tobeparsed"\s*:\s*"([^"]+)"', api_resp)
        if m:
            _log("INFO", f"Decoding tobeparsed blob len={len(m.group(1))} HAS_AES={_HAS_AES}")
            resp = _decode_tobeparsed(m.group(1))
            _log("INFO", f"Decoded providers: {list(resp.keys()) or 'EMPTY — decryption failed'}")
        else:
            _log("WARN", "tobeparsed key found but regex failed to extract blob")
    else:
        _log("INFO", "No tobeparsed — parsing direct sourceUrls")

    if not resp:
        # Direct sourceUrls: sed -nE 's|.*sourceUrl":"--([^"]*)".*sourceName":"([^"]*)"|\2:\1|p'
        for chunk in re.sub(r'[{}]', '\n', api_resp).splitlines():
            name = re.search(r'"sourceName"\s*:\s*"([^"]+)"', chunk)
            url  = re.search(r'"sourceUrl"\s*:\s*"--([0-9a-fA-F]+)"', chunk)
            if name and url:
                resp[name.group(1)] = url.group(1)
        if resp:
            _log("INFO", f"Direct sourceUrl providers: {list(resp.keys())}")

    if not resp:
        _log("ERROR", f"No providers found. API preview: {api_resp[:200]}")
        return None

    # Run all 5 providers in parallel — mirrors ani-cli's & background jobs:
    #   for provider in 1 2 3 4 5; do generate_link "$provider" > cache/$provider & done; wait
    all_links: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_generate_link, resp, i) for i in range(1, 6)]
        for f in concurrent.futures.as_completed(futures):
            all_links.extend(f.result() or [])

    if not all_links:
        _log("ERROR", "All 5 providers returned empty — no stream links found")
        return None

    _log("INFO", f"Got {len(all_links)} links total. Best: {all_links[0][:80]}")
    # cat cache/* | sort -g -r -s  (sort by leading number, descending, stable)
    all_links.sort(
        key=lambda x: int(m.group(1)) if (m := re.match(r'^(\d+)', x)) else 0,
        reverse=True,
    )

    url = _select_quality(all_links, quality)
    if not url:
        return None

    return {'url': url, 'type': 'hls' if '.m3u8' in url else 'mp4'}

# ── search_anime — mirrors ani-cli's search_anime() ──────────────────────────

def search_anime(query: str, mode: str = 'sub') -> list:
    gql = ('query( $search: SearchInput $limit: Int $page: Int '
           '$translationType: VaildTranslationTypeEnumType $countryOrigin: VaildCountryOriginEnumType ) '
           '{ shows( search: $search limit: $limit page: $page translationType: $translationType '
           'countryOrigin: $countryOrigin ) { edges { _id name availableEpisodes __typename } }}')
    try:
        r = _session.post(f"{ALLANIME_API}/api", headers={**_H, 'Content-Type': 'application/json'}, timeout=12,
            json={'variables': {'search': {'allowAdult': False, 'allowUnknown': False,
                                            'query': query},
                                'limit': 40, 'page': 1,
                                'translationType': mode, 'countryOrigin': 'ALL'},
                  'query': gql})
        if r.ok:
            return r.json().get('data', {}).get('shows', {}).get('edges', [])
    except Exception:
        pass
    return []

# ── get_episodes — mirrors ani-cli's episodes_list() ─────────────────────────

def get_episodes(anime_id: str, mode: str = 'sub') -> list:
    gql = 'query ($showId: String!) { show( _id: $showId ) { _id availableEpisodesDetail }}'
    try:
        r = _session.post(f"{ALLANIME_API}/api", headers={**_H, 'Content-Type': 'application/json'}, timeout=12,
            json={'variables': {'showId': anime_id}, 'query': gql})
        if r.ok:
            eps = (r.json().get('data', {}).get('show', {})
                           .get('availableEpisodesDetail', {}).get(mode, []))
            try:
                return sorted(eps, key=lambda x: float(x))
            except Exception:
                return eps
    except Exception:
        pass
    return []

# ── Public interface for app.py ───────────────────────────────────────────────

def get_best_stream(anime_id: str, episode: str, translation_type: str = 'sub',
                    anime_title: str = '', quality: str = 'best') -> dict | None:
    return get_episode_url(anime_id, episode, mode=translation_type, quality=quality)

# ── AniList poster / metadata ─────────────────────────────────────────────────

def get_anilist_info(title: str) -> dict | None:
    gql = """
    query($search: String) {
        Media(search: $search, type: ANIME) {
            title { romaji english }
            coverImage { large extraLarge }
            bannerImage averageScore episodes status
            description(asHtml: false) genres seasonYear
        }
    }"""
    try:
        r = requests.post(ANILIST_API, timeout=10,
                          json={'query': gql, 'variables': {'search': title}})
        return r.json().get('data', {}).get('Media')
    except Exception:
        return None
