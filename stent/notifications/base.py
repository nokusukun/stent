from typing import Protocol, AsyncIterator, Any, Dict, Optional

class NotificationBackend(Protocol):
    async def notify_task_completed(self, task_id: str) -> None: ...
    async def notify_task_updated(self, task_id: str, state: str) -> None: ...
    def subscribe_to_task(
        self,
        task_id: str,
        *,
        expiry: float | None = None,
    ) -> AsyncIterator[Dict[str, Any]]: ...

    async def notify_execution_updated(self, execution_id: str, state: str) -> None: ...
    def subscribe_to_execution(
        self,
        execution_id: str,
        *,
        expiry: float | None = None,
    ) -> AsyncIterator[Dict[str, Any]]: ...