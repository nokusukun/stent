import redis.asyncio as redis
import json
import asyncio
from typing import AsyncIterator, Dict, Any, Optional
from dfns.notifications.base import NotificationBackend

class RedisBackend(NotificationBackend):
    def __init__(self, url: str):
        self.redis = redis.from_url(url)

    async def notify_task_completed(self, task_id: str) -> None:
        await self.redis.publish(f"dfns:task:{task_id}", json.dumps({
            "task_id": task_id,
            "state": "completed"
        }))

    async def notify_task_updated(self, task_id: str, state: str) -> None:
        await self.redis.publish(f"dfns:task:{task_id}", json.dumps({
            "task_id": task_id,
            "state": state
        }))

    async def subscribe_to_task(
        self,
        task_id: str,
        *,
        expiry: float | None = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        pubsub = self.redis.pubsub()
        channel = f"dfns:task:{task_id}"
        await pubsub.subscribe(channel)
        
        try:
            async def _listen():
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        data = json.loads(message["data"])
                        yield data
                        if data.get("state") in ("completed", "failed"):
                            break

            if expiry:
                try:
                    async with asyncio.expiry(expiry):
                        async for item in _listen():
                            yield item
                except asyncio.ExpiryError:
                    pass
            else:
                 async for item in _listen():
                     yield item
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    async def notify_execution_updated(self, execution_id: str, state: str) -> None:
        await self.redis.publish(f"dfns:execution:{execution_id}", json.dumps({
            "execution_id": execution_id,
            "state": state
        }))

    async def subscribe_to_execution(
        self,
        execution_id: str,
        *,
        expiry: float | None = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        pubsub = self.redis.pubsub()
        channel = f"dfns:execution:{execution_id}"
        await pubsub.subscribe(channel)
        
        try:
            async def _listen():
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        data = json.loads(message["data"])
                        yield data
                        if data.get("state") in ("completed", "failed", "timed_out", "cancelled"):
                            break

            if expiry:
                try:
                    async with asyncio.expiry(expiry):
                        async for item in _listen():
                            yield item
                except asyncio.ExpiryError:
                    pass
            else:
                 async for item in _listen():
                     yield item
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()