"""Modbus TCP多PLC通訊、輪詢與點位讀寫管理器。"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .data_model import PointValue, make_modbus_tcp_point_key
from .modbus_codec import decode_modbus_value, register_count_for_type
from .modbus_manager import ModbusRtuManager

PROTOCOL_MODBUS_TCP = "MODBUS_TCP"


class ModbusTcpManager(ModbusRtuManager):
    """每台PLC使用各自的IP、TCP Port與Unit ID。

    相同IP與Port的裝置會共用一個TCP Client，讓TCP Gateway後方的多個
    Unit ID仍可使用；不同端點則各自建立及維護Client。
    """

    def __init__(self, config_manager, value_bus, log_func=None):
        self._clients: dict[tuple[str, int], Any] = {}
        self._config_generation = 0
        super().__init__(config_manager, value_bus, log_func)

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

    def reload_config(self):
        old_keys = set(getattr(self, "_points", {}))
        with self._state_lock:
            self._config_generation += 1

        # 非輪詢狀態下super().reload_config()不會關閉Client，需主動清理舊端點，
        # 並以io->state鎖序避免寫入使用舊端點重建Client。
        if not self.is_running():
            with self._io_lock:
                self._close_client()
                result = super().reload_config()
        else:
            result = super().reload_config()

        retired_keys = old_keys.difference(self._points)
        remove_many = getattr(self.value_bus, "remove_many", None)
        if retired_keys and callable(remove_many):
            remove_many(retired_keys)
        return result

    def _device_host(self, device: Mapping[str, Any]) -> str:
        # fallback保留舊版「全域host/port」設定的讀取相容性。
        return str(
            device.get("host", device.get("ip", self._config.get("host", "127.0.0.1")))
            or ""
        ).strip()

    def _device_port(self, device: Mapping[str, Any]) -> int:
        return int(device.get("port", self._config.get("port", 502)))

    def _endpoint(self, device: Mapping[str, Any]) -> str:
        host = self._device_host(device)
        display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return f"{display_host}:{self._device_port(device)}"

    def _client_key(self, device: Mapping[str, Any]) -> tuple[str, int]:
        host = self._device_host(device)
        if not host:
            raise ValueError(f"PLC「{device.get('name', '')}」未設定主機/IP")
        port = self._device_port(device)
        if not 1 <= port <= 65535:
            raise ValueError(f"PLC「{device.get('name', '')}」TCP Port必須介於1到65535")
        return host.casefold(), port

    def _point_key(self, device: Mapping[str, Any], point: Mapping[str, Any]) -> str:
        source = f"{self._endpoint(device)}|{device.get('name', '')}"
        try:
            return make_modbus_tcp_point_key(
                source,
                int(device.get("station_id", 1)),
                str(point.get("type", "holding_register")),
                int(point.get("address", 0)),
                str(point.get("name", "")),
            )
        except (TypeError, ValueError):
            return "::".join(
                (
                    "MODBUS_TCP_CONFIG_ERROR",
                    quote(source, safe=""),
                    quote(str(device.get("station_id", 1)), safe=""),
                    quote(str(point.get("type", "holding_register")), safe=""),
                    quote(str(point.get("address", 0)), safe=""),
                    quote(str(point.get("name", "")), safe=""),
                )
            )

    def _make_client(self, device: Mapping[str, Any]):
        try:
            from pymodbus.client import ModbusTcpClient
        except ImportError as exc:
            raise RuntimeError(
                "尚未安裝pymodbus，請先執行pip install -r requirements.txt"
            ) from exc

        return ModbusTcpClient(
            host=self._device_host(device),
            port=self._device_port(device),
            timeout=float(self._config.get("timeout", 3.0)),
        )

    def _ensure_client_unlocked(self, device: Mapping[str, Any]):
        key = self._client_key(device)
        client = self._clients.get(key)
        if client is None:
            client = self._make_client(device)
            self._clients[key] = client
        connected = client.connect()
        if connected is False:
            self._discard_client_unlocked(device)
            raise ConnectionError(f"無法連線Modbus TCP：{self._endpoint(device)}")
        return client

    def _discard_client_unlocked(self, device: Mapping[str, Any]) -> None:
        try:
            key = self._client_key(device)
        except (TypeError, ValueError):
            return
        client = self._clients.pop(key, None)
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                self._log(
                    f"關閉Modbus TCP Client「{self._endpoint(device)}」失敗：{exc}",
                    "WARNING",
                )

    def _discard_client(self, device: Mapping[str, Any]) -> None:
        with self._io_lock:
            self._discard_client_unlocked(device)

    def _close_client(self) -> None:
        with self._io_lock:
            clients, self._clients = self._clients, {}
            self._client = None
            for (host, port), client in clients.items():
                try:
                    client.close()
                except Exception as exc:
                    self._log(
                        f"關閉Modbus TCP Client「{host}:{port}」失敗：{exc}",
                        "WARNING",
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
        endpoint = self._endpoint(device)
        point_key = self._point_key(device, point)
        tag_id = str(point.get("tag_id", "") or point_key)
        connection_id = str(
            point.get("connection_id", "")
            or device.get("connection_id", "")
            or f"modbus-tcp-connection:{endpoint}"
        )
        device_id = str(
            point.get("device_id", "")
            or device.get("device_id", "")
            or f"modbus-tcp-device:{endpoint}:{device.get('station_id', 1)}"
        )
        area = str(point.get("type", "holding_register")).strip().lower()
        source_time = source_timestamp or datetime.now(timezone.utc)
        gateway_time = server_timestamp or datetime.now(timezone.utc)
        raw_config = dict(point)
        raw_config.update(
            {
                "station_id": int(device.get("station_id", 1)),
                "host": self._device_host(device),
                "port": self._device_port(device),
                "device_name": str(device.get("name", "")),
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
            point_key=point_key,
            protocol=PROTOCOL_MODBUS_TCP,
            source_name=endpoint,
            device_name=str(device.get("name", "")),
            point_name=str(point.get("name", "")),
            address_text=(
                f"{endpoint} Unit ID {device.get('station_id', 1)} "
                f"{point.get('type', '')} {point.get('address', 0)}"
            ),
            value=value,
            value_text=self._value_text(value),
            value_number=self._value_number(value),
            status_text=status,
            timestamp=source_time,
            writable=self._as_bool(point.get("writable", False), False),
            data_type=str(point.get("data_type", "Auto")),
            raw_config=raw_config,
            tag_id=tag_id,
            connection_id=connection_id,
            device_id=device_id,
            quality="Good" if status == "Good" else "Bad",
            source_timestamp=source_time,
            server_timestamp=gateway_time,
            gateway_timestamp=gateway_time,
        )
        self.value_bus.publish(point_value)
        return point_value

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
        required = register_count_for_type(
            point.get("data_type", "Auto"),
            area=cls._point_area(point),
        )
        configured = int(point.get("count", required))
        if configured < required:
            raise ValueError(
                f"{point.get('data_type', 'Auto')}至少需要{required}個值"
            )
        limit = 2000 if cls._point_area(point) in {"coil", "discrete_input"} else 125
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

    def read_all_once(self):
        success = 0
        failed = 0
        with self._state_lock:
            devices = list(self._config.get("devices", []))
            generation = self._config_generation

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

            try:
                with self._io_lock:
                    with self._state_lock:
                        if generation != self._config_generation:
                            break
                    client = self._ensure_client_unlocked(device)
            except Exception as exc:
                self._discard_client(device)
                for point in points:
                    self._publish(device, point, None, f"連線失敗：{exc}")
                self._log(
                    f"Modbus TCP PLC「{device.get('name', '')}」"
                    f"（{self._endpoint(device)}）連線失敗：{exc}",
                    "ERROR",
                )
                failed += len(points)
                continue

            valid_points = []
            for point in points:
                try:
                    self._point_record(point)
                    valid_points.append(point)
                except Exception as exc:
                    failure_time = datetime.now(timezone.utc)
                    self._publish(
                        device,
                        point,
                        None,
                        f"設定錯誤：{exc}",
                        source_timestamp=failure_time,
                        server_timestamp=failure_time,
                        diagnostics={"error": str(exc)},
                    )
                    self._log(
                        f"Modbus TCP點位「{point.get('name', '')}」"
                        f"設定錯誤：{exc}",
                        "ERROR",
                    )
                    failed += 1
            groups = self._read_groups(valid_points)

            for area, response_address, response_count, records in groups:
                if self._stop_event.is_set():
                    break
                try:
                    with self._io_lock:
                        with self._state_lock:
                            if generation != self._config_generation:
                                return {
                                    "success": success,
                                    "failed": failed,
                                    "total": success + failed,
                                }
                        client = self._ensure_client_unlocked(device)
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
                    failed_time = datetime.now(timezone.utc)
                    for point, _, _ in records:
                        self._publish(
                            device,
                            point,
                            None,
                            f"讀取失敗：{exc}",
                            source_timestamp=failed_time,
                            server_timestamp=datetime.now(timezone.utc),
                            diagnostics={
                                "response_address": response_address,
                                "response_count": response_count,
                                "error": str(exc),
                            },
                        )
                    self._log(
                        f"Modbus TCP「{self._endpoint(device)}」資料區"
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
                        gateway_timestamp = datetime.now(timezone.utc)
                        self._publish(
                            device,
                            point,
                            value,
                            "Good",
                            source_timestamp=source_timestamp,
                            server_timestamp=gateway_timestamp,
                            diagnostics={
                                "response_address": response_address,
                                "response_count": response_count,
                                "raw_values": list(point_raw),
                            },
                        )
                        success += 1
                    except Exception as exc:
                        self._publish(
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
                        )
                        self._log(
                            f"Modbus TCP點位「{point.get('name', '')}」"
                            f"解碼失敗：{exc}",
                            "ERROR",
                        )
                        failed += 1
        return {"success": success, "failed": failed, "total": success + failed}

    def write_point(self, point_key, value_text):
        self._reject_write_point(point_key, value_text, PROTOCOL_MODBUS_TCP)

    def start_polling(self):
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                if self._stop_event.is_set():
                    return "Modbus TCP輪詢正在停止，請稍後再啟動"
                return "Modbus TCP輪詢已在執行"
            self._thread = None
            if not self._as_bool(
                self._config.get("enable", self._config.get("enabled")), False
            ):
                raise RuntimeError("config.json尚未啟用modbus_tcp.enable")
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._poll_loop,
                name="ModbusTcpPolling",
                daemon=True,
            )
            self._thread.start()
        return "Modbus TCP輪詢已啟動"
