"""Modbus RTU多站輪詢、讀取與寫入管理。"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .data_model import PointValue, make_modbus_point_key
from .gateway_security import ReadonlyGatewayPolicy
from .modbus_codec import decode_modbus_value, register_count_for_type

PROTOCOL_MODBUS = "MODBUS_RTU"


class ModbusRtuManager:
    """使用單一序列埠輪詢多個Modbus RTU站號。

    修正重點：
    - 所有Modbus讀取、寫入、寫入後read-back與close都使用同一把_io_lock。
    - stop_polling逾時時保留舊thread引用，避免再次啟動第二個輪詢thread。
    - 輪詢迴圈在stop_event被設定後盡快中止，降低關閉卡住機率。
    """

    protocol = PROTOCOL_MODBUS

    def __init__(
        self,
        config_manager,
        value_bus,
        log_func=None,
        client_factory=None,
    ):
        self.config_manager = config_manager
        self.value_bus = value_bus
        self.log_func = log_func
        if client_factory is not None and not callable(client_factory):
            raise TypeError("client_factory必須可呼叫")
        self._client_factory = client_factory
        self._readonly_policy = ReadonlyGatewayPolicy(self._log)
        self._state_lock = threading.RLock()
        self._io_lock = threading.RLock()
        self._publication_lock = threading.RLock()
        self._reload_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = None
        self._config: dict[str, Any] = {}
        self._points: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self._config_generation = 0
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
        try:
            return make_modbus_point_key(
                source,
                int(device.get("station_id", 1)),
                str(point.get("type", "holding_register")),
                int(point.get("address", 0)),
                str(point.get("name", "")),
            )
        except (TypeError, ValueError):
            return "::".join(
                (
                    "MODBUS_RTU_CONFIG_ERROR",
                    quote(source, safe=""),
                    quote(str(device.get("station_id", 1)), safe=""),
                    quote(str(point.get("type", "holding_register")), safe=""),
                    quote(str(point.get("address", 0)), safe=""),
                    quote(str(point.get("name", "")), safe=""),
                )
            )

    def reload_config(self):
        with self._reload_lock:
            return self._reload_config_locked()

    def _reload_config_locked(self):
        with self._state_lock:
            old_keys = set(self._points)
        # 偶數代表完成態，奇數代表 reload 中或上次 reload 失敗。
        # 重試失敗的 reload 時維持奇數，成功 swap 後再明確回到偶數。
        with self._publication_lock:
            with self._state_lock:
                if self._config_generation % 2 == 0:
                    self._config_generation += 1
        was_running = self.is_running()
        if was_running:
            stop_result = self.stop_polling()
            if isinstance(stop_result, str) and "逾時" in stop_result:
                raise RuntimeError("Modbus輪詢尚未安全停止，無法重新載入設定。")

        new_config = self._config_snapshot()

        def apply_snapshot() -> None:
            new_points: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
            with self._state_lock:
                old_config = self._config
                self._config = new_config
                try:
                    devices = self._config.get("devices", [])
                    for device in devices:
                        if not isinstance(device, Mapping):
                            continue
                        for point in device.get("points", []):
                            if isinstance(point, Mapping):
                                key = self._point_key(device, point)
                                if key in new_points:
                                    raise ValueError(f"Modbus point_key重複：{key}")
                                new_points[key] = (dict(device), dict(point))
                except Exception:
                    self._config = old_config
                    raise
                self._points = new_points

        # 以 io->state 鎖序讓一次性讀取不會在設定切換期間重建舊 Transport。
        # 完成後再次遞增 generation，讓 reload 進行中取得舊 config 的 reader
        # 也必定失效，而不只是在 reload 開始前已取得 snapshot 的 reader。
        with self._io_lock:
            self._close_client()
            apply_snapshot()
            with self._publication_lock:
                with self._state_lock:
                    if self._config_generation % 2 != 0:
                        self._config_generation += 1

        retired_keys = old_keys.difference(self._points)
        remove_many = getattr(self.value_bus, "remove_many", None)
        if retired_keys and callable(remove_many):
            remove_many(retired_keys)

        if was_running and self._as_bool(self._config.get("enable", self._config.get("enabled")), False):
            self.start_polling()
        return {"point_count": len(self._points), "running": self.is_running()}

    def _make_client(self):
        if self._client_factory is not None:
            return self._client_factory()
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

    def _ensure_device_client_unlocked(self, device: Mapping[str, Any]):
        """取得指定 Device 的 Transport；RTU Device 共用單一序列 Client。"""
        return self._ensure_client_unlocked()

    def _discard_device_client_unlocked(self, device: Mapping[str, Any]) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                self._log(f"關閉Modbus序列埠時發生錯誤：{exc}", "WARNING")

    def _discard_device_client(self, device: Mapping[str, Any]) -> None:
        with self._io_lock:
            self._discard_device_client_unlocked(device)

    def _close_client(self) -> None:
        with self._io_lock:
            self._discard_device_client_unlocked({})

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
    def _decode(raw_values: list[Any], data_type: str, point_type: str):
        return decode_modbus_value(
            raw_values,
            data_type,
            area=point_type,
        )

    @staticmethod
    def _point_area(point: Mapping[str, Any]) -> str:
        area = str(point.get("type", "holding_register")).strip().lower()
        if area not in {
            "coil",
            "discrete_input",
            "input_register",
            "holding_register",
        }:
            raise ValueError(f"不支援的Modbus資料區：{area}")
        return area

    @classmethod
    def _point_count(cls, point: Mapping[str, Any]) -> int:
        area = cls._point_area(point)
        required = register_count_for_type(
            point.get("data_type", "Auto"),
            area=area,
        )
        configured = int(point.get("count", required))
        if configured < required:
            raise ValueError(
                f"{point.get('data_type', 'Auto')}至少需要{required}個值"
            )
        limit = 2000 if area in {"coil", "discrete_input"} else 125
        if configured > limit:
            raise ValueError(f"單次Modbus讀取數量不可超過{limit}")
        return configured

    @classmethod
    def _point_record(
        cls,
        point: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], int, int]:
        cls._point_area(point)
        if int(point.get("address_base", 0)) != 0:
            raise ValueError("Modbus來源只接受0-based PDU address")
        address = int(point.get("address", 0))
        if not 0 <= address <= 65535:
            raise ValueError("Modbus PDU address必須介於0到65535")
        count = cls._point_count(point)
        if address + count > 65536:
            raise ValueError("Modbus PDU address加count不可超過65536")
        return point, address, count

    @classmethod
    def _read_groups(
        cls,
        points: list[Mapping[str, Any]],
    ) -> list[tuple[str, int, int, list[tuple[Mapping[str, Any], int, int]]]]:
        by_area: dict[str, list[tuple[Mapping[str, Any], int, int]]] = {}
        for point in points:
            record = cls._point_record(point)
            by_area.setdefault(cls._point_area(point), []).append(record)

        groups = []
        for area, records in by_area.items():
            limit = 2000 if area in {"coil", "discrete_input"} else 125
            current: list[tuple[Mapping[str, Any], int, int]] = []
            start = end = 0
            for record in sorted(records, key=lambda item: item[1]):
                _, address, count = record
                record_end = address + count
                if current and (address > end or record_end - start > limit):
                    groups.append((area, start, end - start, current))
                    current = []
                if not current:
                    start, end = address, record_end
                else:
                    end = max(end, record_end)
                current.append(record)
            if current:
                groups.append((area, start, end - start, current))
        return groups

    @staticmethod
    def _decode_point(raw_values, point: Mapping[str, Any]) -> Any:
        return decode_modbus_value(
            list(raw_values),
            point.get("data_type", "Auto"),
            area=str(point.get("type", "holding_register")),
            byte_order=point.get("byte_order"),
            word_order=point.get("word_order"),
        )

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

    def _source_name(self, device: Mapping[str, Any]) -> str:
        return str(self._config.get("port", ""))

    def _connection_id(
        self,
        device: Mapping[str, Any],
        point: Mapping[str, Any],
    ) -> str:
        source_name = self._source_name(device)
        return str(
            point.get("connection_id", "")
            or device.get("connection_id", "")
            or self._config.get("connection_id", "")
            or f"modbus-rtu-connection:{source_name}"
        )

    def _device_id(
        self,
        device: Mapping[str, Any],
        point: Mapping[str, Any],
    ) -> str:
        source_name = self._source_name(device)
        return str(
            point.get("device_id", "")
            or device.get("device_id", "")
            or f"modbus-rtu-device:{source_name}:{device.get('station_id', 1)}"
        )

    def _source_metadata(
        self,
        device: Mapping[str, Any],
        point: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "station_id": device.get("station_id", 1),
            "serial_port": self._source_name(device),
            "device_name": str(device.get("name", "")),
        }

    def _address_text(
        self,
        device: Mapping[str, Any],
        point: Mapping[str, Any],
    ) -> str:
        return (
            f"站號{device.get('station_id', 1)} "
            f"{point.get('type', '')} {point.get('address', 0)}"
        )

    def _publish(
        self,
        device: Mapping[str, Any],
        point: Mapping[str, Any],
        value: Any,
        status: str,
        *,
        source_timestamp: datetime | None = None,
        server_timestamp: datetime | None = None,
        diagnostics: Mapping[str, Any] | None = None,
    ):
        key = self._point_key(device, point)
        source_time = source_timestamp or datetime.now(timezone.utc)
        gateway_time = server_timestamp or datetime.now(timezone.utc)
        area = str(point.get("type", "holding_register")).strip().lower()
        raw_config = dict(point)
        raw_config.update(
            {
                **self._source_metadata(device, point),
                "source_area": area,
                "pdu_address": point.get("address", 0),
                "address_base": 0,
                "byte_order": str(point.get("byte_order", "big")),
                "word_order": str(point.get("word_order", "big")),
            }
        )
        if diagnostics:
            raw_config.update(dict(diagnostics))
        point_value = PointValue(
            point_key=key,
            protocol=self.protocol,
            source_name=self._source_name(device),
            device_name=str(device.get("name", "")),
            point_name=str(point.get("name", "")),
            address_text=self._address_text(device, point),
            value=value,
            value_text=self._value_text(value),
            value_number=self._value_number(value),
            status_text=status,
            timestamp=source_time,
            writable=self._as_bool(point.get("writable", False), False),
            data_type=str(point.get("data_type", "Auto")),
            raw_config=raw_config,
            tag_id=str(point.get("tag_id", "") or key),
            connection_id=self._connection_id(device, point),
            device_id=self._device_id(device, point),
            quality="Good" if status == "Good" else "Bad",
            source_timestamp=source_time,
            server_timestamp=gateway_time,
            gateway_timestamp=gateway_time,
        )
        self.value_bus.publish(point_value)
        return point_value

    def _configuration_generation(self):
        return self._config_generation

    def _configuration_is_current(self, generation) -> bool:
        return (
            generation == self._config_generation
            and generation % 2 == 0
        )

    def _publish_if_current(
        self,
        generation,
        device: Mapping[str, Any],
        point: Mapping[str, Any],
        value: Any,
        status: str,
        **kwargs,
    ) -> bool:
        """只在 snapshot 仍有效時發布，與 reload 的 generation 切換互斥。"""
        with self._publication_lock:
            with self._state_lock:
                if not self._configuration_is_current(generation):
                    return False
            self._publish(device, point, value, status, **kwargs)
        return True

    def read_all_once(self):
        success = 0
        failed = 0
        with self._state_lock:
            devices = list(self._config.get("devices", []))
            generation = self._configuration_generation()

        for device in devices:
            if self._stop_event.is_set():
                break
            if not isinstance(device, Mapping) or not self._as_bool(
                device.get("enable", True), True
            ):
                continue

            points = [
                point
                for point in device.get("points", [])
                if isinstance(point, Mapping)
                and self._as_bool(point.get("enable", True), True)
            ]
            if not points:
                continue

            valid_points = []
            for point in points:
                try:
                    self._point_record(point)
                    valid_points.append(point)
                except Exception as exc:
                    failure_time = datetime.now(timezone.utc)
                    if not self._publish_if_current(
                        generation,
                        device,
                        point,
                        None,
                        f"設定錯誤：{exc}",
                        source_timestamp=failure_time,
                        server_timestamp=failure_time,
                        diagnostics={"error": str(exc)},
                    ):
                        return {
                            "success": success,
                            "failed": failed,
                            "total": success + failed,
                        }
                    self._log(
                        f"{self.protocol}點位「{point.get('name', '')}」"
                        f"設定錯誤：{exc}",
                        "ERROR",
                    )
                    failed += 1

            if not valid_points:
                continue

            try:
                with self._io_lock:
                    with self._state_lock:
                        if not self._configuration_is_current(generation):
                            break
                    self._ensure_device_client_unlocked(device)
            except Exception as exc:
                with self._state_lock:
                    if not self._configuration_is_current(generation):
                        return {
                            "success": success,
                            "failed": failed,
                            "total": success + failed,
                        }
                self._discard_device_client(device)
                failure_time = datetime.now(timezone.utc)
                for point in valid_points:
                    if not self._publish_if_current(
                        generation,
                        device,
                        point,
                        None,
                        f"連線失敗：{exc}",
                        source_timestamp=failure_time,
                        server_timestamp=datetime.now(timezone.utc),
                        diagnostics={"error": str(exc)},
                    ):
                        return {
                            "success": success,
                            "failed": failed,
                            "total": success + failed,
                        }
                self._log(
                    f"{self.protocol}裝置「{device.get('name', '')}」連線失敗：{exc}",
                    "ERROR",
                )
                failed += len(valid_points)
                continue

            for area, response_address, response_count, records in self._read_groups(
                valid_points
            ):
                if self._stop_event.is_set():
                    break
                try:
                    with self._io_lock:
                        with self._state_lock:
                            if not self._configuration_is_current(generation):
                                return {
                                    "success": success,
                                    "failed": failed,
                                    "total": success + failed,
                                }
                        client = self._ensure_device_client_unlocked(device)
                        raw = self._read_raw_unlocked(
                            client,
                            device,
                            {
                                "type": area,
                                "address": response_address,
                                "count": response_count,
                            },
                        )
                    source_timestamp = datetime.now(timezone.utc)
                except Exception as exc:
                    with self._state_lock:
                        if not self._configuration_is_current(generation):
                            return {
                                "success": success,
                                "failed": failed,
                                "total": success + failed,
                            }
                    self._discard_device_client(device)
                    failure_time = datetime.now(timezone.utc)
                    for point, _, _ in records:
                        if not self._publish_if_current(
                            generation,
                            device,
                            point,
                            None,
                            f"讀取失敗：{exc}",
                            source_timestamp=failure_time,
                            server_timestamp=datetime.now(timezone.utc),
                            diagnostics={
                                "response_address": response_address,
                                "response_count": response_count,
                                "error": str(exc),
                            },
                        ):
                            return {
                                "success": success,
                                "failed": failed,
                                "total": success + failed,
                            }
                    self._log(
                        f"{self.protocol}裝置「{device.get('name', '')}」資料區"
                        f"「{area}」讀取失敗：{exc}",
                        "ERROR",
                    )
                    failed += len(records)
                    continue

                for point, address, count in records:
                    offset = address - response_address
                    point_raw = raw[offset : offset + count]
                    try:
                        value = self._decode_point(point_raw, point)
                        if not self._publish_if_current(
                            generation,
                            device,
                            point,
                            value,
                            "Good",
                            source_timestamp=source_timestamp,
                            server_timestamp=datetime.now(timezone.utc),
                            diagnostics={
                                "response_address": response_address,
                                "response_count": response_count,
                                "raw_values": list(point_raw),
                            },
                        ):
                            return {
                                "success": success,
                                "failed": failed,
                                "total": success + failed,
                            }
                        success += 1
                    except Exception as exc:
                        if not self._publish_if_current(
                            generation,
                            device,
                            point,
                            None,
                            f"解碼失敗：{exc}",
                            source_timestamp=source_timestamp,
                            server_timestamp=datetime.now(timezone.utc),
                            diagnostics={
                                "response_address": response_address,
                                "response_count": response_count,
                                "raw_values": list(point_raw),
                                "error": str(exc),
                            },
                        ):
                            return {
                                "success": success,
                                "failed": failed,
                                "total": success + failed,
                            }
                        self._log(
                            f"{self.protocol}點位「{point.get('name', '')}」"
                            f"解碼失敗：{exc}",
                            "ERROR",
                        )
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
        # thread已確認停止，清除舊的取消狀態，讓「讀取一次」可再次使用。
        self._stop_event.clear()
        return "Modbus輪詢已停止"

    def is_running(self) -> bool:
        with self._state_lock:
            return bool(self._thread and self._thread.is_alive())

    def write_point(self, point_key, value_text):
        self._reject_write_point(point_key, value_text, PROTOCOL_MODBUS)

    def _reject_write_point(self, point_key, value_text, protocol):
        """相容舊公開入口，但在任何 Transport I/O 前固定拒絕。"""
        with self._state_lock:
            item = self._points.get(str(point_key))
        device, point = item if item is not None else ({}, {})
        self._readonly_policy.reject_write(
            protocol=protocol,
            client="local-api",
            target=(
                f"{device.get('name', 'unknown')}/"
                f"{point.get('name', point_key)}"
            ),
            address=(
                f"{point.get('type', 'unknown')}:"
                f"{point.get('address', 'unknown')}"
            ),
            request_type="write_point",
            requested_value=value_text,
        )
