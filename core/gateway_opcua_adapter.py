"""Canonical ValueBus 到唯讀 OPC UA Server 的輸出 Adapter。"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import CancelledError, Future
from dataclasses import dataclass

from asyncua import ua

from .data_model import GatewayModel, PointValue, normalize_data_type


_VARIANT_TYPES = {
    "Boolean": ua.VariantType.Boolean,
    "UInt16": ua.VariantType.UInt16,
}

_DEFAULT_VALUES = {
    "Boolean": False,
    "UInt16": 0,
}


@dataclass(frozen=True, slots=True)
class _OutputBinding:
    tag_id: str
    node_id: str
    browse_name: str
    data_type: str
    variant_type: ua.VariantType


class GatewayOpcuaOutputAdapter:
    """依 Canonical Tag mapping 將 Good scalar 值發布到 OPC UA 輸出。"""

    def __init__(
        self,
        config_manager,
        value_bus,
        server,
        event_loop: asyncio.AbstractEventLoop,
        log_func=None,
    ) -> None:
        self.config_manager = config_manager
        self.value_bus = value_bus
        self.server = server
        self.event_loop = event_loop
        self.log_func = log_func
        self._lock = threading.RLock()
        self._bindings: dict[str, _OutputBinding] = {}
        self._versions: dict[str, int] = {}
        self._publish_locks: dict[str, asyncio.Lock] = {}
        self._pending_futures: set[Future] = set()
        self._running = False

    def _log(self, message: str, level: str = "INFO") -> None:
        if not callable(self.log_func):
            return
        try:
            self.log_func(message, level)
        except TypeError:
            self.log_func(message)

    def _gateway_model(self) -> GatewayModel:
        getter = getattr(self.config_manager, "get_gateway_model", None)
        if callable(getter):
            model = getter()
            if isinstance(model, GatewayModel):
                return model
        section_getter = getattr(self.config_manager, "get_section", None)
        value = (
            section_getter("gateway_model", {})
            if callable(section_getter)
            else {}
        )
        return GatewayModel.from_dict(value)

    def _load_bindings(self) -> dict[str, _OutputBinding]:
        bindings: dict[str, _OutputBinding] = {}
        for tag in self._gateway_model().tags:
            mapping = tag.opcua_output
            data_type = normalize_data_type(tag.data_type)
            variant_type = _VARIANT_TYPES.get(data_type)
            if (
                not tag.enabled
                or not mapping.enabled
                or variant_type is None
            ):
                continue
            bindings[str(tag.tag_id)] = _OutputBinding(
                tag_id=str(tag.tag_id),
                node_id=str(mapping.node_id),
                browse_name=str(mapping.browse_name or tag.name or tag.tag_id),
                data_type=data_type,
                variant_type=variant_type,
            )
        return bindings

    async def start(self) -> None:
        with self._lock:
            if self._running:
                return
            bindings = self._load_bindings()

        for binding in bindings.values():
            await self.server.add_readonly_variable(
                tag_id=binding.node_id,
                display_name=binding.browse_name,
                value=_DEFAULT_VALUES[binding.data_type],
                variant_type=binding.variant_type,
            )

        with self._lock:
            if self._running:
                return
            self._bindings = bindings
            self._versions = {tag_id: 0 for tag_id in bindings}
            self._publish_locks = {
                tag_id: asyncio.Lock() for tag_id in bindings
            }
            self._running = True
            self.value_bus.subscribe(self._consume)
            snapshot = self.value_bus.get_latest_list()
            replay: list[
                tuple[_OutputBinding, PointValue, int, asyncio.Lock]
            ] = []
            for point_value in snapshot:
                binding = bindings.get(str(point_value.tag_id))
                if binding is None:
                    continue
                version = self._next_version_locked(binding.tag_id)
                replay.append(
                    (
                        binding,
                        point_value,
                        version,
                        self._publish_locks[binding.tag_id],
                    )
                )

        for binding, point_value, version, publish_lock in replay:
            await self._publish_binding(
                binding,
                point_value,
                version,
                publish_lock,
            )

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            self.value_bus.unsubscribe(self._consume)
            futures = tuple(self._pending_futures)
            self._pending_futures.clear()
            self._bindings = {}
            self._versions = {}
            self._publish_locks = {}
        for future in futures:
            future.cancel()

    def _next_version_locked(self, tag_id: str) -> int:
        version = self._versions.get(tag_id, 0) + 1
        self._versions[tag_id] = version
        return version

    def _consume(self, point_value: PointValue) -> None:
        with self._lock:
            if not self._running:
                return
            binding = self._bindings.get(str(point_value.tag_id))
            if binding is None:
                return
            version = self._next_version_locked(binding.tag_id)
            publish_lock = self._publish_locks[binding.tag_id]
            future = asyncio.run_coroutine_threadsafe(
                self._publish_binding(
                    binding,
                    point_value,
                    version,
                    publish_lock,
                ),
                self.event_loop,
            )
            self._pending_futures.add(future)
        future.add_done_callback(self._report_future_error)

    def _report_future_error(self, future) -> None:
        with self._lock:
            self._pending_futures.discard(future)
        try:
            future.result()
        except CancelledError:
            return
        except Exception as exc:
            self._log(f"OPC UA輸出更新失敗：{exc}", "ERROR")

    async def _publish_binding(
        self,
        binding: _OutputBinding,
        point_value: PointValue,
        version: int,
        publish_lock: asyncio.Lock,
    ) -> None:
        async with publish_lock:
            with self._lock:
                if (
                    not self._running
                    or self._versions.get(binding.tag_id) != version
                ):
                    return
            if not str(point_value.quality).startswith("Good"):
                return
            actual_type = normalize_data_type(point_value.data_type)
            if actual_type != binding.data_type:
                self._log(
                    f"OPC UA輸出略過Tag「{binding.tag_id}」：來源型別由"
                    f"{binding.data_type}變更為{actual_type}",
                    "WARNING",
                )
                return
            value = point_value.value
            if binding.data_type == "Boolean":
                if not isinstance(value, bool):
                    self._log(
                        f"OPC UA輸出Tag「{binding.tag_id}」更新失敗："
                        "Boolean只接受bool值",
                        "ERROR",
                    )
                    return
            elif binding.data_type == "UInt16":
                if isinstance(value, bool) or not isinstance(value, int):
                    self._log(
                        f"OPC UA輸出Tag「{binding.tag_id}」更新失敗："
                        "UInt16只接受整數值",
                        "ERROR",
                    )
                    return
                if not 0 <= value <= 0xFFFF:
                    self._log(
                        f"OPC UA輸出Tag「{binding.tag_id}」更新失敗："
                        "UInt16必須介於0到65535",
                        "ERROR",
                    )
                    return
            await self.server.publish_value(
                tag_id=binding.node_id,
                value=value,
                variant_type=binding.variant_type,
                source_timestamp=point_value.source_timestamp,
                server_timestamp=point_value.server_timestamp,
            )
