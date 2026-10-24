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
        logger.info("populus_synapse: storage allowance check enabled (%s)", config.account_service_url)

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


def permits(allowance: Allowance, size: int) -> bool:
    if allowance.over:
        return False
    return allowance.remaining_bytes is None or size <= allowance.remaining_bytes


async def with_timeout(awaitable: Awaitable[Any], seconds: float) -> Any:
    d = defer.ensureDeferred(awaitable)
    d.addTimeout(seconds, reactor)
    return await d
