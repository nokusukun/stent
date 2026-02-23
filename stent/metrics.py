from __future__ import annotations

from typing import Protocol


class MetricsRecorder(Protocol):
    """
    Receives callbacks when important Stent events occur so hosts can export
    metrics to Prometheus, StatsD, etc.
    """

    def task_claimed(self, *, queue: str | None, step_name: str, kind: str) -> None: ...

    def task_completed(
        self,
        *,
        queue: str | None,
        step_name: str,
        kind: str,
        duration_s: float,
    ) -> None: ...

    def task_failed(
        self,
        *,
        queue: str | None,
        step_name: str,
        kind: str,
        reason: str,
        retrying: bool,
    ) -> None: ...

    def dead_lettered(
        self,
        *,
        queue: str | None,
        step_name: str,
        kind: str,
        reason: str,
    ) -> None: ...

    def lease_renewed(self, *, task_id: str, success: bool) -> None: ...


class NoOpMetricsRecorder:
    def task_claimed(self, **_kwargs) -> None:
        pass

    def task_completed(self, **_kwargs) -> None:
        pass

    def task_failed(self, **_kwargs) -> None:
        pass

    def dead_lettered(self, **_kwargs) -> None:
        pass

    def lease_renewed(self, **_kwargs) -> None:
        pass
