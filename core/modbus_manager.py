"""Modbus RTU多站輪詢、讀取與寫入管理。"""

from __future__ import annotations

import math
import struct
import threading
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .data_model import PointValue, make_modbus_point_key

PROTOCOL_MODBUS = "MODBUS_RTU"


class ModbusRtuManager:
    """使用單一序列埠輪詢多個Modbus RTU站號。

    修正重點：
    - 所有Modbus讀取、寫入、寫入後read-back與close都使用同一把_io_lock。
    - stop_polling逾時時保留舊thread引用，避免再次啟動第二個輪詢thread。
    - 輪詢迴圈在stop_event被設定後盡快中止，降低關閉卡住機率。
    """

    def __init__(self, config_manager, value_bus, log_func=None):
        self.config_manager = config_manager
        self.value_bus = value_bus
        self.log_func = log_func
        self._state_lock = threading.RLock()
        self._io_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = None
        self._config: dict[str, Any] = {}
        self._points: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self.reload_config()

    def _log(self, message: str, level: str = "INFO") -> None:
        if callable(self.log_func):
            try:
                self.log_func(message, level)
            except TypeError:
                self.log_func(message)

    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "y", "on", "是", "啟用"}:
                return True
            if text in {"0", "false", "no", "n", "off", "否", "停用"}:
                return False
        return bool(value)

    def _config_snapshot(self) -> dict[str, Any]:
        getter = getattr(self.config_manager, "get_section", None)
        if callable(getter):
            value = getter("modbus_rtu", {})
            if isinstance(value, dict):
                return dict(value)
        getter = getattr(self.config_manager, "get_config", None)
        if callable(getter):
            root = getter()
            if isinstance(root, dict) and isinstance(root.get("modbus_rtu"), dict):
                return dict(root["modbus_rtu"])
        root = getattr(self.config_manager, "config", {})
        if isinstance(root, dict) and isinstance(root.get("modbus_rtu"), dict):
            return dict(root["modbus_rtu"])
        return {}

    def _point_key(self, device: Mapping[str, Any], point: Mapping[str, Any]) -> str:
        port = str(self._config.get("port", ""))
        source = f"{port}|{device.get('name', '')}"
        return make_modbus_point_key(
            source,
            int(device.get("station_id", 1)),
            str(point.get("type", "holding_register")),
            int(point.get("address", 0)),
            str(point.get("name", "")),
        )

    def reload_config(self):
        was_running = self.is_running()
        if was_running:
            stop_result = self.stop_polling()
            if isinstance(stop_result, str) and "逾時" in stop_result:
                raise RuntimeError("Modbus輪詢尚未安全停止，無法重新載入設定。")

        with self._state_lock:
            self._config = self._config_snapshot()
            self._points.clear()
            for device in self._config.get("devices", []):
                if not isinstance(device, Mapping):
                    continue
                for point in device.get("points", []):
                    if isinstance(point, Mapping):
                        key = self._point_key(device, point)
                        if key in self._points:
                            raise ValueError(f"Modbus point_key重複：{key}")
                        self._points[key] = (dict(device), dict(point))

        if was_running and self._as_bool(self._config.get("enable", self._config.get("enabled")), False):
            self.start_polling()
        return {"point_count": len(self._points), "running": self.is_running()}

    def _make_client(self):
        try:
            from pymodbus.client import ModbusSerialClient
        except ImportError as exc:
            raise RuntimeError("尚未安裝pymodbus，請執行pip install -r requirements.txt") from exc

        config = self._config
        return ModbusSerialClient(
            port=str(config.get("port", "COM1")),
            baudrate=int(config.get("baudrate", 9600)),
            bytesize=int(config.get("bytesize", 8)),
            parity=str(config.get("parity", "N")).upper(),
            stopbits=float(config.get("stopbits", 1)),
            timeout=float(config.get("timeout", 1.0)),
        )

    def _ensure_client_unlocked(self):
        if self._client is None:
            self._client = self._make_client()
        connected = self._client.connect()
        if connected is False:
            raise ConnectionError(f"無法開啟Modbus序列埠：{self._config.get('port', '')}")
        return self._client

    def _ensure_client(self):
        with self._io_lock:
            return self._ensure_client_unlocked()

    def _close_client(self) -> None:
        with self._io_lock:
            client, self._client = self._client, None
            if client is not None:
                try:
                    client.close()
                except Exception as exc:
                    self._log(f"關閉Modbus序列埠時發生錯誤：{exc}", "WARNING")

    @staticmethod
    def _call_unit(method, station_id: int, **kwargs):
        last_error = None
        for unit_key in ("device_id", "slave", "unit"):
            try:
                return method(**kwargs, **{unit_key: station_id})
            except TypeError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        return method(**kwargs)

    @staticmethod
    def _response_error(response) -> None:
        if response is None:
            raise RuntimeError("Modbus沒有回應")
        checker = getattr(response, "isError", None)
        if callable(checker) and checker():
            raise RuntimeError(str(response))

    def _read_raw_unlocked(self, client, device: Mapping[str, Any], point: Mapping[str, Any]):
        station = int(device.get("station_id", 1))
        address = int(point.get("address", 0))
        count = max(1, int(point.get("count", 1)))
        point_type = str(point.get("type", "holding_register")).lower()
        methods = {
            "holding_register": "read_holding_registers",
            "input_register": "read_input_registers",
            "coil": "read_coils",
            "discrete_input": "read_discrete_inputs",
        }
        method_name = methods.get(point_type)
        if method_name is None:
            raise ValueError(f"不支援的Modbus點位類型：{point_type}")
        method = getattr(client, method_name)
        response = self._call_unit(method, station, address=address, count=count)
        self._response_error(response)
        if point_type in {"coil", "discrete_input"}:
            return list(getattr(response, "bits", []))[:count]
        return list(getattr(response, "registers", []))[:count]

    def _read_raw(self, client, device: Mapping[str, Any], point: Mapping[str, Any]):
        with self._io_lock:
            return self._read_raw_unlocked(client, device, point)

    @staticmethod
    def _ordered_bytes(registers: list[int], data_type: str) -> bytes:
        raw = b"".join(struct.pack(">H", int(value) & 0xFFFF) for value in registers)
        normalized = data_type.upper().replace("-", "_")
        suffix = normalized.rsplit("_", 1)[-1]
        if suffix not in {"ABCD", "BADC", "CDAB", "DCBA"}:
            suffix = "ABCD"
        if suffix in {"BADC", "DCBA"}:
            raw = b"".join(raw[index : index + 2][::-1] for index in range(0, len(raw), 2))
        if suffix in {"CDAB", "DCBA"} and len(raw) >= 4:
            words = [raw[index : index + 2] for index in range(0, len(raw), 2)]
            raw = b"".join(reversed(words))
        return raw

    @classmethod
    def _decode(cls, raw_values: list[Any], data_type: str, point_type: str):
        normalized = str(data_type or "Auto").upper().replace("-", "_")
        if point_type in {"coil", "discrete_input"}:
            values = [bool(value) for value in raw_values]
            return values[0] if len(values) == 1 else values
        registers = [int(value) & 0xFFFF for value in raw_values]
        raw = cls._ordered_bytes(registers, normalized)
        base = normalized.split("_", 1)[0]
        if base in {"AUTO", "UINT16"}:
            return registers[0] if len(registers) == 1 else registers
        if base in {"BOOL", "BOOLEAN"}:
            return bool(registers[0])
        if base == "INT16":
            return struct.unpack(">h", raw[:2])[0]
        formats = {
            "UINT32": ">I",
            "INT32": ">i",
            "FLOAT32": ">f",
            "UINT64": ">Q",
            "INT64": ">q",
            "FLOAT64": ">d",
            "DOUBLE": ">d",
        }
        if base in formats:
            size = struct.calcsize(formats[base])
            if len(raw) < size:
                raise ValueError(f"{data_type}需要{size // 2}個Register")
            return struct.unpack(formats[base], raw[:size])[0]
        if base == "STRING":
            return raw.rstrip(b"\x00").decode("utf-8", errors="replace")
        if base == "RAW":
            return registers
        raise ValueError(f"不支援的Modbus data_type：{data_type}")

    @staticmethod
    def _value_text(value: Any) -> str:
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        return str(value)

    @staticmethod
    def _value_number(value: Any):
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, (int, float)):
            number = float(value)
            return number if math.isfinite(number) else None
        return None

    def _publish(self, device: Mapping[str, Any], point: Mapping[str, Any], value: Any, status: str):
        key = self._point_key(device, point)
        raw_config = dict(point)
        raw_config.update(
            {
                "station_id": int(device.get("station_id", 1)),
                "serial_port": str(self._config.get("port", "")),
                "device_name": str(device.get("name", "")),
            }
        )
        point_value = PointValue(
            point_key=key,
            protocol=PROTOCOL_MODBUS,
            source_name=str(self._config.get("port", "")),
            device_name=str(device.get("name", "")),
            point_name=str(point.get("name", "")),
            address_text=(
                f"站號{device.get('station_id', 1)} "
                f"{point.get('type', '')} {point.get('address', 0)}"
            ),
            value=value,
            value_text=self._value_text(value),
            value_number=self._value_number(value),
            status_text=status,
            timestamp=datetime.now(),
            writable=self._as_bool(point.get("writable", False), False),
            data_type=str(point.get("data_type", "Auto")),
            raw_config=raw_config,
        )
        self.value_bus.publish(point_value)
        return point_value

    def read_all_once(self):
        success = 0
        failed = 0
        with self._state_lock:
            devices = list(self._config.get("devices", []))

        for device in devices:
            if self._stop_event.is_set():
                break
            if not isinstance(device, Mapping) or not self._as_bool(device.get("enable", True), True):
                continue
            for point in device.get("points", []):
                if self._stop_event.is_set():
                    break
                if not isinstance(point, Mapping) or not self._as_bool(point.get("enable", True), True):
                    continue
                try:
                    with self._io_lock:
                        client = self._ensure_client_unlocked()
                        raw = self._read_raw_unlocked(client, device, point)
                    value = self._decode(
                        raw,
                        str(point.get("data_type", "Auto")),
                        str(point.get("type", "")),
                    )
                    self._publish(device, point, value, "Good")
                    success += 1
                except Exception as exc:
                    self._publish(device, point, None, f"讀取失敗：{exc}")
                    self._log(f"Modbus點位「{point.get('name', '')}」讀取失敗：{exc}", "ERROR")
                    failed += 1

        return {"success": success, "failed": failed, "total": success + failed}

    def _poll_loop(self) -> None:
        interval = max(0.05, float(self._config.get("poll_interval", 1.0)))
        try:
            while not self._stop_event.is_set():
                started = time.monotonic()
                try:
                    self.read_all_once()
                except Exception as exc:
                    self._log(f"Modbus輪詢失敗：{exc}", "ERROR")
                    self._close_client()
                wait_time = max(0.0, interval - (time.monotonic() - started))
                self._stop_event.wait(wait_time)
        finally:
            self._close_client()
            with self._state_lock:
                current = threading.current_thread()
                if self._thread is current:
                    self._thread = None

    def start_polling(self):
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                if self._stop_event.is_set():
                    return "Modbus輪詢正在停止，請稍後再啟動"
                return "Modbus輪詢已在執行"

            self._thread = None
            if not self._as_bool(self._config.get("enable", self._config.get("enabled")), False):
                raise RuntimeError("config.json尚未啟用modbus_rtu.enable")
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._poll_loop,
                name="ModbusRtuPolling",
                daemon=True,
            )
            self._thread.start()
        return "Modbus輪詢已啟動"

    def stop_polling(self):
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread

        if thread is not None and thread is not threading.current_thread():
            timeout = max(3.0, float(self._config.get("timeout", 1.0)) + 2.0)
            thread.join(timeout=timeout)

        if thread is not None and thread.is_alive():
            self._log("Modbus輪詢執行緒停止逾時，保留執行緒引用並禁止再次啟動。", "WARNING")
            return "Modbus輪詢停止逾時，請稍後再確認狀態"

        self._close_client()
        with self._state_lock:
            if self._thread is thread:
                self._thread = None
        return "Modbus輪詢已停止"

    def is_running(self) -> bool:
        with self._state_lock:
            return bool(self._thread and self._thread.is_alive())

    @classmethod
    def _encode_registers(cls, value_text: Any, data_type: str, count: int) -> list[int]:
        normalized = str(data_type or "UInt16").upper().replace("-", "_")
        base = normalized.split("_", 1)[0]
        if base in {"AUTO", "UINT16"}:
            registers = [int(str(value_text).strip(), 0)]
        elif base == "INT16":
            registers = list(struct.unpack(">H", struct.pack(">h", int(value_text))))
        elif base in {"BOOL", "BOOLEAN"}:
            registers = [1 if cls._parse_bool(value_text) else 0]
        else:
            formats = {
                "UINT32": ">I",
                "INT32": ">i",
                "FLOAT32": ">f",
                "UINT64": ">Q",
                "INT64": ">q",
                "FLOAT64": ">d",
                "DOUBLE": ">d",
            }
            if base == "STRING":
                raw = str(value_text).encode("utf-8")[: count * 2].ljust(count * 2, b"\x00")
            elif base in formats:
                converter = float if base in {"FLOAT32", "FLOAT64", "DOUBLE"} else int
                raw = struct.pack(formats[base], converter(value_text))
            else:
                raise ValueError(f"不支援的Modbus寫入data_type：{data_type}")
            suffix = normalized.rsplit("_", 1)[-1]
            if suffix in {"CDAB", "DCBA"} and len(raw) >= 4:
                words = [raw[index : index + 2] for index in range(0, len(raw), 2)]
                raw = b"".join(reversed(words))
            if suffix in {"BADC", "DCBA"}:
                raw = b"".join(raw[index : index + 2][::-1] for index in range(0, len(raw), 2))
            registers = [
                struct.unpack(">H", raw[index : index + 2])[0]
                for index in range(0, len(raw), 2)
            ]

        if len(registers) > count:
            raise ValueError(f"寫入值需要{len(registers)}個Register，但設定count只有{count}")
        return registers + [0] * max(0, count - len(registers))

    @staticmethod
    def _parse_bool(value_text: Any) -> bool:
        if isinstance(value_text, bool):
            return value_text
        text = str(value_text).strip().lower()
        if text in {"1", "true", "yes", "y", "on", "是"}:
            return True
        if text in {"0", "false", "no", "n", "off", "否"}:
            return False
        return bool(int(text, 0))

    def _write_registers_unlocked(
        self,
        client,
        station: int,
        address: int,
        registers: list[int],
    ):
        if len(registers) == 1:
            method = getattr(client, "write_register")
            return self._call_unit(method, station, address=address, value=registers[0])
        method = getattr(client, "write_registers")
        return self._call_unit(method, station, address=address, values=registers)

    def _write_coil_unlocked(self, client, station: int, address: int, value: bool):
        method = getattr(client, "write_coil")
        return self._call_unit(method, station, address=address, value=value)

    def write_point(self, point_key, value_text):
        with self._state_lock:
            item = self._points.get(str(point_key))
        if item is None:
            raise KeyError(f"找不到Modbus點位：{point_key}")

        device, point = item
        if not self._as_bool(point.get("writable", False), False):
            raise PermissionError(f"Modbus點位不可寫入：{point.get('name', point_key)}")

        point_type = str(point.get("type", "holding_register")).lower()
        if point_type not in {"holding_register", "coil"}:
            raise ValueError(f"{point_type}不支援寫入，僅holding_register與coil可寫入")

        station = int(device.get("station_id", 1))
        address = int(point.get("address", 0))
        count = max(1, int(point.get("count", 1)))

        with self._io_lock:
            client = self._ensure_client_unlocked()
            if point_type == "coil":
                response = self._write_coil_unlocked(
                    client,
                    station,
                    address,
                    self._parse_bool(value_text),
                )
            else:
                registers = self._encode_registers(
                    value_text,
                    str(point.get("data_type", "UInt16")),
                    count,
                )
                response = self._write_registers_unlocked(client, station, address, registers)
            self._response_error(response)

            raw = self._read_raw_unlocked(client, device, point)

        value = self._decode(raw, str(point.get("data_type", "Auto")), point_type)
        self._publish(device, point, value, "WriteGood")
        return True
