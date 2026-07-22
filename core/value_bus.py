"""跨協定即時值發布與訂閱匯流排。"""

from __future__ import annotations

import copy
import logging
import threading
from collections.abc import Callable
from typing import Any

from .data_model import PointValue


LOGGER = logging.getLogger(__name__)
ValueBusCallback = Callable[[PointValue], None]


def _safe_copy(value: Any) -> Any:
    """盡可能回傳深層副本，遇到不可複製物件時退回原物件。"""
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


class ValueBus:
    """執行緒安全地保存最新點位值並通知訂閱者。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._latest: dict[str, PointValue] = {}
        self._subscribers: list[ValueBusCallback] = []

    def publish(self, point_value: PointValue) -> None:
        """發布點位資料並逐一通知所有訂閱者。

        callback會在鎖定區域外執行。單一callback發生錯誤時只記錄例外，
        不會中斷ValueBus、通訊執行緒或其他訂閱者。
        """
        if not isinstance(point_value, PointValue):
            raise TypeError("ValueBus.publish只接受PointValue")

        stored_value = _safe_copy(point_value)
        with self._lock:
            self._latest[point_value.point_key] = stored_value
            subscribers = tuple(self._subscribers)

        for callback in subscribers:
            try:
                callback(_safe_copy(stored_value))
            except Exception:
                LOGGER.exception(
                    "ValueBus訂閱callback執行失敗，point_key=%s，callback=%r",
                    point_value.point_key,
                    callback,
                )

    def subscribe(self, callback: ValueBusCallback) -> None:
        """加入訂閱callback；相同callback不會重複加入。"""
        if not callable(callback):
            raise TypeError("callback必須可呼叫")
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: ValueBusCallback) -> None:
        """移除訂閱callback；不存在時不拋出錯誤。"""
        with self._lock:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

    def get_latest_dict(self) -> dict[str, PointValue]:
        """取得以point_key為鍵的最新點位資料副本。"""
        with self._lock:
            return {
                point_key: _safe_copy(point_value)
                for point_key, point_value in self._latest.items()
            }

    def get_latest_list(self) -> list[PointValue]:
        """取得依協定、來源及point_key排序的最新點位資料副本。"""
        with self._lock:
            values = [_safe_copy(value) for value in self._latest.values()]
        return sorted(
            values,
            key=lambda item: (item.protocol, item.source_name, item.point_key),
        )

    def clear(self) -> None:
        """清除所有最新點位值，但保留訂閱callback。"""
        with self._lock:
            self._latest.clear()
