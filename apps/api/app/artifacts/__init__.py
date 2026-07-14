"""Plugin artifact review and publication domain."""

from typing import Any

__all__ = ["ArtifactRuntime", "build_artifact_runtime"]


def __getattr__(name: str) -> Any:
    """延迟加载市场运行时，避免独立 runner 导入 API 依赖。"""
    if name == "ArtifactRuntime":
        from .runtime import ArtifactRuntime

        return ArtifactRuntime
    if name == "build_artifact_runtime":
        from .runtime import build_artifact_runtime

        return build_artifact_runtime
    raise AttributeError(name)
