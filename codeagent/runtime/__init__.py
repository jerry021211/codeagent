"""Runtime cancellation primitives and lazily loaded background supervisor."""

from codeagent.runtime.cancellation import CancellationToken, CancelledError
from codeagent.runtime.data_paths import RuntimeDataPaths, default_runtime_data_dir


def __getattr__(name: str):
    if name in {"BackgroundTaskRunner", "TeamSupervisor"}:
        from codeagent.runtime.background import BackgroundTaskRunner, TeamSupervisor

        return {
            "BackgroundTaskRunner": BackgroundTaskRunner,
            "TeamSupervisor": TeamSupervisor,
        }[name]
    raise AttributeError(name)

__all__ = [
    "BackgroundTaskRunner",
    "CancellationToken",
    "CancelledError",
    "RuntimeDataPaths",
    "TeamSupervisor",
    "default_runtime_data_dir",
]
