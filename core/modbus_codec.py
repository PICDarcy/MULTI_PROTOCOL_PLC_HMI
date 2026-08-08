"""Modbus scalar 型別的 register 數量、byte/word order 編解碼。"""

from __future__ import annotations

import math
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
    "BYTESTRING": "BYTESTRING",
    "BYTE_STRING": "BYTESTRING",
    "STRUCTURE": "STRUCTURE",
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

_INTEGER_LIMITS = {
    "BYTE": (0, 0xFF),
    "SBYTE": (-0x80, 0x7F),
    "INT16": (-0x8000, 0x7FFF),
    "UINT16": (0, 0xFFFF),
    "INT32": (-0x80000000, 0x7FFFFFFF),
    "UINT32": (0, 0xFFFFFFFF),
    "INT64": (-0x8000000000000000, 0x7FFFFFFFFFFFFFFF),
    "UINT64": (0, 0xFFFFFFFFFFFFFFFF),
}

_MODBUS_OUTPUT_REGISTER_COUNTS = {
    "BYTE": 1,
    "SBYTE": 1,
    "INT16": 1,
    "UINT16": 1,
    "INT32": 2,
    "UINT32": 2,
    "FLOAT": 2,
    "INT64": 4,
    "UINT64": 4,
    "DOUBLE": 4,
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
    if text.endswith("[]"):
        raise ValueError(f"不支援的Modbus data_type：{data_type}")
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


def normalize_modbus_order(
    value: Any,
    *,
    kind: str,
    default: str = "big",
) -> str:
    """正規化輸出 mapping 的 byte/word order。"""
    if value is None or str(value).strip() == "":
        return default
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "big": "big",
        "big_endian": "big",
        "high": "big",
        "high_first": "big",
        "high_word_first": "big",
        "msb": "big",
        "msw": "big",
        "little": "little",
        "little_endian": "little",
        "low": "little",
        "low_first": "little",
        "low_word_first": "little",
        "lsb": "little",
        "lsw": "little",
    }
    try:
        return aliases[text]
    except KeyError as exc:
        raise ValueError(f"{kind}_order只允許big或little") from exc


def _normalize_order(value: Any, *, kind: str, default: str) -> str:
    return normalize_modbus_order(value, kind=kind, default=default)


def is_modbus_output_scalar_type(data_type: Any) -> bool:
    """回傳型別是否可無轉型地發布至 Modbus Coil/Register。"""
    try:
        type_name, _, _ = _type_and_legacy_order(data_type)
    except ValueError:
        return False
    return type_name == "BOOLEAN" or type_name in _MODBUS_OUTPUT_REGISTER_COUNTS


def modbus_output_description(data_type: Any) -> str:
    """回傳可直接顯示於設定介面的 Modbus 輸出支援說明。"""
    try:
        type_name, _, _ = _type_and_legacy_order(data_type)
    except ValueError:
        return f"不支援：{data_type}"
    if type_name == "BOOLEAN":
        return "支援：Coil（1 bit）"
    count = _MODBUS_OUTPUT_REGISTER_COUNTS.get(type_name)
    if count is None:
        return f"不支援：{data_type}"
    return f"支援：Holding Register × {count}"


def modbus_output_register_count(data_type: Any) -> int:
    """回傳第一版 Modbus TCP 數值輸出固定占用的 Register 數量。"""
    try:
        type_name, _, _ = _type_and_legacy_order(data_type)
        return _MODBUS_OUTPUT_REGISTER_COUNTS[type_name]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{data_type}不支援Modbus TCP Register輸出") from exc


def register_count_for_type(data_type: Any, *, area: str) -> int:
    """回傳來源端單一 scalar 需要的 bit/register 數量。"""
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


def _validate_output_value(value: Any, type_name: str) -> int | float:
    if type_name in _INTEGER_LIMITS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{type_name}輸出只接受整數值，不進行跨型別轉換")
        minimum, maximum = _INTEGER_LIMITS[type_name]
        if not minimum <= value <= maximum:
            raise ValueError(f"{type_name}輸出值必須介於{minimum}到{maximum}")
        return value

    if type_name in {"FLOAT", "DOUBLE"}:
        if not isinstance(value, float):
            raise TypeError(f"{type_name}輸出只接受float，不進行跨型別轉換")
        if not math.isfinite(value):
            raise ValueError(f"{type_name}輸出值必須是有限數值")
        return value

    raise ValueError(f"{type_name}不支援Modbus TCP Register輸出")


def encode_modbus_value(
    value: Any,
    data_type: Any,
    *,
    byte_order: Any = None,
    word_order: Any = None,
) -> list[int]:
    """將 Canonical scalar 無跨型別轉換地編碼成連續 16-bit Registers。"""
    try:
        type_name, legacy_byte_order, legacy_word_order = _type_and_legacy_order(
            data_type
        )
        register_count = _MODBUS_OUTPUT_REGISTER_COUNTS[type_name]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{data_type}不支援Modbus TCP Register輸出") from exc

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
    normalized_value = _validate_output_value(value, type_name)
    payload = struct.pack(_FORMATS[type_name], normalized_value)

    # Byte/SByte 是單一 8-bit scalar；固定占用一個 Register，值放在
    # Big-endian 表示法的高位元組，另一個位元組明確補零。
    if len(payload) == 1:
        payload += b"\x00"
    expected_size = register_count * 2
    if len(payload) != expected_size:
        raise AssertionError(
            f"{type_name}編碼長度錯誤：{len(payload)} != {expected_size}"
        )

    words = [payload[index : index + 2] for index in range(0, len(payload), 2)]
    if resolved_byte_order == "little":
        words = [word[::-1] for word in words]
    if resolved_word_order == "little":
        words.reverse()
    return [struct.unpack(">H", word)[0] for word in words]


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
    return struct.unpack(
        _FORMATS[type_name],
        payload[: struct.calcsize(_FORMATS[type_name])],
    )[0]
