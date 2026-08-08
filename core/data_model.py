"""跨協定點位資料模型、資料轉換與唯一鍵工具。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import quote, unquote

from .modbus_codec import (
    is_modbus_output_scalar_type,
    modbus_output_register_count,
    normalize_modbus_order,
)


SUPPORTED_PROTOCOLS = frozenset({"MODBUS_RTU", "MODBUS_TCP", "OPCUA"})


_DATA_TYPE_ALIASES = {
    "AUTO": "Auto",
    "BOOL": "Boolean",
    "BOOLEAN": "Boolean",
    "BIT": "Boolean",
    "BYTE": "Byte",
    "UINT8": "Byte",
    "UNSIGNED8": "Byte",
    "SBYTE": "SByte",
    "INT8": "SByte",
    "SIGNED8": "SByte",
    "WORD": "UInt16",
    "UINT16": "UInt16",
    "UNSIGNED16": "UInt16",
    "INT": "Int16",
    "INT16": "Int16",
    "SIGNED16": "Int16",
    "DWORD": "UInt32",
    "UINT32": "UInt32",
    "UNSIGNED32": "UInt32",
    "DINT": "Int32",
    "INT32": "Int32",
    "SIGNED32": "Int32",
    "LWORD": "UInt64",
    "UINT64": "UInt64",
    "UNSIGNED64": "UInt64",
    "LINT": "Int64",
    "INT64": "Int64",
    "SIGNED64": "Int64",
    "REAL": "Float",
    "FLOAT": "Float",
    "FLOAT32": "Float",
    "LREAL": "Double",
    "DOUBLE": "Double",
    "FLOAT64": "Double",
    "STRING": "String",
    "STR": "String",
    "TEXT": "String",
    "CHAR": "String",
    "DATETIME": "DateTime",
    "DATE_TIME": "DateTime",
    "DATE": "DateTime",
    "BYTESTRING": "ByteString",
    "BYTE_STRING": "ByteString",
    "BYTES": "ByteString",
}

_INTEGER_LIMITS = {
    "Byte": (0, 255),
    "SByte": (-128, 127),
    "UInt16": (0, 65535),
    "Int16": (-32768, 32767),
    "UInt32": (0, 4294967295),
    "Int32": (-2147483648, 2147483647),
    "UInt64": (0, 18446744073709551615),
    "Int64": (-9223372036854775808, 9223372036854775807),
}


def now_text() -> str:
    """回傳適合UI及資料庫顯示的目前時間文字。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def value_to_text(value: Any) -> str:
    """將任意點位值轉成穩定且可顯示的文字。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex(" ").upper()
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def value_to_number(value: Any) -> float | None:
    """嘗試將值轉成浮點數，無法轉換時回傳None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float, Decimal)):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    text = str(value).strip()
    if not text:
        return None

    lowered = text.lower()
    if lowered in {"true", "on", "yes", "y"}:
        return 1.0
    if lowered in {"false", "off", "no", "n"}:
        return 0.0

    try:
        if lowered.startswith(("0x", "+0x", "-0x")):
            return float(int(text, 16))
        if lowered.startswith(("0b", "+0b", "-0b")):
            return float(int(text, 2))
        if lowered.startswith(("0o", "+0o", "-0o")):
            return float(int(text, 8))
        number = float(Decimal(text))
        return number if math.isfinite(number) else None
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None


def normalize_data_type(data_type: Any) -> str:
    """將常見PLC及OPC UA資料型別別名正規化。"""
    text = str(data_type or "Auto").strip()
    if not text:
        return "Auto"

    if text.lower().startswith("varianttype."):
        text = text.split(".", 1)[1]

    is_array = text.endswith("[]")
    base_text = text[:-2].strip() if is_array else text
    lookup_key = base_text.replace("-", "_").replace(" ", "_").upper()
    normalized = _DATA_TYPE_ALIASES.get(lookup_key, base_text)
    return f"{normalized}[]" if is_array else normalized


