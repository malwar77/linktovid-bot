"""
Spotify / Shazam music resolver.

Both sites are metadata pages (no downloadable stream), so we:
1. Resolve the track name (and artist, when possible) from the page.
2. Search YouTube for the song and pull the best audio stream.
3. Let the caller convert it to mp3 and ship it out.

This module only does resolution; bot.py drives the download/send.
"""

import json
import re
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TIMEOUT = 20

MUSIC_HOSTS = ("open.spotify.com", "spotify.com", "www.shazam.com",
               "shazam.com")


def is_music_url(url: str) -> bool:
    """True if this URL is a Spotify/Shazam link we handle specially."""
    try:
        host = urllib.parse.urlsplit(url).netloc.lower()
    except Exception:
        return False
    return host in MUSIC_HOSTS


def _get(url: str, timeout: int = TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _similar(a: str, b: str) -> bool:
    """Loose title match ignoring punctuation/case/bracket extras."""
    def norm(s):
        s = re.sub(r"\(.*?\)|\[.*?\]", " ", s.lower())
        return re.sub(r"[^a-z0-9]+", "", s)
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    return na in nb or nb in na


def _deezer_artist(song: str) -> str:
    """Best-effort artist lookup by track title via Deezer's public API."""
    try:
        q = urllib.parse.quote(f'track:"{song}"')
        data = json.loads(_get(f"https://api.deezer.com/search?q={q}&limit=1",
                               timeout=15))
        hits = data.get("data") or []
        if hits and _similar(hits[0]["title"], song):
            return hits[0]["artist"]["name"]
    except Exception:
        pass
    return ""


_SPOTIFY_KIND_RE = re.compile(
    r"spotify\.com/(track|album|playlist|episode|show)/")


def _spotify(url: str) -> dict:
    m = _SPOTIFY_KIND_RE.search(url)
    if m and m.group(1) != "track":
        raise ValueError(
            "That's a Spotify %s link. Send me a single track link "
            "(Share -> Copy Link on a song) and I'll grab the audio."
            % m.group(1)
        )
    oembed = json.loads(_get(
        "https://open.spotify.com/oembed?url=" + urllib.parse.quote(url, safe="")
    ))
    song = (oembed.get("title") or "").strip()
    if not song:
        raise ValueError("Couldn't read that Spotify link. Check it's public.")
    cover = oembed.get("thumbnail_url") or ""
    artist = _deezer_artist(song)
    return {"song": song, "artist": artist, "cover": cover,
            "source": "Spotify"}


def _fetch(url: str) -> str:
    """Robust page fetch: Shazam/Spotify CDNs fingerprint TLS and reject
    plain urllib, so impersonate Chrome first, fall back to urllib."""
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", timeout=TIMEOUT)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return _get(url)


def _shazam(url: str) -> dict:
    html = _fetch(url)
    m = re.search(
        r'<meta[^>]+property="og:title"[^>]*content="([^"]*)"', html)
    if not m:
        raise ValueError(
            "Couldn't read that Shazam link. Open the track page in Shazam "
            "and share that link.")
    # og:title looks like: "DtMF - Bad Bunny: Song Lyrics, Music Videos..."
    og = m.group(1)
    og = re.sub(r":\s*(Song Lyrics|Song|Music).*", "", og).strip()
    og = og.replace("&amp;", "&")
    if " - " in og:
        song, artist = og.split(" - ", 1)
    else:
        song, artist = og, ""
    cover_m = re.search(
        r'<meta[^>]+property="og:image"[^>]*content="([^"]*)"', html)
    return {"song": song.strip(), "artist": artist.strip(),
            "cover": cover_m.group(1) if cover_m else "",
            "source": "Shazam"}


def resolve_track(url: str) -> dict:
    """Return {'song', 'artist', 'cover', 'source'} for a Spotify/Shazam URL.

    Raises ValueError with a user-friendly message when the link can't be
    resolved (album/playlist, private page, ...).
    """
    host = urllib.parse.urlsplit(url).netloc.lower()
    if "spotify" in host:
        return _spotify(url)
    return _shazam(url)


def search_query(track: dict) -> str:
    """YouTube search phrase for the resolved track."""
    if track["artist"]:
        return f'{track["artist"]} {track["song"]} audio'
    return f'{track["song"]} audio'
