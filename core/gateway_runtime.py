"""兩種唯讀輸出 Server 的產品生命週期整合。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from typing import Any

from asyncua import ua

from .gateway_modbus_adapter import GatewayModbusOutputAdapter
from .gateway_modbus_server import GatewayModbusTcpServer
from .gateway_opcua_adapter import GatewayOpcuaOutputAdapter
from .gateway_opcua_server import GatewayOpcuaServer


class GatewayOutputRuntime:
    """依設定啟停 Modbus TCP／OPC UA 輸出並保證完整清理。"""

    def __init__(self, config_manager, log_func=None, value_bus=None) -> None:
        self.config_manager = config_manager
        self.log_callback = log_func
        self.value_bus = value_bus
        self.modbus_server: GatewayModbusTcpServer | None = None
        self.modbus_adapter: GatewayModbusOutputAdapter | None = None
        self.opcua_server: GatewayOpcuaServer | None = None
        self.opcua_adapter: GatewayOpcuaOutputAdapter | None = None
        self.opcua_system_node_id: ua.NodeId | None = None
        self._opcua_loop: asyncio.AbstractEventLoop | None = None
        self._opcua_thread: threading.Thread | None = None
        self._opcua_ready = threading.Event()
        self._opcua_error: BaseException | None = None
        self._lock = threading.RLock()
        self._running = False

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            config = self._config_snapshot()
            modbus = self._section(config, "modbus_tcp_server")
            opcua = self._section(config, "opcua_server")

            try:
                if self._enabled(modbus):
                    self.modbus_server = GatewayModbusTcpServer(
                        host=str(modbus.get("host", "127.0.0.1")),
                        port=int(modbus.get("port", 1502)),
                        log_callback=self.log_callback,
                    )
                    self.modbus_server.set_holding_registers(
                        0,
                        [1],
                        target="gateway/system/read_only",
                    )
                    self.modbus_server.start()
                    if self.value_bus is not None:
                        self.modbus_adapter = GatewayModbusOutputAdapter(
                            self.config_manager,
                            self.value_bus,
                            self.modbus_server,
                            self.log_callback,
                        )
                        self.modbus_adapter.start()

                if self._enabled(opcua):
                    self._start_opcua(
                        str(
                            opcua.get(
                                "endpoint",
                                "opc.tcp://127.0.0.1:4841",
                            )
                        )
                    )
                self._running = True
            except Exception:
                self._running = True
                self.stop()
                raise

    def stop(self) -> None:
        with self._lock:
            if (
                not self._running
                and self.modbus_server is None
                and self.opcua_server is None
            ):
                return
            self._running = False
            modbus_adapter, self.modbus_adapter = self.modbus_adapter, None
            modbus, self.modbus_server = self.modbus_server, None
            opcua_adapter, self.opcua_adapter = self.opcua_adapter, None
            loop = self._opcua_loop
            thread, self._opcua_thread = self._opcua_thread, None
        if modbus_adapter is not None:
            modbus_adapter.stop()
        if modbus is not None:
            modbus.stop()
        if opcua_adapter is not None:
            opcua_adapter.stop()
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
        self._opcua_loop = None
        self.opcua_server = None
        self.opcua_system_node_id = None

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def _start_opcua(self, endpoint: str) -> None:
        self._opcua_ready.clear()
        self._opcua_error = None
        self.opcua_server = GatewayOpcuaServer(
            endpoint=endpoint,
            log_callback=self.log_callback,
        )
        self._opcua_thread = threading.Thread(
            target=self._opcua_worker,
            name="GatewayOpcuaOutput",
            daemon=True,
        )
        self._opcua_thread.start()
        if not self._opcua_ready.wait(timeout=10):
            raise RuntimeError("OPC UA 輸出 Server 啟動逾時")
        if self._opcua_error is not None:
            raise RuntimeError(
                "OPC UA 輸出 Server 啟動失敗"
            ) from self._opcua_error

    def _opcua_worker(self) -> None:
        loop = asyncio.new_event_loop()
        self._opcua_loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._initialize_opcua())
            self._opcua_ready.set()
            loop.run_forever()
        except BaseException as exc:
            self._opcua_error = exc
            self._opcua_ready.set()
        finally:
            server = self.opcua_server
            if server is not None:
                try:
                    loop.run_until_complete(server.stop())
                except Exception:
                    pass
            loop.close()

    async def _initialize_opcua(self) -> None:
        if self.opcua_server is None:
            raise RuntimeError("OPC UA 輸出 Server 尚未建立")
        await self.opcua_server.start()
        self.opcua_system_node_id = await self.opcua_server.add_readonly_variable(
            tag_id="gateway-system-read-only",
            display_name="ReadOnly",
            value=True,
            variant_type=ua.VariantType.Boolean,
        )
        if self.value_bus is not None:
            self.opcua_adapter = GatewayOpcuaOutputAdapter(
                self.config_manager,
                self.value_bus,
                self.opcua_server,
                asyncio.get_running_loop(),
                self.log_callback,
            )
            await self.opcua_adapter.start()

    def _config_snapshot(self) -> dict[str, Any]:
        getter = getattr(self.config_manager, "get_section", None)
        if callable(getter):
            value = getter("gateway_outputs", {})
            return dict(value) if isinstance(value, Mapping) else {}
        return {}

    @staticmethod
    def _section(config: Mapping[str, Any], name: str) -> dict[str, Any]:
        value = config.get(name, {})
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _enabled(config: Mapping[str, Any]) -> bool:
        value = config.get("enable", False)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() == "true"
        return False
