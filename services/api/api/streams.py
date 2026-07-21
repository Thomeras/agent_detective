"""Redis Streams publisher (build spec section 4.1).

Stream field values are strings (Redis limitation): `schema_version` is sent
as "1" and a missing `tier1_verdict_ref` as "".
"""

import redis.asyncio as redis

TIER2_STREAM = "ad.graphs.tier2"


class RedisStreamPublisher:
    def __init__(self, redis_url: str):
        self._redis = redis.from_url(redis_url)

    async def publish_tier2(self, message: dict[str, str]) -> str:
        """XADD one message to ad.graphs.tier2; returns the stream entry id."""
        entry_id = await self._redis.xadd(TIER2_STREAM, message)
        return entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)

    async def close(self) -> None:
        await self._redis.aclose()
