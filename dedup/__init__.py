"""流式去重窗口内核：滑动时间窗、精确集合与近似结构协同、容量淘汰。"""

from .core import DedupError, DedupWindow, Observation, PresenceSketch, digest64

__all__ = [
    "DedupError",
    "DedupWindow",
    "Observation",
    "PresenceSketch",
    "digest64",
]
