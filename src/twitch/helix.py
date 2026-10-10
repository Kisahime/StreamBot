from __future__ import annotations

import httpx

from src.twitch.eventsub import HELIX, bearer_token


class HelixError(RuntimeError):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _headers(token: str, client_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {bearer_token(token)}",
        "Client-Id": client_id,
        "Content-Type": "application/json",
    }


async def user_id(token: str, client_id: str, login: str) -> str:
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get(f"{HELIX}/users", headers=_headers(token, client_id), params={"login": login})
    if r.status_code != 200:
        raise HelixError(f"user lookup HTTP {r.status_code}", r.status_code)
    data = r.json().get("data") or []
    if not data:
        raise HelixError(f"Twitch login not found: {login}")
    return str(data[0].get("id") or "")


async def shoutout(
    token: str,
    client_id: str,
    *,
    from_broadcaster_id: str,
    to_broadcaster_id: str,
    moderator_id: str,
) -> None:
    params = {
        "from_broadcaster_id": from_broadcaster_id,
        "to_broadcaster_id": to_broadcaster_id,
        "moderator_id": moderator_id,
    }
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.post(f"{HELIX}/chat/shoutouts", headers=_headers(token, client_id), params=params)
    if r.status_code == 429:
        raise HelixError(
            "shoutout rate limited (HTTP 429). Twitch is blocking more shoutouts for a bit.",
            429,
        )
    if r.status_code not in (200, 204):
        raise HelixError(f"shoutout HTTP {r.status_code}: {r.text[:200]}", r.status_code)


async def create_clip(token: str, client_id: str, broadcaster_id: str) -> dict[str, str]:
    """Create a clip. Returns the Helix id and edit URL."""
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.post(
            f"{HELIX}/clips",
            headers=_headers(token, client_id),
            params={"broadcaster_id": broadcaster_id},
        )
    if r.status_code not in (200, 202):
        raise HelixError(f"clip HTTP {r.status_code}: {r.text[:200]}", r.status_code)
    data = (r.json().get("data") or [{}])[0]
    if not isinstance(data, dict):
        data = {}
    return {"id": str(data.get("id") or ""), "edit_url": str(data.get("edit_url") or "")}


async def create_marker(token: str, client_id: str, broadcaster_id: str, description: str) -> None:
    body = {"user_id": broadcaster_id, "description": (description or "cohost moment")[:140]}
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.post(f"{HELIX}/streams/markers", headers=_headers(token, client_id), json=body)
    if r.status_code not in (200, 204):
        raise HelixError(f"marker HTTP {r.status_code}: {r.text[:200]}", r.status_code)