def convert_text_to_value(value_text: Any, data_type: Any) -> Any:
    """依資料型別將UI輸入文字轉成Python值。

    此函式不匯入asyncua；OPC UA VariantType由呼叫端依回傳型別文字建立。
    """
    normalized_type = normalize_data_type(data_type)
    original_text = "" if value_text is None else str(value_text)
    stripped_text = original_text.strip()

    if normalized_type.endswith("[]"):
        item_type = normalized_type[:-2]
        if not stripped_text:
            return []
        try:
            parsed = json.loads(stripped_text)
        except json.JSONDecodeError:
            parsed = [item.strip() for item in stripped_text.split(",")]
        if not isinstance(parsed, list):
            raise ValueError(f"{normalized_type}必須輸入JSON陣列或逗號分隔值")
        return [convert_text_to_value(item, item_type) for item in parsed]

    if normalized_type == "Auto":
        if value_text is None:
            return None
        lowered = stripped_text.lower()
        if lowered in {"true", "on", "yes"}:
            return True
        if lowered in {"false", "off", "no"}:
            return False
        if lowered in {"none", "null"}:
            return None
        if not stripped_text:
            return ""
        try:
            if lowered.startswith(("0x", "+0x", "-0x")):
                return int(stripped_text, 16)
            if lowered.startswith(("0b", "+0b", "-0b")):
                return int(stripped_text, 2)
            if lowered.startswith(("0o", "+0o", "-0o")):
                return int(stripped_text, 8)
            return int(stripped_text, 10)
        except ValueError:
            try:
                return float(stripped_text)
            except ValueError:
                if stripped_text.startswith(("[", "{")):
                    try:
                        return json.loads(stripped_text)
                    except json.JSONDecodeError:
                        pass
                return original_text

    if normalized_type == "Boolean":
        lowered = stripped_text.lower()
        if lowered in {"1", "true", "on", "yes", "y"}:
            return True
        if lowered in {"0", "false", "off", "no", "n"}:
            return False
        raise ValueError("Boolean只接受true/false、on/off、yes/no或1/0")

    if normalized_type in _INTEGER_LIMITS:
        try:
            integer_value = int(stripped_text, 0)
        except ValueError:
            try:
                decimal_value = Decimal(stripped_text)
            except InvalidOperation as exc:
                raise ValueError(f"{normalized_type}必須是整數") from exc
            if decimal_value != decimal_value.to_integral_value():
                raise ValueError(f"{normalized_type}不可包含小數")
            integer_value = int(decimal_value)

        minimum, maximum = _INTEGER_LIMITS[normalized_type]
        if not minimum <= integer_value <= maximum:
            raise ValueError(
                f"{normalized_type}超出範圍：{minimum}至{maximum}"
            )
        return integer_value

    if normalized_type in {"Float", "Double"}:
        try:
            return float(stripped_text)
        except ValueError as exc:
            raise ValueError(f"{normalized_type}必須是數值") from exc

    if normalized_type == "String":
        return original_text

    if normalized_type == "DateTime":
        if not stripped_text:
            raise ValueError("DateTime不可為空")
        normalized_datetime = stripped_text.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized_datetime)
        except ValueError as exc:
            raise ValueError("DateTime請使用ISO 8601格式") from exc

    if normalized_type == "ByteString":
        if not stripped_text:
            return b""
        lowered = stripped_text.lower()
        if lowered.startswith("hex:"):
            hex_text = stripped_text[4:].replace(" ", "").replace("-", "")
            try:
                return bytes.fromhex(hex_text)
            except ValueError as exc:
                raise ValueError("ByteString的hex格式錯誤") from exc
        if lowered.startswith("0x"):
            hex_text = stripped_text[2:].replace(" ", "").replace("-", "")
            try:
                return bytes.fromhex(hex_text)
            except ValueError as exc:
                raise ValueError("ByteString的0x格式錯誤") from exc
        return original_text.encode("utf-8")

    # 未知型別不擅自轉換，保留原始文字供上層處理。
    return original_text


def _key_part(value: Any) -> str:
    """將唯一鍵片段編碼，避免與分隔符衝突。"""
    return quote(str(value), safe="")


