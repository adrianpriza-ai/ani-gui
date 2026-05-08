import re
import requests

ALLANIME_API = "https://api.allanime.day/api"
ANILIST_API  = "https://graphql.anilist.co"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Referer":    "https://allanime.day",
}

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
        "page":  1,
    }
    try:
        r = requests.post(ALLANIME_API, json={"query": gql, "variables": variables},
                          headers=HEADERS, timeout=10)
        return r.json().get("data", {}).get("shows", {}).get("edges", [])
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
        r = requests.post(ALLANIME_API,
                          json={"query": gql, "variables": {"showId": anime_id}},
                          headers=HEADERS, timeout=10)
        detail = r.json().get("data", {}).get("show", {}).get("availableEpisodesDetail", {})
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
        "showId":          anime_id,
        "translationType": translation_type,
        "episodeString":   str(episode),
    }
    try:
        r = requests.post(ALLANIME_API, json={"query": gql, "variables": variables},
                          headers=HEADERS, timeout=10)
        return r.json().get("data", {}).get("episode", {}).get("sourceUrls", [])
    except Exception:
        return []

def _resolve(source_url: str) -> dict | None:
    """Try to extract a playable URL from a source entry."""
    if not source_url:
        return None
    if source_url.startswith("//"):
        source_url = "https:" + source_url
    elif source_url.startswith("/"):
        source_url = "https://allanime.day" + source_url

    try:
        r = requests.get(source_url, headers=HEADERS, timeout=10)
        text = r.text

        m3u8 = re.search(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', text)
        if m3u8:
            return {"url": m3u8.group(1), "type": "hls"}

        mp4 = re.search(r'(https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)', text)
        if mp4:
            return {"url": mp4.group(1), "type": "mp4"}

        # Sometimes the resolved URL itself is the stream
        if ".m3u8" in source_url:
            return {"url": source_url, "type": "hls"}
        if ".mp4" in source_url:
            return {"url": source_url, "type": "mp4"}
    except Exception:
        pass
    return None

def get_best_stream(anime_id: str, episode: str, translation_type: str = "sub") -> dict | None:
    sources = _get_sources(anime_id, episode, translation_type)
    # Prefer these providers (same priority order as ani-cli)
    priority = ["Yt-mp4", "Luf-mp4", "S-mp4", "Kir", "Ac"]

    def _rank(s):
        try:
            return priority.index(s.get("sourceName", ""))
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
        r = requests.post(ANILIST_API,
                          json={"query": gql, "variables": {"search": title}},
                          timeout=10)
        return r.json().get("data", {}).get("Media")
    except Exception:
        return None
