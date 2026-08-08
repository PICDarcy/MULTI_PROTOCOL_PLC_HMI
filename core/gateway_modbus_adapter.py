"""Canonical ValueBus 到唯讀 Modbus TCP Server 的輸出 Adapter。"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .data_model import (
    GatewayModel,
    PointValue,
    allocate_modbus_output_addresses,
    normalize_data_type,
)
from .modbus_codec import encode_modbus_value


@dataclass(frozen=True, slots=True)
class _OutputBinding:
    tag_id: str
    area: str
    address: int
    data_type: str
    byte_order: str
    word_order: str


class GatewayModbusOutputAdapter:
    """依 Canonical Tag mapping 將 Good scalar 值發布到 Modbus 輸出。"""

    def __init__(self, config_manager, value_bus, server, log_func=None) -> None:
        self.config_manager = config_manager
        self.value_bus = value_bus
        self.server = server
        self.log_func = log_func
        self._lock = threading.RLock()
        self._bindings: dict[str, _OutputBinding] = {}
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
                allocated_tags = allocate_modbus_output_addresses(model.tags)
                if allocated_tags == model.tags:
                    return model
                return GatewayModel(
                    connections=model.connections,
                    devices=model.devices,
                    tags=allocated_tags,
                )
        section_getter = getattr(self.config_manager, "get_section", None)
        value = (
            section_getter("gateway_model", {})
            if callable(section_getter)
            else {}
        )
        model = GatewayModel.from_dict(value)
        return GatewayModel(
            connections=model.connections,
            devices=model.devices,
            tags=allocate_modbus_output_addresses(model.tags),
        )

    def reload_mappings(self) -> int:
        bindings: dict[str, _OutputBinding] = {}
        for tag in self._gateway_model().tags:
            mapping = tag.modbus_tcp_output
            if not tag.enabled or not mapping.enabled:
                continue
            bindings[str(tag.tag_id)] = _OutputBinding(
                tag_id=str(tag.tag_id),
                area=mapping.area,
                address=int(mapping.address),
                data_type=normalize_data_type(tag.data_type),
                byte_order=mapping.byte_order,
                word_order=mapping.word_order,
            )
        with self._lock:
            self._bindings = bindings
        return len(bindings)

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self.reload_mappings()
            self._running = True
            self.value_bus.subscribe(self._consume)
            # Keep callbacks behind the lifecycle lock until the initial snapshot
            # has been replayed.  A publish that races with this snapshot either
            # appears in the snapshot or waits here as a newer callback, so an
            # older replay can never overwrite it.
            for point_value in self.value_bus.get_latest_list():
                self._consume(point_value)

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            self.value_bus.unsubscribe(self._consume)

    def _consume(self, point_value: PointValue) -> None:
        with self._lock:
            if not self._running:
                return
            binding = self._bindings.get(str(point_value.tag_id))
            if binding is None or not str(point_value.quality).startswith("Good"):
                return
            self._publish_binding(binding, point_value)

    def _publish_binding(
        self,
        binding: _OutputBinding,
        point_value: PointValue,
    ) -> None:
        """在 lifecycle lock 內完成一次原子的 mapped output 更新。"""
        if not self._running:
            return
        actual_type = normalize_data_type(point_value.data_type)
        if actual_type != binding.data_type:
            self._log(
                f"Modbus輸出略過Tag「{binding.tag_id}」：來源型別由"
                f"{binding.data_type}變更為{actual_type}",
                "WARNING",
            )
            return
        try:
            if binding.data_type == "Boolean":
                if binding.area != "coil":
                    raise ValueError("Boolean mapping必須使用Coil")
                if not isinstance(point_value.value, bool):
                    raise TypeError("Boolean mapping只接受bool值")
                self.server.set_coils(
                    binding.address,
                    [point_value.value],
                    target=binding.tag_id,
                )
                return
            if binding.area != "holding_register":
                raise ValueError(
                    f"{binding.data_type} mapping必須使用Holding Register"
                )
            registers = encode_modbus_value(
                point_value.value,
                binding.data_type,
                byte_order=binding.byte_order,
                word_order=binding.word_order,
            )
            self.server.set_holding_registers(
                binding.address,
                registers,
                target=binding.tag_id,
            )
        except (TypeError, ValueError) as exc:
            self._log(
                f"Modbus輸出Tag「{binding.tag_id}」更新失敗：{exc}",
                "ERROR",
            )