def make_modbus_point_key(
    device_name: str,
    station_id: int,
    point_type: str,
    address: int,
    point_name: str = "",
) -> str:
    """建立Modbus RTU點位唯一鍵。"""
    return "::".join(
        (
            "MODBUS_RTU",
            _key_part(device_name),
            str(int(station_id)),
            _key_part(str(point_type).upper()),
            str(int(address)),
            _key_part(point_name),
        )
    )


def make_modbus_tcp_point_key(
    endpoint: str,
    station_id: int,
    point_type: str,
    address: int,
    point_name: str = "",
) -> str:
    """建立Modbus TCP點位唯一鍵。"""
    return "::".join(
        (
            "MODBUS_TCP",
            _key_part(endpoint),
            str(int(station_id)),
            _key_part(str(point_type).upper()),
            str(int(address)),
            _key_part(point_name),
        )
    )


def make_opcua_point_key(server_name: str, node_id: str) -> str:
    """建立包含Server名稱及NodeId的OPC UA點位唯一鍵。"""
    return f"OPCUA::{_key_part(server_name)}::{_key_part(node_id)}"


def parse_opcua_point_key(point_key: str) -> tuple[str, str]:
    """解析OPC UA點位鍵並回傳(server_name,node_id)。"""
    text = str(point_key or "").strip()
    if text.startswith("OPCUA::"):
        parts = text.split("::", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            return unquote(parts[1]), unquote(parts[2])
    elif text.startswith("OPCUA|"):
        parts = text.split("|", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            return unquote(parts[1]), unquote(parts[2])
    raise ValueError("無效的OPC UA point_key")


def get_opcua_variant_type_name(data_type: Any) -> str:
    """回傳OPC UA VariantType名稱文字，不匯入asyncua。"""
    normalized = normalize_data_type(data_type)
    if normalized.endswith("[]"):
        normalized = normalized[:-2]
    return normalized


class _StableIdentifier(str):
    """避免在 canonical 模型內混用不同種類的裸字串 ID。"""

    field_name = "identifier"

    def __new__(cls, value: Any):
        identifier = str(value or "").strip()
        if not identifier:
            raise ValueError(f"{cls.field_name}不可為空白")
        return super().__new__(cls, identifier)


class ConnectionId(_StableIdentifier):
    field_name = "connection_id"


class DeviceId(_StableIdentifier):
    field_name = "device_id"


class TagId(_StableIdentifier):
    field_name = "tag_id"


class PointKey(_StableIdentifier):
    field_name = "point_key"


def _freeze_config(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_config(child) for key, child in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_config(child) for child in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_config(child) for child in value)
    return value


def _thaw_config(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_config(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_config(child) for child in value]
    if isinstance(value, frozenset):
        return [_thaw_config(child) for child in value]
    return value


def _timestamp_to_json(value: datetime | str | None) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def _timestamp_from_json(value: Any) -> datetime | str | None:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text


@dataclass(frozen=True, slots=True)
class Connection:
    """Canonical 來源連線；`connection_id` 不受顯示名稱變更影響。"""

    connection_id: ConnectionId
    name: str
    protocol: str
    enabled: bool = True
    settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "connection_id",
            ConnectionId(self.connection_id),
        )
        protocol = str(self.protocol).strip().upper()
        if protocol not in SUPPORTED_PROTOCOLS:
            raise ValueError(f"不支援的來源協定：{protocol}")
        object.__setattr__(self, "protocol", protocol)
        object.__setattr__(self, "name", str(self.name or ""))
        object.__setattr__(self, "enabled", bool(self.enabled))
        if not isinstance(self.settings, Mapping):
            raise TypeError("Connection.settings必須是Mapping")
        object.__setattr__(self, "settings", _freeze_config(self.settings))

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "name": self.name,
            "protocol": self.protocol,
            "enabled": self.enabled,
            "settings": _thaw_config(self.settings),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Connection":
        return cls(
            connection_id=value.get("connection_id", ""),
            name=value.get("name", ""),
            protocol=value.get("protocol", ""),
            enabled=value.get("enabled", True),
            settings=value.get("settings", {}),
        )


@dataclass(frozen=True, slots=True)
class Device:
    """Canonical 裝置；透過穩定 ID 隸屬於一個 Connection。"""

    device_id: DeviceId
    connection_id: ConnectionId
    name: str
    enabled: bool = True
    settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "device_id",
            DeviceId(self.device_id),
        )
        object.__setattr__(
            self,
            "connection_id",
            ConnectionId(self.connection_id),
        )
        object.__setattr__(self, "name", str(self.name or ""))
        object.__setattr__(self, "enabled", bool(self.enabled))
        if not isinstance(self.settings, Mapping):
            raise TypeError("Device.settings必須是Mapping")
        object.__setattr__(self, "settings", _freeze_config(self.settings))

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "connection_id": self.connection_id,
            "name": self.name,
            "enabled": self.enabled,
            "settings": _thaw_config(self.settings),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Device":
        return cls(
            device_id=value.get("device_id", ""),
            connection_id=value.get("connection_id", ""),
            name=value.get("name", ""),
            enabled=value.get("enabled", True),
            settings=value.get("settings", {}),
        )


@dataclass(frozen=True, slots=True)
class ModbusTcpOutputMapping:
    """單一 Canonical Tag 的 Modbus TCP 輸出映射。"""

    enabled: bool = False
    area: str | None = None
    address: int | None = None
    byte_order: str = "big"
    word_order: str = "big"
    auto_allocate: bool = False
    supported: bool = True
    unsupported_reason: str = ""
    _area_was_default: bool = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        area_was_default = self.area is None or not str(self.area).strip()
        area = str(self.area or "holding_register").strip().lower()
        if area not in {"coil", "holding_register"}:
            raise ValueError("Modbus TCP輸出area只允許coil或holding_register")
        object.__setattr__(self, "area", area)
        object.__setattr__(self, "_area_was_default", area_was_default)
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "auto_allocate", bool(self.auto_allocate))
        object.__setattr__(self, "supported", bool(self.supported))
        object.__setattr__(
            self,
            "unsupported_reason",
            str(self.unsupported_reason or "").strip(),
        )
        object.__setattr__(
            self,
            "byte_order",
            normalize_modbus_order(self.byte_order, kind="byte"),
        )
        object.__setattr__(
            self,
            "word_order",
            normalize_modbus_order(self.word_order, kind="word"),
        )
        if self.enabled and self.address is None and not self.auto_allocate:
            raise ValueError(
                "啟用Modbus TCP輸出時address不可為空，"
                "除非啟用auto_allocate"
            )
        if self.address is not None:
            address = int(self.address)
            if not 0 <= address <= 65535:
                raise ValueError("Modbus TCP輸出address必須介於0到65535")
            object.__setattr__(self, "address", address)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "area": self.area,
            "address": self.address,
            "byte_order": self.byte_order,
            "word_order": self.word_order,
            "auto_allocate": self.auto_allocate,
            "supported": self.supported,
            "unsupported_reason": self.unsupported_reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "ModbusTcpOutputMapping":
        data = value if isinstance(value, Mapping) else {}
        return cls(
            enabled=data.get("enabled", False),
            area=data.get("area"),
            address=data.get("address"),
            byte_order=data.get("byte_order", "big"),
            word_order=data.get("word_order", "big"),
            auto_allocate=data.get("auto_allocate", False),
            supported=data.get("supported", True),
            unsupported_reason=data.get("unsupported_reason", ""),
        )


