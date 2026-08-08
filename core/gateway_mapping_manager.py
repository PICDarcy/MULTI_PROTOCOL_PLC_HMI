"""Canonical Tag 雙輸出映射的交易式管理與重新掃描合併。"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .data_model import (
    CanonicalTag,
    GatewayModel,
    ModbusTcpOutputMapping,
    OpcuaOutputMapping,
    is_modbus_output_scalar_type,
    normalize_data_type,
)


_UNSET = object()
_TAG_NAMESPACE = uuid.UUID("22a59452-d8af-4ddb-8624-cbf34b358fb2")


class GatewayRuntimeReloadError(RuntimeError):
    """設定已保存，但執行中輸出無法重新載入。"""


@dataclass(frozen=True, slots=True)
class GatewayMergeResult:
    """一次重新掃描合併後的穩定結果摘要。"""

    model: GatewayModel
    added_tag_ids: tuple[str, ...] = ()
    updated_tag_ids: tuple[str, ...] = ()
    offline_tag_ids: tuple[str, ...] = ()
    pending_type_change_tag_ids: tuple[str, ...] = ()


class GatewayMappingManager:
    """集中管理可編輯映射，並以完整模型原子保存。"""

    def __init__(
        self,
        config_manager,
        *,
        reload_callback: Callable[[], Any] | None = None,
    ) -> None:
        self.config_manager = config_manager
        self.reload_callback = reload_callback
        self._lock = threading.RLock()

    def get_model(self) -> GatewayModel:
        getter = getattr(self.config_manager, "get_gateway_model", None)
        if not callable(getter):
            raise TypeError("config_manager必須提供get_gateway_model()")
        model = getter()
        if not isinstance(model, GatewayModel):
            raise TypeError("get_gateway_model()必須回傳GatewayModel")
        return model

    def update_tag(
        self,
        tag_id: str,
        *,
        name: Any = _UNSET,
        enabled: Any = _UNSET,
        publish_modbus: Any = _UNSET,
        modbus_address: Any = _UNSET,
        modbus_byte_order: Any = _UNSET,
        modbus_word_order: Any = _UNSET,
        publish_opcua: Any = _UNSET,
        opcua_browse_name: Any = _UNSET,
    ) -> CanonicalTag:
        """修改單一 Tag 的使用者欄位並交易式保存。"""

        target_id = str(tag_id or "").strip()
        if not target_id:
            raise ValueError("tag_id不可為空白")

        with self._lock:
            model = self.get_model()
            try:
                index = next(
                    position
                    for position, item in enumerate(model.tags)
                    if str(item.tag_id) == target_id
                )
            except StopIteration as exc:
                raise KeyError(f"找不到Gateway Tag：{target_id}") from exc

            original = model.tags[index]
            updated_name = original.name
            if name is not _UNSET:
                updated_name = str(name or "").strip()
                if not updated_name:
                    raise ValueError("Tag顯示名稱不可為空白")

            metadata = self._metadata(original)
            desired_enabled = bool(
                metadata.get("desired_enabled", original.enabled)
            )
            if enabled is not _UNSET:
                desired_enabled = self._coerce_bool(enabled, field="enabled")
            source_online = metadata.get("source_online", True) is not False
            mapping_confirmed = (
                metadata.get("mapping_state", "confirmed")
                != "pending_type_change"
            )
            updated_enabled = (
                desired_enabled if source_online and mapping_confirmed else False
            )

            modbus = original.modbus_tcp_output
            modbus_changes: dict[str, Any] = {}
            if publish_modbus is not _UNSET:
                publish_modbus_value = self._coerce_bool(
                    publish_modbus,
                    field="publish_modbus",
                )
                modbus_changes["enabled"] = publish_modbus_value
                if (
                    publish_modbus_value
                    and modbus.address is None
                    and modbus_address is _UNSET
                ):
                    modbus_changes["auto_allocate"] = True
            if modbus_address is not _UNSET:
                if modbus_address is None or str(modbus_address).strip() == "":
                    modbus_changes.update(address=None, auto_allocate=True)
                else:
                    modbus_changes.update(
                        address=self._parse_address(modbus_address),
                        auto_allocate=False,
                    )
            if modbus_byte_order is not _UNSET:
                modbus_changes["byte_order"] = modbus_byte_order
            if modbus_word_order is not _UNSET:
                modbus_changes["word_order"] = modbus_word_order
            if modbus_changes:
                modbus = replace(modbus, **modbus_changes)

            opcua = original.opcua_output
            opcua_changes: dict[str, Any] = {}
            if publish_opcua is not _UNSET:
                publish_opcua_value = self._coerce_bool(
                    publish_opcua,
                    field="publish_opcua",
                )
                opcua_changes["enabled"] = publish_opcua_value
                if publish_opcua_value and not opcua.node_id:
                    opcua_changes["node_id"] = str(original.tag_id)
            if opcua_browse_name is not _UNSET:
                browse_name = str(opcua_browse_name or "").strip()
                opcua_changes["browse_name"] = browse_name or updated_name
            if opcua_changes:
                opcua = replace(opcua, **opcua_changes)

            metadata["desired_enabled"] = desired_enabled
            updated = replace(
                original,
                name=updated_name,
                enabled=updated_enabled,
                modbus_tcp_output=modbus,
                opcua_output=opcua,
                metadata=metadata,
            )
            tags = list(model.tags)
            tags[index] = updated
            candidate = GatewayModel(
                connections=model.connections,
                devices=model.devices,
                tags=tuple(tags),
            )
            persisted = self._save_model(candidate)
            return self._tag_by_id(persisted, target_id)

    def merge_discovered_tags(
        self,
        discoveries: Iterable[Mapping[str, Any]],
        *,
        connection_id: str,
        device_id: str,
    ) -> GatewayMergeResult:
        """依穩定 point_key 合併掃描結果，保留使用者映射與固定位址。"""

        connection_key = str(connection_id or "").strip()
        device_key = str(device_id or "").strip()
        if not connection_key or not device_key:
            raise ValueError("connection_id與device_id不可為空白")

        with self._lock:
            model = self.get_model()
            source_protocol = self._validate_scope(
                model,
                connection_key,
                device_key,
            )
            normalized = self._normalize_discoveries(discoveries)
            for discovery in normalized:
                if discovery["source_protocol"] != source_protocol:
                    raise ValueError(
                        f"掃描來源協定{discovery['source_protocol']}與"
                        f"Connection協定{source_protocol}不一致"
                    )
            by_point_key = {str(tag.point_key): tag for tag in model.tags}
            scoped_existing = {
                str(tag.point_key): tag
                for tag in model.tags
                if str(tag.connection_id) == connection_key
                and str(tag.device_id) == device_key
            }

            replacements: dict[str, CanonicalTag] = {}
            additions: list[CanonicalTag] = []
            added_ids: list[str] = []
            updated_ids: list[str] = []
            offline_ids: list[str] = []
            pending_ids: list[str] = []
            seen_keys: set[str] = set()

            for discovery in normalized:
                point_key = discovery["point_key"]
                seen_keys.add(point_key)
                existing = scoped_existing.get(point_key)
                if existing is None:
                    if point_key in by_point_key:
                        raise ValueError(
                            f"point_key已由其他Connection或Device使用：{point_key}"
                        )
                    added = self._new_tag(
                        discovery,
                        connection_id=connection_key,
                        device_id=device_key,
                        existing_ids={str(item.tag_id) for item in model.tags}
                        | set(added_ids),
                    )
                    additions.append(added)
                    added_ids.append(str(added.tag_id))
                    continue

                observed_type = discovery["data_type"]
                metadata = self._metadata(existing)
                desired_enabled = bool(
                    metadata.get("desired_enabled", existing.enabled)
                )
                metadata.update(
                    {
                        "desired_enabled": desired_enabled,
                        "source_online": True,
                        "source_state": "online",
                        "observed_data_type": observed_type,
                    }
                )

                if observed_type != existing.data_type:
                    metadata["mapping_state"] = "pending_type_change"
                    replacements[point_key] = replace(
                        existing,
                        source_address=discovery["source_address"],
                        quality="BadTypeMismatch",
                        enabled=False,
                        metadata=metadata,
                    )
                    pending_ids.append(str(existing.tag_id))
                    continue

                metadata["mapping_state"] = "confirmed"
                replacements[point_key] = replace(
                    existing,
                    source_address=discovery["source_address"],
                    quality=discovery.get("quality", "Good"),
                    source_timestamp=discovery.get(
                        "source_timestamp",
                        existing.source_timestamp,
                    ),
                    server_timestamp=discovery.get(
                        "server_timestamp",
                        existing.server_timestamp,
                    ),
                    gateway_timestamp=discovery.get(
                        "gateway_timestamp",
                        existing.gateway_timestamp,
                    ),
                    enabled=desired_enabled,
                    metadata=metadata,
                )
                updated_ids.append(str(existing.tag_id))

            for point_key, existing in scoped_existing.items():
                if point_key in seen_keys:
                    continue
                metadata = self._metadata(existing)
                desired_enabled = bool(
                    metadata.get("desired_enabled", existing.enabled)
                )
                metadata.update(
                    {
                        "desired_enabled": desired_enabled,
                        "source_online": False,
                        "source_state": "offline",
                    }
                )
                replacements[point_key] = replace(
                    existing,
                    enabled=False,
                    quality="BadNoCommunication",
                    metadata=metadata,
                )
                offline_ids.append(str(existing.tag_id))

            merged_tags = [
                replacements.get(str(tag.point_key), tag)
                for tag in model.tags
            ]
            merged_tags.extend(additions)
            candidate = GatewayModel(
                connections=model.connections,
                devices=model.devices,
                tags=tuple(merged_tags),
            )
            persisted = self._save_model(candidate)
            return GatewayMergeResult(
                model=persisted,
                added_tag_ids=tuple(sorted(added_ids)),
                updated_tag_ids=tuple(sorted(updated_ids)),
                offline_tag_ids=tuple(sorted(offline_ids)),
                pending_type_change_tag_ids=tuple(sorted(pending_ids)),
            )

    def _save_model(self, model: GatewayModel) -> GatewayModel:
        getter = getattr(self.config_manager, "get_config", None)
        saver = getattr(self.config_manager, "save_config", None)
        if not callable(getter) or not callable(saver):
            raise TypeError(
                "config_manager必須提供get_config()與save_config()"
            )
        candidate = getter()
        if not isinstance(candidate, Mapping):
            raise TypeError("get_config()必須回傳Mapping")
        complete = dict(candidate)
        complete["gateway_model"] = model.to_dict()
        saver(complete)
        persisted = self.get_model()
        if callable(self.reload_callback):
            try:
                self.reload_callback()
            except Exception as exc:
                raise GatewayRuntimeReloadError(
                    "設定已保存，但Gateway輸出重新載入失敗；"
                    "請檢查輸出連接埠與日誌後重新啟動。"
                ) from exc
        return persisted

    @staticmethod
    def _metadata(tag: CanonicalTag) -> dict[str, Any]:
        return dict(tag.to_dict().get("metadata", {}))

    @staticmethod
    def _tag_by_id(model: GatewayModel, tag_id: str) -> CanonicalTag:
        try:
            return next(
                tag for tag in model.tags if str(tag.tag_id) == str(tag_id)
            )
        except StopIteration as exc:
            raise KeyError(f"找不到Gateway Tag：{tag_id}") from exc

    @staticmethod
    def _validate_scope(
        model: GatewayModel,
        connection_id: str,
        device_id: str,
    ) -> str:
        try:
            connection = next(
                item
                for item in model.connections
                if str(item.connection_id) == connection_id
            )
        except StopIteration as exc:
            raise ValueError(f"Connection不存在：{connection_id}") from exc
        try:
            device = next(
                item
                for item in model.devices
                if str(item.device_id) == device_id
            )
        except StopIteration as exc:
            raise ValueError(f"Device不存在：{device_id}") from exc
        if str(device.connection_id) != connection_id:
            raise ValueError("Device不屬於指定Connection")
        return connection.protocol

    @staticmethod
    def _coerce_bool(value: Any, *, field: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text == "true":
                return True
            if text == "false":
                return False
        raise ValueError(f"{field}只允許布林值true或false")

    @staticmethod
    def _parse_address(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("modbus_address必須是0至65535的整數")
        try:
            address = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("modbus_address必須是0至65535的整數") from exc
        if not 0 <= address <= 65535:
            raise ValueError("modbus_address必須介於0到65535")
        return address

    @staticmethod
    def _normalize_discoveries(
        discoveries: Iterable[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], ...]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(discoveries, start=1):
            if not isinstance(raw, Mapping):
                raise TypeError(f"第{index}筆掃描結果必須是Mapping")
            point_key = str(raw.get("point_key", "") or "").strip()
            if not point_key:
                raise ValueError(f"第{index}筆掃描結果缺少point_key")
            if point_key in seen:
                raise ValueError(f"掃描結果point_key重複：{point_key}")
            seen.add(point_key)
            protocol = str(raw.get("source_protocol", "") or "").strip().upper()
            if not protocol:
                raise ValueError(f"第{index}筆掃描結果缺少source_protocol")
            source_address = str(raw.get("source_address", "") or "").strip()
            if not source_address:
                raise ValueError(f"第{index}筆掃描結果缺少source_address")
            data_type = normalize_data_type(raw.get("data_type", "Auto"))
            name = str(raw.get("name", point_key) or point_key).strip()
            item = dict(raw)
            item.update(
                {
                    "point_key": point_key,
                    "name": name,
                    "source_protocol": protocol,
                    "source_address": source_address,
                    "data_type": data_type,
                }
            )
            normalized.append(item)
        return tuple(normalized)

    @staticmethod
    def _stable_tag_id(point_key: str) -> str:
        return f"tag-{uuid.uuid5(_TAG_NAMESPACE, point_key).hex}"

    def _new_tag(
        self,
        discovery: Mapping[str, Any],
        *,
        connection_id: str,
        device_id: str,
        existing_ids: set[str],
    ) -> CanonicalTag:
        point_key = str(discovery["point_key"])
        requested_id = str(discovery.get("tag_id", "") or "").strip()
        tag_id = requested_id or self._stable_tag_id(point_key)
        if tag_id in existing_ids:
            raise ValueError(f"新掃描Tag ID重複：{tag_id}")
        data_type = str(discovery["data_type"])
        supported = is_modbus_output_scalar_type(data_type)
        modbus = ModbusTcpOutputMapping(
            enabled=supported,
            address=None,
            auto_allocate=supported,
        )
        opcua = OpcuaOutputMapping(
            enabled=True,
            node_id=tag_id,
            browse_name=str(discovery["name"]),
        )
        raw_metadata = discovery.get("metadata", {}) or {}
        if not isinstance(raw_metadata, Mapping):
            raise TypeError("掃描Tag metadata必須是Mapping")
        metadata = dict(raw_metadata)
        metadata.update(
            {
                "desired_enabled": True,
                "source_online": True,
                "source_state": "online",
                "mapping_state": "confirmed",
                "observed_data_type": data_type,
            }
        )
        return CanonicalTag(
            tag_id=tag_id,
            point_key=point_key,
            connection_id=connection_id,
            device_id=device_id,
            name=str(discovery["name"]),
            source_protocol=str(discovery["source_protocol"]),
            source_address=str(discovery["source_address"]),
            data_type=data_type,
            quality=str(discovery.get("quality", "Good") or "Good"),
            source_timestamp=discovery.get("source_timestamp"),
            server_timestamp=discovery.get("server_timestamp"),
            gateway_timestamp=discovery.get("gateway_timestamp"),
            enabled=True,
            modbus_tcp_output=modbus,
            opcua_output=opcua,
            metadata=metadata,
        )
