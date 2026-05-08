import re
from urllib.parse import urljoin

import requests

ALLANIME_ENDPOINTS = [
    "https://api.allanime.day/api",
    "https://allanime.day/api",
    "https://api.allanime.to/api",
    "https://allanime.to/api",
    "https://allanime.day/allanimeapi",
    "https://allanime.to/allanimeapi",
]
ANILIST_API = "https://graphql.anilist.co"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
}


def _post_allanime(query: str, variables: dict) -> dict:
    payload = {"query": query, "variables": variables}
    last_error = None
    for endpoint in ALLANIME_ENDPOINTS:
        headers = {**HEADERS, "Referer": endpoint.rsplit("/", 1)[0]}
        try:
            res = requests.post(endpoint, json=payload, headers=headers, timeout=12)
            if res.ok:
                data = res.json()
                if "data" in data:
                    return data
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    return {}


# ── Search ──────────────────────────────────────────────────────────────────

def search_anime(query: str, translation_type: str = "sub") -> list:
    gql = """
    query($search: SearchInput, $limit: Int, $page: Int) {
        shows(search: $search, limit: $limit, page: $page) {
            edges {
                _id name englishName availableEpisodes thumbnail score
            }
        }
    }
    """
    variables = {
        "search": {"query": query, "allowAdult": False},
        "limit": 20,
        "page": 1,
    }
    try:
        data = _post_allanime(gql, variables)
        return data.get("data", {}).get("shows", {}).get("edges", [])
    except Exception:
        return []


# ── Episodes ─────────────────────────────────────────────────────────────────

def get_episodes(anime_id: str, translation_type: str = "sub") -> list:
    gql = """
    query($showId: String!) {
        show(_id: $showId) { availableEpisodesDetail }
    }
    """
    try:
        data = _post_allanime(gql, {"showId": anime_id})
        detail = data.get("data", {}).get("show", {}).get("availableEpisodesDetail", {})
        eps = detail.get(translation_type, [])
        try:
            eps = sorted(eps, key=lambda x: float(x))
        except Exception:
            pass
        return eps
    except Exception:
        return []


# ── Stream ────────────────────────────────────────────────────────────────────

def _get_sources(anime_id: str, episode: str, translation_type: str) -> list:
    gql = """
    query($showId: String!, $translationType: VaildTranslationTypeEnumType!, $episodeString: String!) {
        episode(showId: $showId, translationType: $translationType, episodeString: $episodeString) {
            sourceUrls
        }
    }
    """
    variables = {
        "showId": anime_id,
        "translationType": translation_type,
        "episodeString": str(episode),
    }
    try:
        data = _post_allanime(gql, variables)
        return data.get("data", {}).get("episode", {}).get("sourceUrls", [])
    except Exception:
        return []




def _decode_allanime_url(encoded: str) -> str | None:
    """Decode AllAnime obfuscated source URLs (ani-cli compatible style)."""
    if not encoded:
        return None

    token = encoded.strip()
    if token.startswith('--'):
        token = token[2:]

    # Variant 1: hex-pairs XOR 56 (used in some Allanime sources)
    if re.fullmatch(r'[0-9a-fA-F]+', token) and len(token) % 2 == 0:
        try:
            plain = bytes.fromhex(token).decode("utf-8", errors="ignore")
            if plain.startswith("http") or plain.startswith("/") or plain.startswith("//"):
                return plain
        except Exception:
            pass
        try:
            decoded = ''.join(chr(int(token[i:i + 2], 16) ^ 56) for i in range(0, len(token), 2))
            if decoded.startswith('http') or decoded.startswith('/') or decoded.startswith('//'):
                return decoded
        except Exception:
            pass

    # Variant 2: encoded path separators using "--"
    if '--' in encoded and not re.fullmatch(r'[0-9a-fA-F]+', token):
        guessed = encoded.replace('--', '/')
        if guessed.startswith('http') or guessed.startswith('/') or guessed.startswith('//'):
            return guessed

    return None


def _candidate_source_urls(raw_source: str) -> list[str]:
    candidates = []
    if not raw_source:
        return candidates

    candidates.append(raw_source)
    decoded = _decode_allanime_url(raw_source)
    if decoded and decoded not in candidates:
        candidates.append(decoded)

    return candidates

def _resolve(source_url: str) -> dict | None:
    for raw in _candidate_source_urls(source_url):
        candidate_source = raw

        if candidate_source.startswith("//"):
            candidate_source = "https:" + candidate_source
        elif candidate_source.startswith("/"):
            candidate_source = urljoin("https://allanime.to", candidate_source)

        if ".m3u8" in candidate_source:
            return {"url": candidate_source, "type": "hls"}
        if ".mp4" in candidate_source:
            return {"url": candidate_source, "type": "mp4"}

        for base in ("https://allanime.to", "https://allanime.day"):
            headers = {**HEADERS, "Referer": base}
            try:
                candidate = (
                    candidate_source
                    if candidate_source.startswith("http")
                    else urljoin(base, candidate_source)
                )
                res = requests.get(candidate, headers=headers, timeout=12)
                text = res.text

                m3u8 = re.search(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', text)
                if m3u8:
                    return {"url": m3u8.group(1), "type": "hls"}

                mp4 = re.search(r'(https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)', text)
                if mp4:
                    return {"url": mp4.group(1), "type": "mp4"}
            except Exception:
                continue

    return None


def get_best_stream(anime_id: str, episode: str, translation_type: str = "sub") -> dict | None:
    sources = _get_sources(anime_id, episode, translation_type)
    priority = ["Yt-mp4", "Luf-mp4", "S-mp4", "Kir", "Ac"]

    def _rank(source: dict) -> int:
        try:
            return priority.index(source.get("sourceName", ""))
        except ValueError:
            return 99

    for source in sorted(sources, key=_rank):
        result = _resolve(source.get("sourceUrl", ""))
        if result:
            return result
    return None


# ── AniList ───────────────────────────────────────────────────────────────────

def get_anilist_info(title: str) -> dict | None:
    gql = """
    query($search: String) {
        Media(search: $search, type: ANIME) {
            title { romaji english }
            coverImage { large extraLarge }
            bannerImage
            averageScore
            episodes
            status
            description(asHtml: false)
            genres
            seasonYear
        }
    }
    """
    try:
        r = requests.post(
            ANILIST_API,
            json={"query": gql, "variables": {"search": title}},
            timeout=10,
        )
        return r.json().get("data", {}).get("Media")
    except Exception:
        return None
