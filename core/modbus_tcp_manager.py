"""Modbus TCP多PLC通訊、輪詢與點位讀寫管理器。"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .data_model import PointValue, make_modbus_tcp_point_key
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
        return make_modbus_tcp_point_key(
            source,
            int(device.get("station_id", 1)),
            str(point.get("type", "holding_register")),
            int(point.get("address", 0)),
            str(point.get("name", "")),
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
    ):
        endpoint = self._endpoint(device)
        raw_config = dict(point)
        raw_config.update(
            {
                "station_id": int(device.get("station_id", 1)),
                "host": self._device_host(device),
                "port": self._device_port(device),
                "device_name": str(device.get("name", "")),
            }
        )
        point_value = PointValue(
            point_key=self._point_key(device, point),
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

            for point in points:
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
                    self._log(
                        f"Modbus TCP「{self._endpoint(device)}」點位"
                        f"「{point.get('name', '')}」讀取失敗：{exc}",
                        "ERROR",
                    )
                    failed += 1
        return {"success": success, "failed": failed, "total": success + failed}

    def write_point(self, point_key, value_text):
        try:
            with self._io_lock:
                with self._state_lock:
                    item = self._points.get(str(point_key))
                if item is None:
                    raise KeyError(f"找不到Modbus TCP點位：{point_key}")

                device, point = item
                if not self._as_bool(point.get("writable", False), False):
                    raise PermissionError(
                        f"Modbus TCP點位不可寫入："
                        f"{point.get('name', point_key)}"
                    )

                point_type = str(
                    point.get("type", "holding_register")
                ).lower()
                if point_type not in {"holding_register", "coil"}:
                    raise ValueError(
                        f"{point_type}不支援寫入，"
                        "僅holding_register與coil可寫入"
                    )

                station = int(device.get("station_id", 1))
                address = int(point.get("address", 0))
                count = max(1, int(point.get("count", 1)))
                client = self._ensure_client_unlocked(device)
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
                    response = self._write_registers_unlocked(
                        client, station, address, registers
                    )
                self._response_error(response)
                raw = self._read_raw_unlocked(client, device, point)
        except Exception:
            if "device" in locals():
                self._discard_client(device)
            raise

        value = self._decode(
            raw,
            str(point.get("data_type", "Auto")),
            point_type,
        )
        self._publish(device, point, value, "WriteGood")
        return True

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
