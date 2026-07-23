"""Modbus TCP多設備輪詢、讀取與寫入管理。"""

from __future__ import annotations

import math
import struct
import threading
import time
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import quote

from .data_model import PointValue

PROTOCOL_MODBUS_TCP = "MODBUS_TCP"


def _key_part(value: Any) -> str:
    return quote(str(value), safe="")


def make_modbus_tcp_point_key(
    host: str,
    port: int,
    unit_id: int,
    point_type: str,
    address: int,
    point_name: str = "",
    device_name: str = "",
) -> str:
    return "::".join(
        (
            PROTOCOL_MODBUS_TCP,
            _key_part(f"{host}:{int(port)}"),
            str(int(unit_id)),
            _key_part(str(point_type).upper()),
            str(int(address)),
            _key_part(device_name),
            _key_part(point_name),
        )
    )


class ModbusTcpManager:
    """使用Modbus TCP輪詢多台設備。"""

    def __init__(self, config_manager, value_bus, log_func=None):
        self.config_manager = config_manager
        self.value_bus = value_bus
        self.log_func = log_func
        self._state_lock = threading.RLock()
        self._io_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._clients: dict[str, Any] = {}
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
    def _to_bool(value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on", "是", "啟用"}
        return bool(value)

    def _config_snapshot(self) -> dict[str, Any]:
        getter = getattr(self.config_manager, "get_section", None)
        if callable(getter):
            value = getter("modbus_tcp", {})
            if isinstance(value, dict):
                return dict(value)
        getter = getattr(self.config_manager, "get_config", None)
        if callable(getter):
            root = getter()
            if isinstance(root, dict) and isinstance(root.get("modbus_tcp"), dict):
                return dict(root["modbus_tcp"])
        root = getattr(self.config_manager, "config", {})
        if isinstance(root, dict) and isinstance(root.get("modbus_tcp"), dict):
            return dict(root["modbus_tcp"])
        return {}

    def _device_host(self, device: Mapping[str, Any]) -> str:
        return str(device.get("host", device.get("ip", "127.0.0.1"))).strip() or "127.0.0.1"

    def _device_port(self, device: Mapping[str, Any]) -> int:
        return int(device.get("port", self._config.get("default_port", 502)))

    def _device_unit(self, device: Mapping[str, Any]) -> int:
        return int(device.get("unit_id", device.get("station_id", device.get("slave", 1))))

    def _client_key(self, device: Mapping[str, Any]) -> str:
        return f"{self._device_host(device)}:{self._device_port(device)}"

    def _point_key(self, device: Mapping[str, Any], point: Mapping[str, Any]) -> str:
        return make_modbus_tcp_point_key(
            self._device_host(device),
            self._device_port(device),
            self._device_unit(device),
            str(point.get("type", "holding_register")),
            int(point.get("address", 0)),
            str(point.get("name", "")),
            str(device.get("name", "")),
        )

    def reload_config(self):
        was_running = self.is_running()
        if was_running:
            stop_message = self.stop_polling()
            if self.is_running():
                raise RuntimeError(f"Modbus TCP輪詢尚未完全停止，暫不重新載入設定：{stop_message}")
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
                            raise ValueError(f"Modbus TCP point_key重複：{key}")
                        self._points[key] = (dict(device), dict(point))
        if was_running and self._to_bool(self._config.get("enable"), False):
            self.start_polling()
        return {"point_count": len(self._points), "running": self.is_running()}

    def _make_client(self, device: Mapping[str, Any]):
        try:
            from pymodbus.client import ModbusTcpClient
        except ImportError as exc:
            raise RuntimeError("尚未安裝pymodbus，請執行pip install -r requirements.txt") from exc
        host = self._device_host(device)
        port = self._device_port(device)
        timeout = float(device.get("timeout", self._config.get("timeout", 1.0)))
        try:
            return ModbusTcpClient(host=host, port=port, timeout=timeout)
        except TypeError:
            return ModbusTcpClient(host, port=port, timeout=timeout)

    def _ensure_client(self, device: Mapping[str, Any]):
        key = self._client_key(device)
        client = self._clients.get(key)
        if client is None:
            client = self._make_client(device)
            self._clients[key] = client
        connected = client.connect()
        if connected is False:
            raise ConnectionError(f"無法連線Modbus TCP設備：{key}")
        return client

    def _close_clients(self) -> None:
        with self._io_lock:
            clients, self._clients = self._clients, {}
            for client in clients.values():
                try:
                    client.close()
                except Exception:
                    pass

    @staticmethod
    def _call_unit(method, unit_id: int, **kwargs):
        last_error = None
        for unit_key in ("device_id", "slave", "unit"):
            try:
                return method(**kwargs, **{unit_key: unit_id})
            except TypeError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        return method(**kwargs)

    @staticmethod
    def _response_error(response) -> None:
        if response is None:
            raise RuntimeError("Modbus TCP沒有回應")
        checker = getattr(response, "isError", None)
        if callable(checker) and checker():
            raise RuntimeError(str(response))

    def _read_raw_locked(self, client, device: Mapping[str, Any], point: Mapping[str, Any]):
        unit_id = self._device_unit(device)
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
            raise ValueError(f"不支援的Modbus TCP點位類型：{point_type}")
        method = getattr(client, method_name)
        response = self._call_unit(method, unit_id, address=address, count=count)
        self._response_error(response)
        if point_type in {"coil", "discrete_input"}:
            return list(getattr(response, "bits", []))[:count]
        return list(getattr(response, "registers", []))[:count]

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
        point_type = str(point_type or "").lower()
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
        formats = {"UINT32": ">I", "INT32": ">i", "FLOAT32": ">f", "FLOAT": ">f", "UINT64": ">Q", "INT64": ">q", "FLOAT64": ">d", "DOUBLE": ">d"}
        if base in formats:
            size = struct.calcsize(formats[base])
            if len(raw) < size:
                raise ValueError(f"{data_type}需要{size // 2}個Register")
            return struct.unpack(formats[base], raw[:size])[0]
        if base == "STRING":
            return raw.rstrip(b"\x00").decode("utf-8", errors="replace")
        if base == "RAW":
            return registers
        raise ValueError(f"不支援的Modbus TCP data_type：{data_type}")

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
        host = self._device_host(device)
        port = self._device_port(device)
        unit_id = self._device_unit(device)
        raw_config = dict(point)
        raw_config.update({"host": host, "port": port, "unit_id": unit_id, "device_name": str(device.get("name", ""))})
        point_value = PointValue(
            point_key=key,
            protocol=PROTOCOL_MODBUS_TCP,
            source_name=f"{host}:{port}",
            device_name=str(device.get("name", "")),
            point_name=str(point.get("name", "")),
            address_text=f"{host}:{port} Unit {unit_id} {point.get('type', '')} {point.get('address', 0)}",
            value=value,
            value_text=self._value_text(value),
            value_number=self._value_number(value),
            status_text=status,
            timestamp=datetime.now(),
            writable=self._to_bool(point.get("writable"), False),
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
            if not isinstance(device, Mapping) or not self._to_bool(device.get("enable"), True):
                continue
            for point in device.get("points", []):
                if self._stop_event.is_set():
                    break
                if not isinstance(point, Mapping) or not self._to_bool(point.get("enable"), True):
                    continue
                try:
                    with self._io_lock:
                        client = self._ensure_client(device)
                        raw = self._read_raw_locked(client, device, point)
                    value = self._decode(raw, str(point.get("data_type", "Auto")), str(point.get("type", "")))
                    self._publish(device, point, value, "Good")
                    success += 1
                except Exception as exc:
                    self._publish(device, point, None, f"讀取失敗：{exc}")
                    self._log(f"Modbus TCP點位「{point.get('name', '')}」讀取失敗：{exc}", "ERROR")
                    failed += 1
        return {"success": success, "failed": failed, "total": success + failed}

    def _poll_loop(self) -> None:
        try:
            interval = max(0.05, float(self._config.get("poll_interval", 1.0)))
            while not self._stop_event.is_set():
                started = time.monotonic()
                try:
                    self.read_all_once()
                except Exception as exc:
                    self._log(f"Modbus TCP輪詢失敗：{exc}", "ERROR")
                    self._close_clients()
                self._stop_event.wait(max(0.0, interval - (time.monotonic() - started)))
        finally:
            self._close_clients()
            with self._state_lock:
                current = threading.current_thread()
                if self._thread is current:
                    self._thread = None

    def start_polling(self):
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return "Modbus TCP輪詢已在執行"
            if not self._to_bool(self._config.get("enable"), False):
                raise RuntimeError("config.json尚未啟用modbus_tcp.enable")
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._poll_loop, name="ModbusTcpPolling", daemon=True)
            self._thread.start()
        return "Modbus TCP輪詢已啟動"

    def stop_polling(self):
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(2.0, float(self._config.get("timeout", 1.0)) + 1.0))
        if thread is not None and thread.is_alive():
            self._log("Modbus TCP輪詢執行緒停止逾時，等待目前通訊逾時後自動結束", "WARNING")
            return "Modbus TCP輪詢停止逾時，正在等待目前通訊結束"
        self._close_clients()
        with self._state_lock:
            if self._thread is thread:
                self._thread = None
        return "Modbus TCP輪詢已停止"

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
        else:
            formats = {"UINT32": ">I", "INT32": ">i", "FLOAT32": ">f", "FLOAT": ">f", "UINT64": ">Q", "INT64": ">q", "FLOAT64": ">d", "DOUBLE": ">d"}
            if base == "STRING":
                raw = str(value_text).encode("utf-8")[: count * 2].ljust(count * 2, b"\x00")
            elif base in formats:
                converter = float if base in {"FLOAT32", "FLOAT64", "FLOAT", "DOUBLE"} else int
                raw = struct.pack(formats[base], converter(value_text))
            else:
                raise ValueError(f"不支援的Modbus TCP寫入data_type：{data_type}")
            suffix = normalized.rsplit("_", 1)[-1]
            if suffix in {"CDAB", "DCBA"} and len(raw) >= 4:
                words = [raw[index : index + 2] for index in range(0, len(raw), 2)]
                raw = b"".join(reversed(words))
            if suffix in {"BADC", "DCBA"}:
                raw = b"".join(raw[index : index + 2][::-1] for index in range(0, len(raw), 2))
            registers = [struct.unpack(">H", raw[index : index + 2])[0] for index in range(0, len(raw), 2)]
        if len(registers) > count:
            raise ValueError(f"寫入值需要{len(registers)}個Register，但設定count只有{count}")
        return registers + [0] * (count - len(registers))

    @staticmethod
    def _encode_bits(value_text: Any, count: int) -> list[bool]:
        text = str(value_text).strip()
        lowered = text.lower()
        if count <= 1:
            if lowered in {"1", "true", "yes", "y", "on", "是"}:
                return [True]
            if lowered in {"0", "false", "no", "n", "off", "否"}:
                return [False]
            return [bool(int(text, 0))]
        separators = "," if "," in text else " "
        items = [item.strip() for item in text.split(separators) if item.strip()]
        if len(items) != count:
            raise ValueError(f"需要{count}個Boolean值")
        return [ModbusTcpManager._encode_bits(item, 1)[0] for item in items]

    def _write_raw_locked(self, client, device: Mapping[str, Any], point: Mapping[str, Any], value_text: Any):
        unit_id = self._device_unit(device)
        address = int(point.get("address", 0))
        count = max(1, int(point.get("count", 1)))
        point_type = str(point.get("type", "holding_register")).lower()
        data_type = str(point.get("data_type", "Auto"))
        if point_type == "holding_register":
            registers = self._encode_registers(value_text, data_type, count)
            if len(registers) == 1:
                response = self._call_unit(client.write_register, unit_id, address=address, value=registers[0])
            else:
                method = getattr(client, "write_registers")
                try:
                    response = self._call_unit(method, unit_id, address=address, values=registers)
                except TypeError:
                    response = self._call_unit(method, unit_id, address=address, registers=registers)
            self._response_error(response)
            return registers
        if point_type == "coil":
            bits = self._encode_bits(value_text, count)
            if len(bits) == 1:
                response = self._call_unit(client.write_coil, unit_id, address=address, value=bits[0])
            else:
                response = self._call_unit(client.write_coils, unit_id, address=address, values=bits)
            self._response_error(response)
            return bits
        raise ValueError("Modbus TCP只支援寫入holding_register與coil")

    def write_point(self, point_key: str, value_text: Any):
        with self._state_lock:
            item = self._points.get(str(point_key))
        if item is None:
            raise KeyError(f"找不到Modbus TCP點位：{point_key}")
        device, point = item
        if not self._to_bool(point.get("writable"), False):
            raise PermissionError(f"點位不可寫入：{point.get('name', '')}")
        with self._io_lock:
            client = self._ensure_client(device)
            self._write_raw_locked(client, device, point, value_text)
            raw = self._read_raw_locked(client, device, point)
        value = self._decode(raw, str(point.get("data_type", "Auto")), str(point.get("type", "")))
        self._publish(device, point, value, "Good")
        return True
