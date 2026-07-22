"""跨協定點位資料模型與唯一鍵工具。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import quote

SUPPORTED_PROTOCOLS = frozenset({"MODBUS_RTU", "OPCUA"})


def _key_part(value: Any) -> str:
    """將唯一鍵片段轉成不會與分隔符衝突的文字。"""
    return quote(str(value), safe="")


def make_opcua_point_key(server_name: str, node_id: str) -> str:
    """建立包含Server名稱的OPC UA唯一點位鍵。"""
    return f"OPCUA::{_key_part(server_name)}::{_key_part(node_id)}"


def make_modbus_point_key(
    source_name: str,
    station_id: int,
    point_type: str,
    address: int,
    point_name: str = "",
) -> str:
    """建立包含通訊來源、站號及位址的Modbus RTU唯一點位鍵。"""
    return "::".join(
        (
            "MODBUS_RTU",
            _key_part(source_name),
            str(int(station_id)),
            _key_part(point_type),
            str(int(address)),
            _key_part(point_name),
        )
    )


@dataclass(slots=True)
class PointValue:
    """HMI內部統一使用的即時點位值。"""

    point_key: str
    protocol: str
    source_name: str
    device_name: str
    point_name: str
    address_text: str
    value: Any = None
    value_text: str = ""
    value_number: float | None = None
    status_text: str = "Good"
    timestamp: datetime = field(default_factory=datetime.now)
    writable: bool = False
    data_type: str = "Auto"
    raw_config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.point_key = str(self.point_key).strip()
        if not self.point_key:
            raise ValueError("PointValue.point_key不可為空白")

        self.protocol = str(self.protocol).strip().upper()
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise ValueError(
                f"不支援的protocol：{self.protocol}，只允許MODBUS_RTU或OPCUA"
            )

        self.source_name = str(self.source_name or "")
        self.device_name = str(self.device_name or "")
        self.point_name = str(self.point_name or "")
        self.address_text = str(self.address_text or "")
        self.value_text = str(self.value_text if self.value_text is not None else "")
        self.status_text = str(self.status_text or "")
        self.data_type = str(self.data_type or "Auto")
        self.writable = bool(self.writable)

        if not isinstance(self.timestamp, datetime):
            raise TypeError("PointValue.timestamp必須是datetime")
        if self.value_number is not None:
            self.value_number = float(self.value_number)
        if isinstance(self.raw_config, Mapping):
            self.raw_config = dict(self.raw_config)
        else:
            raise TypeError("PointValue.raw_config必須是Mapping")

    def to_dict(self) -> dict[str, Any]:
        """轉成可供UI與資料庫使用的字典。"""
        return {
            "point_key": self.point_key,
            "protocol": self.protocol,
            "source_name": self.source_name,
            "device_name": self.device_name,
            "point_name": self.point_name,
            "address_text": self.address_text,
            "value": self.value,
            "value_text": self.value_text,
            "value_number": self.value_number,
            "status_text": self.status_text,
            "timestamp": self.timestamp,
            "writable": self.writable,
            "data_type": self.data_type,
            "raw_config": dict(self.raw_config),
        }
