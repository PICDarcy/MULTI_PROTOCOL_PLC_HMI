"""跨協定即時值發布與訂閱匯流排。"""

from __future__ import annotations

import threading
from collections.abc import Callable

from .data_model import PointValue


class ValueBus:
    """保存每個point_key最新值，並將更新通知所有訂閱者。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._latest: dict[str, PointValue] = {}
        self._subscribers: list[Callable[[PointValue], None]] = []

    def publish(self, point_value: PointValue) -> None:
        if not isinstance(point_value, PointValue):
            raise TypeError("ValueBus.publish只接受PointValue")
        with self._lock:
            self._latest[point_value.point_key] = point_value
            subscribers = tuple(self._subscribers)
        for callback in subscribers:
            try:
                callback(point_value)
            except Exception:
                # 單一訂閱者失敗不可中斷通訊執行緒或其他訂閱者。
                continue

    def subscribe(self, callback: Callable[[PointValue], None]) -> None:
        if not callable(callback):
            raise TypeError("callback必須可呼叫")
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[PointValue], None]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

    def get_latest_dict(self) -> dict[str, PointValue]:
        with self._lock:
            return dict(self._latest)

    def get_latest_list(self) -> list[PointValue]:
        with self._lock:
            return sorted(
                self._latest.values(),
                key=lambda item: (item.protocol, item.source_name, item.point_key),
            )

    def clear(self) -> None:
        with self._lock:
            self._latest.clear()
