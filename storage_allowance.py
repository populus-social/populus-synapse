# SPDX-License-Identifier: AGPL-3.0-only
"""Refuse media uploads over the member's storage allowance (#1982).

Synapse calls is_user_allowed_to_upload_media_of_size(user_id, size) for
every media and avatar upload. The module asks account-service's storage
allowance contract (#1981) and answers False when the upload would exceed
what the member has left. Text messages never reach this callback.

Failure policy: fail open. Quota is a business limit, and an unreachable
account-service must not stop a member's chat. A 401 means the key is
missing or wrong, which is a misconfiguration and not an outage, so it is
logged at error.
"""

import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

import attr
from twisted.internet import defer, reactor

logger = logging.getLogger(__name__)

_CHAT_SERVICE = "chat"


@attr.s(auto_attribs=True, frozen=True)
class StorageAllowanceConfig:
    account_service_url: str
    api_key_file: str
    cache_ttl_seconds: float = 5.0
    timeout_seconds: float = 2.0
    # #1945: account-service's Sunset state route. Empty leaves Chat's
    # read-only mode off, so the two features land independently.
    sunset_state_url: str = ""


@attr.s(auto_attribs=True, frozen=True)
class Allowance:
    """The contract's answer. remaining_bytes None means no limit."""

    over: bool
    remaining_bytes: Optional[int]


class _Unauthorized(Exception):
    pass


def localpart(user_id: str) -> str:
    """'@name:server' -> 'name'. The Matrix localpart is the pseudonym's
    shortname, the key the contract's `pseudonym` parameter expects."""
    return user_id[1:].split(":", 1)[0] if user_id.startswith("@") else user_id


