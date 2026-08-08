"""Canonical Tag 雙輸出映射的編輯、保存與重新掃描合併。"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import replace

from .data_model import (
    CanonicalTag,
    GatewayModel,
    ModbusTcpOutputMapping,
    OpcuaOutputMapping,
    normalize_data_type,
)


_UNSET = object()


class GatewayMappingManager:
    """以穩定來源識別管理可長期保存的 Canonical Tag 映射。"""

    def __init__(self, config_manager) -> None:
        self.config_manager = config_manager
        self._lock = threading.RLock()

    def get_model(self) -> GatewayModel:
        model = self.config_manager.get_gateway_model()
        if not isinstance(model, GatewayModel):
            raise TypeError("config_manager必須回傳GatewayModel")
        return model

    def get_tag(self, tag_id: str) -> CanonicalTag:
        tag_id = str(tag_id or "").strip()
        for tag in self.get_model().tags:
            if str(tag.tag_id) == tag_id:
                return tag
        raise KeyError(f"找不到Canonical Tag：{tag_id}")

    def update_tag(
        self,
        tag_id: str,
        *,
        name: str | object = _UNSET,
        enabled: bool | object = _UNSET,
        publish_modbus: bool | object = _UNSET,
        publish_opcua: bool | object = _UNSET,
        modbus_area: str | object = _UNSET,
        modbus_address: int | None | object = _UNSET,
        byte_order: str | object = _UNSET,
        word_order: str | object = _UNSET,
        opcua_browse_name: str | object = _UNSET,
    ) -> CanonicalTag:
        """修改一個 Tag 並以單一原子設定替換保存。"""
        with self._lock:
            model = self.get_model()
            index = self._tag_index(model.tags, tag_id)
            current = model.tags[index]

            modbus = self._updated_modbus_mapping(
                current.modbus_tcp_output,
                publish=publish_modbus,
                area=modbus_area,
                address=modbus_address,
                byte_order=byte_order,
                word_order=word_order,
            )
            opcua = self._updated_opcua_mapping(
                current,
                publish=publish_opcua,
                browse_name=opcua_browse_name,
            )
            updated = replace(
                current,
                name=current.name if name is _UNSET else str(name),
                enabled=(
                    current.enabled if enabled is _UNSET else bool(enabled)
                ),
                modbus_tcp_output=modbus,
                opcua_output=opcua,
            )
            tags = list(model.tags)
            tags[index] = updated
            saved = self._persist(
                GatewayModel(
                    connections=model.connections,
                    devices=model.devices,
                    tags=tuple(tags),
                )
            )
            return saved.tags[index]

    def merge_discovery(self, discovered: GatewayModel) -> GatewayModel:
        """以 point_key 合併掃描結果，保留既有 ID 與使用者映射。

        `discovered.devices` 定義本次完整掃描範圍；只有該範圍內未再出現
        的 Tag 會標記離線，其他 Device 的 Tag 不受局部掃描影響。
        """
        if not isinstance(discovered, GatewayModel):
            raise TypeError("discovered必須是GatewayModel")

        with self._lock:
            current = self.get_model()
            discovered_by_key = {
                str(tag.point_key): tag for tag in discovered.tags
            }
            scanned_device_ids = {
                str(device.device_id) for device in discovered.devices
            }
            merged_tags: list[CanonicalTag] = []
            known_tag_ids = {str(tag.tag_id) for tag in current.tags}

            for existing in current.tags:
                observed = discovered_by_key.pop(
                    str(existing.point_key),
                    None,
                )
                if observed is None:
                    merged_tags.append(
                        replace(existing, source_online=False)
                        if str(existing.device_id) in scanned_device_ids
                        else existing
                    )
                    continue

                observed_type = normalize_data_type(observed.data_type)
                if observed_type in {"Auto", "Unknown"}:
                    pending_type = existing.pending_source_data_type
                else:
                    pending_type = (
                        ""
                        if observed_type == existing.data_type
                        else observed_type
                    )
                merged_tags.append(
                    replace(
                        existing,
                        source_address=observed.source_address,
                        quality=observed.quality,
                        source_timestamp=observed.source_timestamp,
                        server_timestamp=observed.server_timestamp,
                        gateway_timestamp=observed.gateway_timestamp,
                        source_online=True,
                        pending_source_data_type=pending_type,
                    )
                )

            for observed in discovered_by_key.values():
                if str(observed.tag_id) in known_tag_ids:
                    raise ValueError(
                        f"新來源Tag「{observed.point_key}」的tag_id"
                        f"與既有Tag「{observed.tag_id}」衝突"
                    )
                known_tag_ids.add(str(observed.tag_id))
                opcua = observed.opcua_output
                if opcua.enabled and opcua.node_id != str(observed.tag_id):
                    opcua = replace(opcua, node_id=str(observed.tag_id))
                merged_tags.append(
                    replace(
                        observed,
                        source_online=True,
                        pending_source_data_type="",
                        opcua_output=opcua,
                    )
                )

            connections = self._append_new_by_id(
                current.connections,
                discovered.connections,
                "connection_id",
            )
            devices = self._append_new_by_id(
                current.devices,
                discovered.devices,
                "device_id",
            )
            return self._persist(
                GatewayModel(
                    connections=connections,
                    devices=devices,
                    tags=tuple(merged_tags),
                )
            )

    def confirm_pending_data_type(self, tag_id: str) -> CanonicalTag:
        """明確採用偵測到的型別；衝突時整個設定保持原狀。"""
        with self._lock:
            model = self.get_model()
            index = self._tag_index(model.tags, tag_id)
            current = model.tags[index]
            target_type = current.pending_source_data_type
            if not target_type:
                raise ValueError(f"Tag「{current.tag_id}」沒有待確認的來源型別")

            required_area = (
                "coil" if target_type == "Boolean" else "holding_register"
            )
            modbus = replace(
                current.modbus_tcp_output,
                area=required_area,
            )
            updated = replace(
                current,
                data_type=target_type,
                pending_source_data_type="",
                modbus_tcp_output=modbus,
            )
            tags = list(model.tags)
            tags[index] = updated
            saved = self._persist(
                GatewayModel(
                    connections=model.connections,
                    devices=model.devices,
                    tags=tuple(tags),
                )
            )
            return saved.tags[index]

    def _persist(self, model: GatewayModel) -> GatewayModel:
        config = self.config_manager.get_config()
        config["gateway_model"] = model.to_dict()
        self.config_manager.save_config(config)
        return self.get_model()

    @staticmethod
    def _tag_index(tags: Iterable[CanonicalTag], tag_id: str) -> int:
        wanted = str(tag_id or "").strip()
        for index, tag in enumerate(tags):
            if str(tag.tag_id) == wanted:
                return index
        raise KeyError(f"找不到Canonical Tag：{wanted}")

    @staticmethod
    def _append_new_by_id(current, discovered, field_name: str):
        result = list(current)
        known = {str(getattr(item, field_name)) for item in result}
        for item in discovered:
            identifier = str(getattr(item, field_name))
            if identifier not in known:
                result.append(item)
                known.add(identifier)
        return tuple(result)

    @staticmethod
    def _updated_modbus_mapping(
        current: ModbusTcpOutputMapping,
        *,
        publish: bool | object,
        area: str | object,
        address: int | None | object,
        byte_order: str | object,
        word_order: str | object,
    ) -> ModbusTcpOutputMapping:
        enabled = current.enabled if publish is _UNSET else bool(publish)
        resolved_address = current.address if address is _UNSET else address
        auto_allocate = current.auto_allocate
        if address is not _UNSET:
            auto_allocate = resolved_address is None and enabled
        elif enabled and resolved_address is None:
            auto_allocate = True
        return replace(
            current,
            enabled=enabled,
            area=current.area if area is _UNSET else str(area),
            address=resolved_address,
            auto_allocate=auto_allocate,
            byte_order=(
                current.byte_order if byte_order is _UNSET else str(byte_order)
            ),
            word_order=(
                current.word_order if word_order is _UNSET else str(word_order)
            ),
        )

    @staticmethod
    def _updated_opcua_mapping(
        tag: CanonicalTag,
        *,
        publish: bool | object,
        browse_name: str | object,
    ) -> OpcuaOutputMapping:
        current = tag.opcua_output
        enabled = current.enabled if publish is _UNSET else bool(publish)
        node_id = current.node_id
        if enabled and not node_id:
            node_id = str(tag.tag_id)
        return replace(
            current,
            enabled=enabled,
            node_id=node_id,
            browse_name=(
                current.browse_name
                if browse_name is _UNSET
                else str(browse_name)
            ),
        )