@dataclass(frozen=True, slots=True)
class OpcuaOutputMapping:
    """單一 Canonical Tag 的 OPC UA 輸出映射。"""

    enabled: bool = False
    node_id: str = ""
    browse_name: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "node_id", str(self.node_id or "").strip())
        object.__setattr__(
            self,
            "browse_name",
            str(self.browse_name or "").strip(),
        )
        if self.enabled and not self.node_id:
            raise ValueError("啟用OPC UA輸出時node_id不可為空")

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "node_id": self.node_id,
            "browse_name": self.browse_name,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "OpcuaOutputMapping":
        data = value if isinstance(value, Mapping) else {}
        return cls(
            enabled=data.get("enabled", False),
            node_id=data.get("node_id", ""),
            browse_name=data.get("browse_name", ""),
        )


@dataclass(frozen=True, slots=True)
class CanonicalTag:
    """來源與雙輸出映射皆明確的 Gateway Tag 契約。"""

    tag_id: TagId
    point_key: PointKey
    connection_id: ConnectionId
    device_id: DeviceId
    name: str
    source_protocol: str
    source_address: str
    data_type: str = "Auto"
    quality: str = "Good"
    source_timestamp: datetime | str | None = None
    server_timestamp: datetime | str | None = None
    enabled: bool = True
    modbus_tcp_output: ModbusTcpOutputMapping = field(
        default_factory=ModbusTcpOutputMapping
    )
    opcua_output: OpcuaOutputMapping = field(default_factory=OpcuaOutputMapping)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    gateway_timestamp: datetime | str | None = None
    source_online: bool = True
    pending_source_data_type: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "tag_id", TagId(self.tag_id))
        object.__setattr__(self, "point_key", PointKey(self.point_key))
        object.__setattr__(
            self,
            "connection_id",
            ConnectionId(self.connection_id),
        )
        object.__setattr__(self, "device_id", DeviceId(self.device_id))
        protocol = str(self.source_protocol).strip().upper()
        if protocol not in SUPPORTED_PROTOCOLS:
            raise ValueError(f"不支援的來源協定：{protocol}")
        object.__setattr__(self, "source_protocol", protocol)
        object.__setattr__(self, "name", str(self.name or ""))
        object.__setattr__(self, "source_address", str(self.source_address or ""))
        object.__setattr__(self, "data_type", normalize_data_type(self.data_type))
        object.__setattr__(self, "quality", str(self.quality or "Unknown"))
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "source_online", bool(self.source_online))
        pending_source_data_type = str(
            self.pending_source_data_type or ""
        ).strip()
        if pending_source_data_type:
            pending_source_data_type = normalize_data_type(
                pending_source_data_type
            )
            if pending_source_data_type == self.data_type:
                pending_source_data_type = ""
        object.__setattr__(
            self,
            "pending_source_data_type",
            pending_source_data_type,
        )
        if not isinstance(self.modbus_tcp_output, ModbusTcpOutputMapping):
            raise TypeError("modbus_tcp_output型別錯誤")
        mapping = self.modbus_tcp_output
        if not is_modbus_output_scalar_type(self.data_type):
            reason = f"{self.data_type}不支援Modbus TCP自動映射"
            if mapping.enabled:
                raise ValueError(
                    f"{self.data_type}不支援Modbus TCP輸出；"
                    "String、Array、Structure、ByteString與其他可變長度型別"
                    "不得啟用映射"
                )
            mapping = replace(
                mapping,
                enabled=False,
                address=None,
                auto_allocate=False,
                supported=False,
                unsupported_reason=reason,
            )
        else:
            required_area = (
                "coil" if self.data_type == "Boolean" else "holding_register"
            )
            if mapping._area_was_default:
                mapping = replace(mapping, area=required_area)
            elif mapping.area != required_area:
                raise ValueError(
                    f"{self.data_type}不允許映射至{mapping.area}；"
                    f"必須使用{required_area}且不得跨型別轉換"
                )
            mapping = replace(
                mapping,
                supported=True,
                unsupported_reason="",
            )
        object.__setattr__(self, "modbus_tcp_output", mapping)
        if not isinstance(self.opcua_output, OpcuaOutputMapping):
            raise TypeError("opcua_output型別錯誤")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("CanonicalTag.metadata必須是Mapping")
        object.__setattr__(self, "metadata", _freeze_config(self.metadata))

    @property
    def mapping_confirmation_required(self) -> bool:
        """來源型別改變後，映射必須由使用者明確確認。"""
        return bool(self.pending_source_data_type)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag_id": self.tag_id,
            "point_key": self.point_key,
            "connection_id": self.connection_id,
            "device_id": self.device_id,
            "name": self.name,
            "source_protocol": self.source_protocol,
            "source_address": self.source_address,
            "data_type": self.data_type,
            "quality": self.quality,
            "source_timestamp": _timestamp_to_json(self.source_timestamp),
            "server_timestamp": _timestamp_to_json(self.server_timestamp),
            "gateway_timestamp": _timestamp_to_json(self.gateway_timestamp),
            "enabled": self.enabled,
            "source_online": self.source_online,
            "pending_source_data_type": self.pending_source_data_type,
            "mapping_confirmation_required": self.mapping_confirmation_required,
            "modbus_tcp_output": self.modbus_tcp_output.to_dict(),
            "opcua_output": self.opcua_output.to_dict(),
            "metadata": _thaw_config(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CanonicalTag":
        return cls(
            tag_id=value.get("tag_id", ""),
            point_key=value.get("point_key", ""),
            connection_id=value.get("connection_id", ""),
            device_id=value.get("device_id", ""),
            name=value.get("name", ""),
            source_protocol=value.get("source_protocol", ""),
            source_address=value.get("source_address", ""),
            data_type=value.get("data_type", "Auto"),
            quality=value.get("quality", "Good"),
            source_timestamp=_timestamp_from_json(value.get("source_timestamp")),
            server_timestamp=_timestamp_from_json(value.get("server_timestamp")),
            gateway_timestamp=_timestamp_from_json(value.get("gateway_timestamp")),
            enabled=value.get("enabled", True),
            source_online=value.get("source_online", True),
            pending_source_data_type=value.get(
                "pending_source_data_type",
                "",
            ),
            modbus_tcp_output=ModbusTcpOutputMapping.from_dict(
                value.get("modbus_tcp_output")
            ),
            opcua_output=OpcuaOutputMapping.from_dict(value.get("opcua_output")),
            metadata=value.get("metadata", {}),
        )

    def to_point_value(
        self,
        *,
        value: Any,
        source_name: str = "",
        device_name: str = "",
    ) -> "PointValue":
        """以既有 PointValue 介面提供 canonical 值給 UI／資料庫。"""
        timestamp = self.source_timestamp or self.server_timestamp or datetime.now()
        return PointValue(
            point_key=self.point_key,
            protocol=self.source_protocol,
            source_name=source_name,
            device_name=device_name,
            point_name=self.name,
            address_text=self.source_address,
            value=value,
            status_text=self.quality,
            timestamp=timestamp,
            data_type=self.data_type,
            raw_config={
                **_thaw_config(self.metadata),
                "tag_id": self.tag_id,
                "connection_id": self.connection_id,
                "device_id": self.device_id,
                "modbus_tcp_output": self.modbus_tcp_output.to_dict(),
                "opcua_output": self.opcua_output.to_dict(),
            },
            tag_id=self.tag_id,
            connection_id=self.connection_id,
            device_id=self.device_id,
            quality=self.quality,
            source_timestamp=self.source_timestamp,
            server_timestamp=self.server_timestamp,
            gateway_timestamp=self.gateway_timestamp,
        )


def _modbus_output_width(tag: CanonicalTag) -> int:
    if tag.data_type == "Boolean":
        return 1
    return modbus_output_register_count(tag.data_type)


def allocate_modbus_output_addresses(
    tags: tuple[CanonicalTag, ...] | list[CanonicalTag],
    *,
    coil_start: int = 0,
    coil_end: int = 65535,
    register_start: int = 0,
    register_end: int = 65535,
    allocate_auto: bool = True,
) -> tuple[CanonicalTag, ...]:
    """驗證固定映射並為 auto mapping 配置第一段連續空間。

    範圍端點皆為 0-based 且包含端點。已有固定地址即使 Tag 目前停用，
    仍視為保留，避免自動配置破壞既有上位系統地址契約。
    """

    limits = {
        "coil": (int(coil_start), int(coil_end)),
        "holding_register": (int(register_start), int(register_end)),
    }
    for area, (start, end) in limits.items():
        if not 0 <= start <= end <= 65535:
            raise ValueError(
                f"{area}輸出範圍必須介於0到65535且起點不可大於終點"
            )

    result = list(tags)
    occupied: dict[str, list[tuple[int, int, str]]] = {
        "coil": [],
        "holding_register": [],
    }

    def reserve(tag: CanonicalTag, start: int) -> None:
        mapping = tag.modbus_tcp_output
        area = mapping.area
        width = _modbus_output_width(tag)
        end = start + width - 1
        allowed_start, allowed_end = limits[area]
        if start < allowed_start or end > allowed_end:
            raise ValueError(
                f"Tag「{tag.tag_id}」Modbus TCP輸出範圍{start}-{end}"
                f"超出允許範圍{allowed_start}-{allowed_end}"
            )
        for other_start, other_end, other_tag_id in occupied[area]:
            if start <= other_end and other_start <= end:
                raise ValueError(
                    f"Tag「{other_tag_id}」與Tag「{tag.tag_id}」"
                    f"Modbus TCP輸出位址重疊："
                    f"{other_start}-{other_end}與{start}-{end}"
                )
        occupied[area].append((start, end, str(tag.tag_id)))
        occupied[area].sort(key=lambda item: (item[0], item[1], item[2]))

    # 先保留所有明確地址，再為尚未配置的 enabled auto mapping 找空間。
    for tag in result:
        mapping = tag.modbus_tcp_output
        if not mapping.supported or mapping.address is None:
            continue
        reserve(tag, mapping.address)

    if not allocate_auto:
        return tuple(result)

    for index, tag in enumerate(result):
        mapping = tag.modbus_tcp_output
        if (
            not tag.enabled
            or not mapping.enabled
            or not mapping.supported
            or mapping.address is not None
            or not mapping.auto_allocate
        ):
            continue
        area = mapping.area
        width = _modbus_output_width(tag)
        candidate, allowed_end = limits[area]
        while candidate + width - 1 <= allowed_end:
            candidate_end = candidate + width - 1
            conflict = next(
                (
                    (start, end)
                    for start, end, _tag_id in occupied[area]
                    if candidate <= end and start <= candidate_end
                ),
                None,
            )
            if conflict is None:
                break
            candidate = conflict[1] + 1
        else:
            allowed_start, allowed_end = limits[area]
            raise ValueError(
                f"Tag「{tag.tag_id}」找不到{width}個連續空間；"
                f"{area}允許範圍為{allowed_start}-{allowed_end}"
            )

        allocated_mapping = replace(mapping, address=candidate)
        allocated_tag = replace(tag, modbus_tcp_output=allocated_mapping)
        result[index] = allocated_tag
        reserve(allocated_tag, candidate)

    return tuple(result)


@dataclass(frozen=True, slots=True)
class GatewayModel:
    """Connection、Device 與 Tag 的一致性邊界。"""

    connections: tuple[Connection, ...] = ()
    devices: tuple[Device, ...] = ()
    tags: tuple[CanonicalTag, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "connections", tuple(self.connections))
        object.__setattr__(self, "devices", tuple(self.devices))
        object.__setattr__(self, "tags", tuple(self.tags))
        connection_ids = self._unique_ids(
            self.connections,
            "connection_id",
        )
        device_ids = self._unique_ids(self.devices, "device_id")
        self._unique_ids(self.tags, "tag_id")
        self._unique_ids(self.tags, "point_key")
        for device in self.devices:
            if device.connection_id not in connection_ids:
                raise ValueError(
                    f"Device.connection_id不存在：{device.connection_id}"
                )
        for tag in self.tags:
            if tag.connection_id not in connection_ids:
                raise ValueError(
                    f"CanonicalTag.connection_id不存在：{tag.connection_id}"
                )
            if tag.device_id not in device_ids:
                raise ValueError(f"CanonicalTag.device_id不存在：{tag.device_id}")
            device = next(item for item in self.devices if item.device_id == tag.device_id)
            if device.connection_id != tag.connection_id:
                raise ValueError("CanonicalTag的connection_id與Device不一致")
        object.__setattr__(
            self,
            "tags",
            allocate_modbus_output_addresses(
                self.tags,
                allocate_auto=False,
            ),
        )

    @staticmethod
    def _unique_ids(items: tuple[Any, ...], field_name: str) -> set[str]:
        values = [str(getattr(item, field_name)) for item in items]
        if len(values) != len(set(values)):
            raise ValueError(f"{field_name}不可重複")
        return set(values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "connections": [item.to_dict() for item in self.connections],
            "devices": [item.to_dict() for item in self.devices],
            "tags": [item.to_dict() for item in self.tags],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "GatewayModel":
        data = value if isinstance(value, Mapping) else {}
        return cls(
            connections=tuple(
                Connection.from_dict(item)
                for item in data.get("connections", [])
                if isinstance(item, Mapping)
            ),
            devices=tuple(
                Device.from_dict(item)
                for item in data.get("devices", [])
                if isinstance(item, Mapping)
            ),
            tags=tuple(
                CanonicalTag.from_dict(item)
                for item in data.get("tags", [])
                if isinstance(item, Mapping)
            ),
        )


@dataclass(slots=True)
class PointValue:
    """HMI內部統一使用的即時點位資料。"""

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
    timestamp: datetime | str = field(default_factory=datetime.now)
    writable: bool = False
    data_type: str = "Auto"
    raw_config: dict[str, Any] = field(default_factory=dict)
    tag_id: str = ""
    connection_id: str = ""
    device_id: str = ""
    quality: str = ""
    source_timestamp: datetime | str | None = None
    server_timestamp: datetime | str | None = None
    gateway_timestamp: datetime | str | None = None

    def __post_init__(self) -> None:
        self.point_key = str(self.point_key).strip()
        if not self.point_key:
            raise ValueError("PointValue.point_key不可為空白")

        self.protocol = str(self.protocol).strip().upper()
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise ValueError(
                f"不支援的protocol：{self.protocol}，"
                "只允許MODBUS_RTU、MODBUS_TCP或OPCUA"
            )

        self.source_name = str(self.source_name or "")
        self.device_name = str(self.device_name or "")
        self.point_name = str(self.point_name or "")
        self.address_text = str(self.address_text or "")
        self.status_text = str(self.status_text or "")
        self.data_type = normalize_data_type(self.data_type)
        self.writable = bool(self.writable)

        if self.timestamp is None:
            self.timestamp = datetime.now()
        elif not isinstance(self.timestamp, (datetime, str)):
            self.timestamp = str(self.timestamp)

        self.tag_id = str(self.tag_id or self.point_key)
        self.connection_id = str(self.connection_id or "")
        self.device_id = str(self.device_id or "")
        self.quality = str(self.quality or self.status_text or "Unknown")
        if self.source_timestamp is None:
            self.source_timestamp = self.timestamp
        if self.server_timestamp is None:
            self.server_timestamp = self.timestamp
        if self.gateway_timestamp is None:
            self.gateway_timestamp = self.server_timestamp

        if self.value_text is None or (
            self.value_text == "" and self.value is not None
        ):
            self.value_text = value_to_text(self.value)
        else:
            self.value_text = str(self.value_text)

        if self.value_number is None:
            self.value_number = value_to_number(self.value)
        else:
            self.value_number = float(self.value_number)

        if isinstance(self.raw_config, Mapping):
            self.raw_config = dict(self.raw_config)
        else:
            raise TypeError("PointValue.raw_config必須是Mapping")

    def to_dict(self) -> dict[str, Any]:
        """轉成可供UI及資料庫使用的字典。"""
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
            "tag_id": self.tag_id,
            "connection_id": self.connection_id,
            "device_id": self.device_id,
            "quality": self.quality,
            "source_timestamp": self.source_timestamp,
            "server_timestamp": self.server_timestamp,
            "gateway_timestamp": self.gateway_timestamp,
        }
