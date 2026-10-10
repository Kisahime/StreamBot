"""Twitch token checks that never return the token itself."""

from __future__ import annotations

from typing import Any

import httpx

VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"

# Empty tuple: no user scope. A one-item tuple is required. A longer tuple means any one of them.
SUBSCRIPTION_SCOPES: dict[str, tuple[str, ...]] = {
    "channel.follow": ("moderator:read:followers",),
    "channel.subscribe": ("channel:read:subscriptions",),
    "channel.subscription.gift": ("channel:read:subscriptions",),
    "channel.subscription.message": ("channel:read:subscriptions",),
    "channel.raid": (),
    "channel.cheer": ("bits:read",),
    "channel.channel_points_custom_reward_redemption.add": (
        "channel:read:redemptions",
        "channel:manage:redemptions",
    ),
    "channel.hype_train.begin": ("channel:read:hype_train",),
    "channel.hype_train.progress": ("channel:read:hype_train",),
    "channel.hype_train.end": ("channel:read:hype_train",),
    "channel.poll.begin": ("channel:read:polls", "channel:manage:polls"),
    "channel.poll.end": ("channel:read:polls", "channel:manage:polls"),
    "channel.prediction.begin": ("channel:read:predictions", "channel:manage:predictions"),
    "channel.prediction.end": ("channel:read:predictions", "channel:manage:predictions"),
}

TYPE_TO_FEED: dict[str, str] = {
    "channel.follow": "follow",
    "channel.subscribe": "subscribe",
    "channel.subscription.gift": "gift",
    "channel.subscription.message": "resub",
    "channel.raid": "raid",
    "channel.cheer": "bits",
    "channel.channel_points_custom_reward_redemption.add": "channel_points",
    "channel.hype_train.begin": "hype_train",
    "channel.hype_train.progress": "hype_train",
    "channel.hype_train.end": "hype_train",
    "channel.poll.begin": "polls",
    "channel.poll.end": "polls",
    "channel.prediction.begin": "predictions",
    "channel.prediction.end": "predictions",
}

FEED_ORDER = (
    "follow",
    "subscribe",
    "gift",
    "resub",
    "raid",
    "bits",
    "channel_points",
    "hype_train",
    "polls",
    "predictions",
)

CORE_TYPES = (
    "channel.follow",
    "channel.subscribe",
    "channel.subscription.gift",
    "channel.subscription.message",
    "channel.raid",
)


def missing_scope(scopes: set[str] | None, subscription_type: str) -> str | None:
    """The scope to report when this subscription must be skipped.

    ``None`` scopes means validation did not run, so nothing is skipped.
    """
    if scopes is None:
        return None
    needed = SUBSCRIPTION_SCOPES.get(subscription_type, ())
    if not needed or any(scope in scopes for scope in needed):
        return None
    return needed[0]


def feed_for(subscription_type: str) -> str:
    return TYPE_TO_FEED.get(subscription_type, subscription_type)


async def validate(token: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Call Twitch's validate endpoint.

    The returned dict has login, client_id, scopes, expires_in, and user_id.
    It never includes the token.
    """
    from src.twitch.eventsub import bearer_token

    raw = bearer_token(token)
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await http.get(VALIDATE_URL, headers={"Authorization": f"OAuth {raw}"})
        status = response.status_code
        try:
            body = response.json()
        except Exception:
            body = {}
    finally:
        if owns_client:
            await http.aclose()
    if not isinstance(body, dict):
        body = {}
    if status != 200:
        detail = str(body.get("message") or "")
        for secret in (token, f"oauth:{raw}", raw):
            if secret and secret in detail:
                detail = detail.replace(secret, "[redacted]")
        raise RuntimeError(f"token validate HTTP {status}: {detail[:180]}")
    scopes = body.get("scopes") or []
    if not isinstance(scopes, list):
        scopes = []
    expires = body.get("expires_in")
    return {
        "login": str(body.get("login") or ""),
        "client_id": str(body.get("client_id") or ""),
        "scopes": [str(scope) for scope in scopes],
        "expires_in": int(expires) if expires is not None else None,
        "user_id": str(body.get("user_id") or ""),
    }