class StorageAllowanceModule:
    def __init__(self, config: StorageAllowanceConfig, api: Any, clock: Callable[[], float] = time.monotonic):
        self._config = config
        self._api = api
        self._clock = clock
        self._cache: Dict[str, Tuple[float, Allowance]] = {}
        self._api_key: Optional[str] = None
        api.register_media_repository_callbacks(
            is_user_allowed_to_upload_media_of_size=self.is_user_allowed_to_upload_media_of_size,
        )
        self._sunset_cache: Dict[str, Tuple[float, bool]] = {}
        logger.info("populus_synapse: storage allowance check enabled (%s)", config.account_service_url)
        if config.sunset_state_url:
            api.register_spam_checker_callbacks(
                check_event_for_spam=self.check_event_for_spam,
                user_may_create_room=self.user_may_create_room,
                user_may_invite=self.user_may_invite,
                user_may_join_room=self.user_may_join_room,
            )
            logger.info("populus_synapse: Chat read-only during Sunset enabled (%s)", config.sunset_state_url)

    @staticmethod
    def parse_config(config: Dict[str, Any]) -> StorageAllowanceConfig:
        for key in ("account_service_url", "api_key_file"):
            if not config.get(key):
                raise ValueError("populus_synapse: %s is required" % key)
        return StorageAllowanceConfig(
            account_service_url=str(config["account_service_url"]),
            api_key_file=str(config["api_key_file"]),
            cache_ttl_seconds=float(config.get("cache_ttl_seconds", 5)),
            timeout_seconds=float(config.get("timeout_seconds", 2)),
            sunset_state_url=str(config.get("sunset_state_url", "")),
        )

    async def is_user_allowed_to_upload_media_of_size(self, user_id: str, size: int) -> bool:
        try:
            allowance = await self._allowance(localpart(user_id))
        except _Unauthorized:
            logger.error("populus_synapse: account-service refused the chat API key (401); allowing upload")
            return True
        except Exception as e:  # noqa: BLE001 - every failure fails open
            logger.warning("populus_synapse: allowance lookup failed, allowing upload: %r", e)
            return True
        return permits(allowance, size)

    # --- Chat read-only during Sunset (#1945) -------------------------------
    # Reading, key backup and Element's export keep working: they are not
    # events or room actions. Leaving a room stays allowed. Every failure
    # fails open, like the allowance check.

    async def _restricted(self, user_id: str) -> bool:
        pseudonym = localpart(user_id)
        now = self._clock()
        hit = self._sunset_cache.get(pseudonym)
        if hit and now - hit[0] < self._config.cache_ttl_seconds:
            return hit[1]
        try:
            call = self._api.http_client.get_json(
                self._config.sunset_state_url,
                args={"pseudonym": pseudonym},
                headers={"Authorization": ["Bearer " + self._key()]},
            )
            body = await with_timeout(call, self._config.timeout_seconds)
            restricted = bool(body.get("restricted", False))
        except Exception as e:  # noqa: BLE001 - every failure fails open
            if getattr(e, "code", None) == 401:
                logger.error("populus_synapse: account-service refused the chat API key (401); allowing")
            else:
                logger.warning("populus_synapse: sunset state lookup failed, allowing: %r", e)
            return False
        self._sunset_cache[pseudonym] = (now, restricted)
        if len(self._sunset_cache) > 1024:
            self._sunset_cache = {
                k: v for k, v in self._sunset_cache.items() if now - v[0] < self._config.cache_ttl_seconds
            }
        return restricted

    async def check_event_for_spam(self, event: Any) -> Any:
        if _is_leave(event) or not await self._restricted(event.sender):
            return _allow()
        return _deny()

    async def user_may_create_room(self, user_id: str) -> Any:
        return _deny() if await self._restricted(user_id) else _allow()

    async def user_may_invite(self, inviter: str, invitee: str, room_id: str) -> Any:
        return _deny() if await self._restricted(inviter) else _allow()

    async def user_may_join_room(self, user_id: str, room_id: str, is_invited: bool) -> Any:
        return _deny() if await self._restricted(user_id) else _allow()

    async def _allowance(self, pseudonym: str) -> Allowance:
        now = self._clock()
        hit = self._cache.get(pseudonym)
        if hit and now - hit[0] < self._config.cache_ttl_seconds:
            return hit[1]
        allowance = await self._fetch(pseudonym)
        self._cache[pseudonym] = (now, allowance)
        if len(self._cache) > 1024:
            self._cache = {k: v for k, v in self._cache.items() if now - v[0] < self._config.cache_ttl_seconds}
        return allowance

    def _key(self) -> str:
        if self._api_key is None:
            with open(self._config.api_key_file) as f:
                self._api_key = f.read().strip()
        return self._api_key

    async def _fetch(self, pseudonym: str) -> Allowance:
        call = self._api.http_client.get_json(
            self._config.account_service_url,
            args={"service": _CHAT_SERVICE, "pseudonym": pseudonym},
            headers={"Authorization": ["Bearer " + self._key()]},
        )
        try:
            body = await with_timeout(call, self._config.timeout_seconds)
        except Exception as e:
            # Synapse's HttpResponseException carries the status as .code.
            if getattr(e, "code", None) == 401:
                raise _Unauthorized() from e
            raise
        remaining = body.get("remaining_bytes")
        return Allowance(
            over=bool(body.get("over", False)),
            remaining_bytes=None if remaining is None else int(remaining),
        )


def _is_leave(event: Any) -> bool:
    content = getattr(event, "content", None) or {}
    return getattr(event, "type", None) == "m.room.member" and content.get("membership") == "leave"


def _allow() -> Any:
    from synapse.module_api import NOT_SPAM

    return NOT_SPAM


def _deny() -> Any:
    from synapse.module_api.errors import Codes

    return Codes.FORBIDDEN


def permits(allowance: Allowance, size: int) -> bool:
    if allowance.over:
        return False
    return allowance.remaining_bytes is None or size <= allowance.remaining_bytes


async def with_timeout(awaitable: Awaitable[Any], seconds: float) -> Any:
    d = defer.ensureDeferred(awaitable)
    d.addTimeout(seconds, reactor)
    return await d
