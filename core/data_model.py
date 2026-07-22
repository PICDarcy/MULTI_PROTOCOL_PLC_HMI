"""跨協定點位資料模型、資料轉換與唯一鍵工具。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import quote, unquote


SUPPORTED_PROTOCOLS = frozenset({"MODBUS_RTU", "OPCUA"})


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
        self.status_text = str(self.status_text or "")
        self.data_type = normalize_data_type(self.data_type)
        self.writable = bool(self.writable)

        if self.timestamp is None:
            self.timestamp = datetime.now()
        elif not isinstance(self.timestamp, (datetime, str)):
            self.timestamp = str(self.timestamp)

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
        }
