"""Modbus RTU多站輪詢與寫入管理模組。"""

from __future__ import annotations

import json
import struct
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from pymodbus.client import ModbusSerialClient

from core.data_model import PointValue, make_modbus_point_key


class ModbusRtuManager:
    """管理多序列埠、多站號與多點位的Modbus RTU通訊。"""

    PROTOCOL = "MODBUS_RTU"
    REGISTER_TYPES = {"holding_register", "input_register"}
    BIT_TYPES = {"coil", "discrete_input"}
    DATA_TYPES = {"UInt16", "Int16", "UInt32", "Int32", "Float", "Boolean"}

    def __init__(self, config_manager, value_bus, log_callback=None):
        self.config_manager = config_manager
        self.value_bus = value_bus
        self.log_callback = log_callback

        self._state_lock = threading.RLock()
        self._io_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._clients: Dict[Tuple[Any, ...], ModbusSerialClient] = {}
        self._devices: List[Dict[str, Any]] = []
        self._poll_interval = 1.0
        self.reload_config()

    def start_polling(self):
        """啟動背景輪詢；重複呼叫不會建立第二條執行緒。"""
        with self._state_lock:
            if self.is_running():
                return False
            self.reload_config()
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._poll_loop,
                name="ModbusRtuPollingThread",
                daemon=True,
            )
            self._thread.start()
        self._log("INFO", "Modbus RTU輪詢已啟動。")
        return True

    def stop_polling(self):
        """停止輪詢並關閉所有序列埠連線。"""
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(2.0, self._poll_interval + 1.0))
        with self._state_lock:
            self._thread = None
        with self._io_lock:
            self._close_clients()
        self._log("INFO", "Modbus RTU輪詢已停止。")
        return True

    def read_all_once(self):
        """依序讀取每台啟用裝置與每個啟用點位。"""
        result: List[PointValue] = []
        try:
            with self._io_lock:
                with self._state_lock:
                    devices = list(self._devices)
                for device in devices:
                    if not self._bool(device.get("enable", True), True):
                        continue
                    for point in device.get("points", []):
                        if not isinstance(point, dict):
                            self._log("ERROR", f"裝置{device.get('name')}包含無效點位設定。")
                            continue
                        if not self._bool(point.get("enable", True), True):
                            continue
                        try:
                            point_value = self._read_point(device, point)
                            if point_value is not None:
                                self.value_bus.publish(point_value)
                                result.append(point_value)
                        except Exception as exc:  # noqa: BLE001
                            self._log(
                                "ERROR",
                                f"讀取{device.get('name')}/{point.get('name')}失敗：{exc}",
                            )
        except Exception as exc:  # noqa: BLE001
            self._log("ERROR", f"Modbus RTU單次輪詢失敗：{exc}")
        return result

    def write_point(self, point_key, value_text):
        """寫入單一holding_register或coil點位。"""
        try:
            with self._io_lock:
                found = self._find_point(str(point_key))
                if found is None:
                    self._log("ERROR", f"找不到Modbus RTU點位：{point_key}")
                    return False
                device, point = found
                if not self._bool(device.get("enable", True), True):
                    self._log("ERROR", f"裝置{device.get('name')}未啟用。")
                    return False
                if not self._bool(point.get("enable", True), True):
                    self._log("ERROR", f"點位{point.get('name')}未啟用。")
                    return False
                if not self._bool(point.get("writable", False), False):
                    self._log("ERROR", f"點位{point.get('name')}不允許寫入。")
                    return False

                point_type = self._point_type(point.get("type"))
                address = self._int(point.get("address", 0), "address")
                station = self._station(device)
                client = self._client(device)
                if client is None:
                    return False

                if point_type == "coil":
                    response = self._call_station(
                        client.write_coil,
                        station,
                        address=address,
                        value=self._parse_bool(value_text),
                    )
                elif point_type == "holding_register":
                    data_type = self._data_type(point.get("data_type"))
                    registers = self._encode_registers(value_text, data_type)
                    if len(registers) == 1:
                        response = self._call_station(
                            client.write_register,
                            station,
                            address=address,
                            value=registers[0],
                        )
                    else:
                        response = self._call_station(
                            client.write_registers,
                            station,
                            address=address,
                            values=registers,
                        )
                else:
                    self._log("ERROR", f"{point_type}不支援寫入。")
                    return False

                if self._is_error(response):
                    self._log("ERROR", f"寫入{point.get('name')}失敗：{response}")
                    return False
                self._log("INFO", f"已寫入{device.get('name')}/{point.get('name')}={value_text}")
                return True
        except Exception as exc:  # noqa: BLE001
            self._log("ERROR", f"寫入Modbus RTU點位{point_key}失敗：{exc}")
            return False

    def reload_config(self):
        """重新讀取設定。"""
        try:
            section = self._section()
            devices, interval = self._normalize(section)
            with self._io_lock:
                self._close_clients()
                with self._state_lock:
                    self._devices = devices
                    self._poll_interval = interval
            self._log("INFO", f"Modbus RTU設定已載入，共{len(devices)}台裝置。")
            return True
        except Exception as exc:  # noqa: BLE001
            self._log("ERROR", f"載入Modbus RTU設定失敗：{exc}")
            return False

    def is_running(self):
        """回傳背景輪詢是否執行中。"""
        with self._state_lock:
            return bool(
                self._thread
                and self._thread.is_alive()
                and not self._stop_event.is_set()
            )

    def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                self.read_all_once()
            except Exception as exc:  # noqa: BLE001
                self._log("ERROR", f"Modbus RTU輪詢執行緒錯誤：{exc}")
            with self._state_lock:
                interval = self._poll_interval
            self._stop_event.wait(max(0.05, interval))

    def _read_point(self, device, point):
        point_type = self._point_type(point.get("type"))
        data_type = self._data_type(point.get("data_type"))
        address = self._int(point.get("address", 0), "address")
        count = max(1, self._int(point.get("count", 1), "count"))
        if point_type in self.REGISTER_TYPES and data_type in {"UInt32", "Int32", "Float"}:
            count = max(2, count)
            if count % 2:
                count += 1

        client = self._client(device)
        if client is None:
            return None
        station = self._station(device)
        readers = {
            "holding_register": client.read_holding_registers,
            "input_register": client.read_input_registers,
            "coil": client.read_coils,
            "discrete_input": client.read_discrete_inputs,
        }
        response = self._call_station(
            readers[point_type],
            station,
            address=address,
            count=count,
        )
        if self._is_error(response):
            raise RuntimeError(f"Modbus裝置回傳錯誤：{response}")

        if point_type in self.REGISTER_TYPES:
            registers = list(getattr(response, "registers", []) or [])[:count]
            if len(registers) < count:
                raise RuntimeError(f"暫存器不足，預期{count}，實際{len(registers)}。")
            value = self._decode_registers(registers, data_type)
        else:
            bits = list(getattr(response, "bits", []) or [])[:count]
            if len(bits) < count:
                raise RuntimeError(f"位元不足，預期{count}，實際{len(bits)}。")
            value = bool(bits[0]) if count == 1 else [bool(item) for item in bits]

        source_name = str(
            device.get("source_name")
            or device.get("connection_name")
            or device.get("port")
            or "Modbus RTU"
        )
        device_name = str(device.get("name") or "未命名裝置")
        point_name = str(point.get("name") or "未命名點位")
        point_key = self._make_key(source_name, device_name, point_name)
        return PointValue(
            point_key=point_key,
            protocol=self.PROTOCOL,
            source_name=source_name,
            device_name=device_name,
            point_name=point_name,
            address_text=f"{point_type}:{address}",
            value=value,
            value_text=self._text(value),
            value_number=self._number(value),
            status_text="OK",
            timestamp=datetime.now().astimezone(),
            writable=self._bool(point.get("writable", False), False),
            data_type=data_type,
            raw_config=dict(point),
        )

    def _find_point(self, target_key):
        with self._state_lock:
            devices = list(self._devices)
        for device in devices:
            source = str(device.get("source_name") or device.get("port") or "Modbus RTU")
            device_name = str(device.get("name") or "未命名裝置")
            for point in device.get("points", []):
                if not isinstance(point, dict):
                    continue
                point_name = str(point.get("name") or "未命名點位")
                if self._make_key(source, device_name, point_name) == target_key:
                    return device, point
        return None

    def _client(self, device):
        key = self._client_key(device)
        client = self._clients.get(key)
        try:
            if client is None:
                kwargs = {
                    "port": str(device.get("port", "")),
                    "baudrate": self._int(device.get("baudrate", 9600), "baudrate"),
                    "parity": str(device.get("parity", "N")).upper(),
                    "stopbits": self._int(device.get("stopbits", 1), "stopbits"),
                    "bytesize": self._int(device.get("bytesize", 8), "bytesize"),
                    "timeout": self._float(device.get("timeout", 1.0), "timeout"),
                }
                try:
                    client = ModbusSerialClient(method="rtu", **kwargs)
                except TypeError:
                    client = ModbusSerialClient(**kwargs)
                self._clients[key] = client
            if not bool(getattr(client, "connected", False)) and not bool(client.connect()):
                self._log("ERROR", f"無法連線序列埠{device.get('port', '')}。")
                return None
            return client
        except Exception as exc:  # noqa: BLE001
            self._log("ERROR", f"開啟序列埠{device.get('port', '')}失敗：{exc}")
            self._clients.pop(key, None)
            try:
                if client:
                    client.close()
            except Exception:  # noqa: BLE001
                pass
            return None

    def _client_key(self, device):
        return (
            str(device.get("port", "")),
            self._int(device.get("baudrate", 9600), "baudrate"),
            str(device.get("parity", "N")).upper(),
            self._int(device.get("stopbits", 1), "stopbits"),
            self._int(device.get("bytesize", 8), "bytesize"),
            self._float(device.get("timeout", 1.0), "timeout"),
        )

    def _close_clients(self):
        for client in list(self._clients.values()):
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001
                self._log("ERROR", f"關閉Modbus RTU客戶端失敗：{exc}")
        self._clients.clear()

    @staticmethod
    def _call_station(method: Callable[..., Any], station: int, **kwargs):
        """相容PyModbus的device_id、slave及unit參數。"""
        last_error = None
        for keyword in ("device_id", "slave", "unit"):
            try:
                return method(**kwargs, **{keyword: station})
            except TypeError as exc:
                last_error = exc
        raise last_error or RuntimeError("無法呼叫PyModbus方法。")

    @staticmethod
    def _is_error(response):
        if response is None:
            return True
        checker = getattr(response, "isError", None)
        return bool(checker()) if callable(checker) else False

    def _section(self):
        root = None
        for name in ("get_config", "as_dict", "get_all"):
            method = getattr(self.config_manager, name, None)
            if callable(method):
                try:
                    root = method()
                    break
                except Exception as exc:  # noqa: BLE001
                    self._log("ERROR", f"config_manager.{name}()失敗：{exc}")
        if root is None:
            for name in ("config", "data", "settings"):
                root = getattr(self.config_manager, name, None)
                if root is not None:
                    break
        if isinstance(root, dict):
            for key in ("modbus_rtu", "modbus", "MODBUS_RTU"):
                if key in root:
                    return root[key]
        getter = getattr(self.config_manager, "get", None)
        if callable(getter):
            for key in ("modbus_rtu", "modbus", "MODBUS_RTU"):
                try:
                    value = getter(key, None)
                except TypeError:
                    try:
                        value = getter(key)
                    except Exception:  # noqa: BLE001
                        continue
                if value is not None:
                    return value
        return {}

    def _normalize(self, section):
        section = {"devices": section} if isinstance(section, list) else dict(section or {})
        if "poll_interval_ms" in section:
            interval = self._float(section["poll_interval_ms"], "poll_interval_ms") / 1000.0
        else:
            interval = self._float(
                section.get("poll_interval", section.get("polling_interval", 1.0)),
                "poll_interval",
            )
        base = self._serial_values(section)
        devices = []

        sources = section.get("sources") or section.get("connections") or []
        if isinstance(sources, list):
            for source in sources:
                if not isinstance(source, dict):
                    continue
                serial = dict(base)
                serial.update(self._serial_values(source))
                source_name = str(source.get("name") or source.get("port") or "Modbus RTU")
                for device in source.get("devices", []):
                    if isinstance(device, dict):
                        devices.append(
                            self._device(
                                device,
                                serial,
                                source_name,
                                self._bool(source.get("enable", True), True),
                            )
                        )

        for device in section.get("devices", []):
            if isinstance(device, dict):
                source_name = str(
                    device.get("source_name")
                    or device.get("connection_name")
                    or device.get("port")
                    or section.get("name")
                    or "Modbus RTU"
                )
                devices.append(self._device(device, base, source_name, True))
        return devices, max(0.05, interval)

    def _device(self, device, serial, source_name, parent_enable):
        item = dict(serial)
        item.update(device)
        item["source_name"] = source_name
        item["enable"] = parent_enable and self._bool(device.get("enable", True), True)
        item["name"] = str(
            device.get("name")
            or device.get("device_name")
            or f"站號{self._station(device)}"
        )
        points = device.get("points", [])
        item["points"] = list(points) if isinstance(points, list) else []
        return item

    @staticmethod
    def _serial_values(config):
        values = dict(config.get("serial", {})) if isinstance(config.get("serial"), dict) else {}
        for key in ("port", "baudrate", "parity", "stopbits", "bytesize", "timeout"):
            if key in config:
                values[key] = config[key]
        return values

    @staticmethod
    def _station(device):
        for key in ("device_id", "slave", "unit", "station_id", "station"):
            if key in device:
                value = device[key]
                return int(value, 0) if isinstance(value, str) else int(value)
        return 1

    @classmethod
    def _point_type(cls, value):
        text = str(value or "holding_register").strip().lower()
        aliases = {
            "holding": "holding_register",
            "holding_registers": "holding_register",
            "hr": "holding_register",
            "input": "input_register",
            "input_registers": "input_register",
            "ir": "input_register",
            "coils": "coil",
            "discrete": "discrete_input",
            "discrete_inputs": "discrete_input",
            "di": "discrete_input",
        }
        text = aliases.get(text, text)
        if text not in cls.REGISTER_TYPES | cls.BIT_TYPES:
            raise ValueError(f"不支援的點位類型：{value}")
        return text

    @classmethod
    def _data_type(cls, value):
        text = str(value or "UInt16").strip()
        aliases = {
            "uint16": "UInt16",
            "int16": "Int16",
            "uint32": "UInt32",
            "int32": "Int32",
            "float": "Float",
            "float32": "Float",
            "bool": "Boolean",
            "boolean": "Boolean",
        }
        text = aliases.get(text.lower(), text)
        if text not in cls.DATA_TYPES:
            raise ValueError(f"不支援的data_type：{value}")
        return text

    def _decode_registers(self, registers: Sequence[int], data_type: str):
        width = 2 if data_type in {"UInt32", "Int32", "Float"} else 1
        if len(registers) % width:
            raise ValueError(f"{data_type}需要每組{width}個register。")
        values = [
            self._decode_group(registers[index : index + width], data_type)
            for index in range(0, len(registers), width)
        ]
        return values[0] if len(values) == 1 else values

    @staticmethod
    def _decode_group(registers, data_type):
        first = int(registers[0]) & 0xFFFF
        if data_type == "UInt16":
            return first
        if data_type == "Int16":
            return first - 0x10000 if first & 0x8000 else first
        if data_type == "Boolean":
            return bool(first)
        second = int(registers[1]) & 0xFFFF
        combined = (first << 16) | second
        if data_type == "UInt32":
            return combined
        if data_type == "Int32":
            return combined - 0x100000000 if combined & 0x80000000 else combined
        return struct.unpack(">f", struct.pack(">HH", first, second))[0]

    def _encode_registers(self, value_text, data_type):
        text = str(value_text).strip()
        if data_type == "Boolean":
            return [1 if self._parse_bool(text) else 0]
        if data_type == "Float":
            return list(struct.unpack(">HH", struct.pack(">f", float(text))))
        value = int(text, 0)
        ranges = {
            "UInt16": (0, 0xFFFF),
            "Int16": (-0x8000, 0x7FFF),
            "UInt32": (0, 0xFFFFFFFF),
            "Int32": (-0x80000000, 0x7FFFFFFF),
        }
        minimum, maximum = ranges[data_type]
        if not minimum <= value <= maximum:
            raise ValueError(f"{data_type}數值必須介於{minimum}到{maximum}。")
        if data_type in {"UInt16", "Int16"}:
            return [value & 0xFFFF]
        value &= 0xFFFFFFFF
        return [(value >> 16) & 0xFFFF, value & 0xFFFF]

    @staticmethod
    def _parse_bool(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"1", "true", "on", "yes", "y", "開", "啟用"}:
            return True
        if text in {"0", "false", "off", "no", "n", "關", "停用"}:
            return False
        raise ValueError(f"無法轉換為Boolean：{value}")

    @classmethod
    def _bool(cls, value, default):
        if value is None:
            return default
        try:
            return cls._parse_bool(value)
        except ValueError:
            return default

    @staticmethod
    def _int(value, name):
        try:
            return int(value, 0) if isinstance(value, str) else int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}必須是整數：{value}") from exc

    @staticmethod
    def _float(value, name):
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}必須是數字：{value}") from exc

    @staticmethod
    def _text(value):
        if isinstance(value, bool):
            return "True" if value else "False"
        return json.dumps(value, ensure_ascii=False) if isinstance(value, list) else str(value)

    @staticmethod
    def _number(value):
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        return float(value) if isinstance(value, (int, float)) else None

    @staticmethod
    def _make_key(source_name, device_name, point_name):
        """優先使用專案標準介面，並相容兩參數或三參數版本。"""
        attempts = (
            lambda: make_modbus_point_key(device_name, point_name),
            lambda: make_modbus_point_key(source_name, device_name, point_name),
            lambda: make_modbus_point_key(device_name=device_name, point_name=point_name),
            lambda: make_modbus_point_key(
                source_name=source_name,
                device_name=device_name,
                point_name=point_name,
            ),
        )
        last_error = None
        for attempt in attempts:
            try:
                return str(attempt())
            except TypeError as exc:
                last_error = exc
        raise last_error or RuntimeError("無法建立Modbus點位鍵值。")

    def _log(self, level, message):
        if self.log_callback is None:
            return
        try:
            self.log_callback(f"[{level}] {message}")
        except TypeError:
            try:
                self.log_callback(level, message)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
