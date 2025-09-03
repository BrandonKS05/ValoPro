import aiohttp
import asyncio
from typing import Any, Dict, List

BASE = "https://vlrggapi.vercel.app"

async def _fetch_json(session: aiohttp.ClientSession, path: str, params: Dict[str, str] | None = None) -> Dict[str, Any]:
    url = f"{BASE}{path}"
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        return await resp.json()

async def get_upcoming(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    # { data: { segments: [...] } }
    data = await _fetch_json(session, "/match", {"q": "upcoming"})
    return data.get("data", {}).get("segments", []) or []

async def get_live(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    data = await _fetch_json(session, "/match", {"q": "live_score"})
    return data.get("data", {}).get("segments", []) or []

def normalize_match(m: Dict[str, Any]) -> Dict[str, Any]:
    # Normalize common fields defensively; API is unofficial and may change.
    team1 = m.get("team1", {}).get("name") or m.get("team_1", "") or ""
    team2 = m.get("team2", {}).get("name") or m.get("team_2", "") or ""
    event = m.get("tournament", {}).get("name") or m.get("event", "") or ""
    status = m.get("status", "")
    time_unix = m.get("time_unix") or m.get("unix_time") or 0
    match_id = m.get("match_id") or m.get("id") or ""
    url = m.get("url") or (f"https://www.vlr.gg/{match_id}" if match_id else "")
    score = m.get("score") or m.get("maps", [])
    region = m.get("tournament", {}).get("region") or m.get("region", "") or ""
    return {
        "team1": team1, "team2": team2, "event": event, "status": status,
        "time_unix": int(time_unix) if time_unix else 0, "match_id": match_id,
        "url": url, "score": score, "region": region
    }

def filter_matches(matches: List[Dict[str, Any]], needle: str) -> List[Dict[str, Any]]:
    needle = needle.lower()
    def _ok(m):
        fields = [m.get("event",""), m.get("team1",""), m.get("team2",""), m.get("region","")]
        return any(needle in (f or "").lower() for f in fields)
    return [m for m in matches if _ok(m)]
