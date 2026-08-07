"""Modbus TCP多PLC通訊、輪詢與點位讀寫管理器。"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from .data_model import make_modbus_tcp_point_key
from .modbus_manager import ModbusRtuManager

PROTOCOL_MODBUS_TCP = "MODBUS_TCP"


class ModbusTcpManager(ModbusRtuManager):
    """每台PLC使用各自的IP、TCP Port與Unit ID。

    相同IP與Port的裝置會共用一個TCP Client，讓TCP Gateway後方的多個
    Unit ID仍可使用；不同端點則各自建立及維護Client。點位規劃、解碼、
    Canonical 發布與失敗隔離則和 RTU 共用同一套來源流程。
    """

    protocol = PROTOCOL_MODBUS_TCP

    def __init__(self, config_manager, value_bus, log_func=None):
        self._clients: dict[tuple[str, int], Any] = {}
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

    def _ensure_device_client_unlocked(self, device: Mapping[str, Any]):
        return self._ensure_client_unlocked(device)

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

    def _discard_device_client_unlocked(self, device: Mapping[str, Any]) -> None:
        self._discard_client_unlocked(device)

    def _discard_client(self, device: Mapping[str, Any]) -> None:
        self._discard_device_client(device)

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

    def _source_name(self, device: Mapping[str, Any]) -> str:
        return self._endpoint(device)

    def _connection_id(
        self,
        device: Mapping[str, Any],
        point: Mapping[str, Any],
    ) -> str:
        endpoint = self._endpoint(device)
        return str(
            point.get("connection_id", "")
            or device.get("connection_id", "")
            or f"modbus-tcp-connection:{endpoint}"
        )

    def _device_id(
        self,
        device: Mapping[str, Any],
        point: Mapping[str, Any],
    ) -> str:
        endpoint = self._endpoint(device)
        return str(
            point.get("device_id", "")
            or device.get("device_id", "")
            or f"modbus-tcp-device:{endpoint}:{device.get('station_id', 1)}"
        )

    def _source_metadata(
        self,
        device: Mapping[str, Any],
        point: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "station_id": device.get("station_id", 1),
            "host": self._device_host(device),
            "port": self._device_port(device),
            "device_name": str(device.get("name", "")),
        }

    def _address_text(
        self,
        device: Mapping[str, Any],
        point: Mapping[str, Any],
    ) -> str:
        return (
            f"{self._endpoint(device)} Unit ID {device.get('station_id', 1)} "
            f"{point.get('type', '')} {point.get('address', 0)}"
        )

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
