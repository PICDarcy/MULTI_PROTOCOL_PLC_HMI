"""Modbus scalar 型別的明確 register 數量與 byte/word order 解碼。"""

from __future__ import annotations

import struct
from typing import Any


_TYPE_ALIASES = {
    "BOOL": "BOOLEAN",
    "BOOLEAN": "BOOLEAN",
    "BYTE": "BYTE",
    "UINT8": "BYTE",
    "SBYTE": "SBYTE",
    "INT8": "SBYTE",
    "INT": "INT16",
    "INT16": "INT16",
    "WORD": "UINT16",
    "UINT16": "UINT16",
    "DINT": "INT32",
    "INT32": "INT32",
    "DWORD": "UINT32",
    "UINT32": "UINT32",
    "REAL": "FLOAT",
    "FLOAT": "FLOAT",
    "FLOAT32": "FLOAT",
    "LINT": "INT64",
    "INT64": "INT64",
    "LWORD": "UINT64",
    "UINT64": "UINT64",
    "LREAL": "DOUBLE",
    "DOUBLE": "DOUBLE",
    "FLOAT64": "DOUBLE",
    "STRING": "STRING",
    "STR": "STRING",
    "RAW": "RAW",
    "AUTO": "AUTO",
}

_FORMATS = {
    "BYTE": ">B",
    "SBYTE": ">b",
    "INT16": ">h",
    "UINT16": ">H",
    "INT32": ">i",
    "UINT32": ">I",
    "FLOAT": ">f",
    "INT64": ">q",
    "UINT64": ">Q",
    "DOUBLE": ">d",
}

_AREAS = {
    "coil",
    "discrete_input",
    "input_register",
    "holding_register",
}


def _normalize_area(area: Any) -> str:
    normalized = str(area).strip().lower()
    if normalized not in _AREAS:
        raise ValueError(f"不支援的Modbus資料區：{area}")
    return normalized


def _type_and_legacy_order(data_type: Any) -> tuple[str, str, str]:
    text = str(data_type or "Auto").strip().upper().replace("-", "_")
    suffix = text.rsplit("_", 1)[-1]
    byte_order = "big"
    word_order = "big"
    if suffix in {"ABCD", "BADC", "CDAB", "DCBA"}:
        text = text[: -(len(suffix) + 1)]
        byte_order = "little" if suffix in {"BADC", "DCBA"} else "big"
        word_order = "little" if suffix in {"CDAB", "DCBA"} else "big"
    normalized = _TYPE_ALIASES.get(text)
    if normalized is None:
        raise ValueError(f"不支援的Modbus data_type：{data_type}")
    return normalized, byte_order, word_order


def _normalize_order(value: Any, *, kind: str, default: str) -> str:
    if value is None or str(value).strip() == "":
        return default
    text = str(value).strip().lower().replace("-", "_")
    aliases = {
        "big": "big",
        "big_endian": "big",
        "msb": "big",
        "msw": "big",
        "little": "little",
        "little_endian": "little",
        "lsb": "little",
        "lsw": "little",
    }
    try:
        return aliases[text]
    except KeyError as exc:
        raise ValueError(f"{kind}_order只允許big或little") from exc


def register_count_for_type(data_type: Any, *, area: str) -> int:
    """回傳單一 scalar 需要的 bit/register 數量。"""
    normalized_area = _normalize_area(area)
    if normalized_area in {"coil", "discrete_input"}:
        return 1
    data_type_name, _, _ = _type_and_legacy_order(data_type)
    if data_type_name in {"BOOLEAN", "BYTE", "SBYTE", "INT16", "UINT16", "AUTO"}:
        return 1
    if data_type_name in {"INT32", "UINT32", "FLOAT"}:
        return 2
    if data_type_name in {"INT64", "UINT64", "DOUBLE"}:
        return 4
    return 1


def decode_modbus_value(
    raw_values: list[Any],
    data_type: Any,
    *,
    area: str = "holding_register",
    byte_order: Any = None,
    word_order: Any = None,
) -> Any:
    """依明確資料區與 byte/word order 解碼一個 Modbus scalar。"""
    normalized_area = _normalize_area(area)
    if normalized_area in {"coil", "discrete_input"}:
        if not raw_values:
            raise ValueError("Modbus bit回應不可為空")
        values = [bool(value) for value in raw_values]
        return values[0] if len(values) == 1 else values

    type_name, legacy_byte_order, legacy_word_order = _type_and_legacy_order(
        data_type
    )
    resolved_byte_order = _normalize_order(
        byte_order,
        kind="byte",
        default=legacy_byte_order,
    )
    resolved_word_order = _normalize_order(
        word_order,
        kind="word",
        default=legacy_word_order,
    )
    needed = register_count_for_type(type_name, area=normalized_area)
    registers = [int(value) & 0xFFFF for value in raw_values]
    if len(registers) < needed:
        raise ValueError(f"{data_type}需要{needed}個Register")
    if type_name == "AUTO":
        return registers[0] if len(registers) == 1 else registers
    if type_name == "UINT16" and len(registers) > 1:
        return registers
    if type_name == "RAW":
        return registers
    if type_name == "BOOLEAN":
        return bool(registers[0])

    words = [struct.pack(">H", value) for value in registers]
    if resolved_byte_order == "little":
        words = [word[::-1] for word in words]
    if resolved_word_order == "little":
        words.reverse()
    payload = b"".join(words)
    if type_name == "STRING":
        return payload.rstrip(b"\x00").decode("utf-8", errors="replace")
    return struct.unpack(_FORMATS[type_name], payload[: struct.calcsize(_FORMATS[type_name])])[0]
