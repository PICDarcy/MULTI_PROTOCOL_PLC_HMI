"""真實協定邊界的本機端到端測試環境。"""

from __future__ import annotations

import asyncio
import copy
import tempfile
import threading
from pathlib import Path
from typing import Any

from asyncua import Client, Server, ua

from core.config_manager import ConfigManager
from core.data_model import make_opcua_point_key
from core.gateway_modbus_server import GatewayModbusTcpServer
from core.gateway_runtime import GatewayOutputRuntime
from core.modbus_tcp_manager import ModbusTcpManager
from core.opcua_manager import OpcuaMultiServerManager
from core.value_bus import ValueBus


class _OpcuaSourceRuntime:
    """在專屬 event loop 執行模擬 OPC UA 來源。"""

    def __init__(self, port: int) -> None:
        self.port = int(port)
        self.endpoint = f"opc.tcp://127.0.0.1:{self.port}"
        self.server = Server()
        self.node_id: ua.NodeId | None = None
        self.node = None
        self.boolean_node_id: ua.NodeId | None = None
        self.boolean_node = None
        self.uint16_node_id: ua.NodeId | None = None
        self.uint16_node = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._worker,
            name="ProtocolHarnessOpcuaSource",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            self.stop()
            raise RuntimeError("模擬OPC UA來源啟動逾時")
        if self._error is not None:
            self.stop()
            raise RuntimeError("模擬OPC UA來源啟動失敗") from self._error

    def stop(self) -> None:
        loop = self._loop
        thread, self._thread = self._thread, None
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("模擬OPC UA來源未能正常關閉")
        self._loop = None

    def _worker(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._initialize())
            self._ready.set()
            loop.run_forever()
        except BaseException as exc:
            self._error = exc
            self._ready.set()
        finally:
            try:
                loop.run_until_complete(self.server.stop())
            finally:
                loop.close()

    async def _initialize(self) -> None:
        await self.server.init()
        self.server.set_endpoint(self.endpoint)
        namespace = await self.server.register_namespace(
            "urn:picdarcy:protocol-harness:source"
        )
        node = await self.server.nodes.objects.add_variable(
            ua.NodeId("simulated-temperature", namespace),
            ua.QualifiedName("Temperature", namespace),
            73.5,
            ua.VariantType.Double,
        )
        self.node = node
        self.node_id = node.nodeid
        boolean_node = await self.server.nodes.objects.add_variable(
            ua.NodeId("simulated-running", namespace),
            ua.QualifiedName("Running", namespace),
            False,
            ua.VariantType.Boolean,
        )
        uint16_node = await self.server.nodes.objects.add_variable(
            ua.NodeId("simulated-count", namespace),
            ua.QualifiedName("Count", namespace),
            0,
            ua.VariantType.UInt16,
        )
        await boolean_node.set_writable()
        await uint16_node.set_writable()
        self.boolean_node = boolean_node
        self.boolean_node_id = boolean_node.nodeid
        self.uint16_node = uint16_node
        self.uint16_node_id = uint16_node.nodeid
        await self.server.start()
        self.port = int(self.server.bserver.port)
        self.endpoint = f"opc.tcp://127.0.0.1:{self.port}"

    def set_data_value(
        self,
        value: Any,
        *,
        variant_type: ua.VariantType,
        status_code: int,
        source_timestamp,
        server_timestamp,
    ) -> None:
        loop = self._loop
        if loop is None or not loop.is_running() or self.node is None:
            raise RuntimeError("模擬OPC UA來源尚未啟動")
        future = asyncio.run_coroutine_threadsafe(
            self.node.write_value(
                ua.DataValue(
                    Value=ua.Variant(value, variant_type),
                    StatusCode_=ua.StatusCode(status_code),
                    SourceTimestamp=source_timestamp,
                    ServerTimestamp=server_timestamp,
                )
            ),
            loop,
        )
        future.result(timeout=5)


class LocalProtocolHarness:
    """模擬兩種來源、Gateway 與標準 Client 所需的隔離環境。"""

    def __init__(
        self,
        *,
        modbus_source_port: int = 0,
        opcua_source_port: int = 0,
        gateway_modbus_port: int = 0,
        gateway_opcua_port: int = 0,
        opcua_subscribe: bool = True,
        opcua_poll_interval: float = 0.1,
        modbus_points: list[dict[str, Any]] | None = None,
        gateway_model: dict[str, Any] | None = None,
        auto_start_opcua_collection: bool = False,
    ) -> None:
        self._requested_ports = {
            "modbus_source": int(modbus_source_port),
            "opcua_source": int(opcua_source_port),
            "gateway_modbus": int(gateway_modbus_port),
            "gateway_opcua": int(gateway_opcua_port),
        }
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._opcua_subscribe = bool(opcua_subscribe)
        self._opcua_poll_interval = float(opcua_poll_interval)
        self._modbus_points = copy.deepcopy(modbus_points)
        self._gateway_model = copy.deepcopy(gateway_model)
        self._auto_start_opcua_collection = bool(auto_start_opcua_collection)
        self._config_path: Path | None = None
        self.config_manager: ConfigManager | None = None
        self.value_bus: ValueBus | None = None
        self.modbus_manager: ModbusTcpManager | None = None
        self.opcua_manager: OpcuaMultiServerManager | None = None
        self.gateway_runtime: GatewayOutputRuntime | None = None
        self.modbus_source: GatewayModbusTcpServer | None = None
        self.opcua_source: _OpcuaSourceRuntime | None = None
        self._allocated_ports: dict[str, int] = {}
        self.logs: list[str] = []
        self._running = False

    def __enter__(self) -> "LocalProtocolHarness":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()

    @property
    def config_path(self) -> Path:
        if self._config_path is None:
            raise RuntimeError("測試環境尚未啟動")
        return self._config_path

    @property
    def modbus_source_port(self) -> int:
        if self.modbus_source is None:
            raise RuntimeError("模擬Modbus來源尚未啟動")
        return self.modbus_source.port

    @property
    def gateway_modbus_port(self) -> int:
        runtime = self.gateway_runtime
        if runtime is None or runtime.modbus_server is None:
            raise RuntimeError("Gateway Modbus輸出尚未啟動")
        return runtime.modbus_server.port

    @property
    def gateway_opcua_endpoint(self) -> str:
        runtime = self.gateway_runtime
        if runtime is None or runtime.opcua_server is None:
            raise RuntimeError("Gateway OPC UA輸出尚未啟動")
        return f"opc.tcp://127.0.0.1:{runtime.opcua_server.port}"

    @property
    def opcua_source_endpoint(self) -> str:
        if self.opcua_source is None:
            raise RuntimeError("模擬OPC UA來源尚未啟動")
        return self.opcua_source.endpoint

    @property
    def opcua_boolean_node_id(self) -> ua.NodeId:
        if self.opcua_source is None or self.opcua_source.boolean_node_id is None:
            raise RuntimeError("模擬OPC UA Boolean來源尚未啟動")
        return self.opcua_source.boolean_node_id

    @property
    def opcua_uint16_node_id(self) -> ua.NodeId:
        if self.opcua_source is None or self.opcua_source.uint16_node_id is None:
            raise RuntimeError("模擬OPC UA UInt16來源尚未啟動")
        return self.opcua_source.uint16_node_id

    @property
    def ports(self) -> dict[str, int]:
        if self.opcua_source is None or self.gateway_runtime is None:
            raise RuntimeError("測試環境尚未啟動")
        opcua_server = self.gateway_runtime.opcua_server
        if opcua_server is None:
            raise RuntimeError("Gateway OPC UA輸出尚未啟動")
        return {
            "modbus_source": self.modbus_source_port,
            "opcua_source": self.opcua_source.port,
            "gateway_modbus": self.gateway_modbus_port,
            "gateway_opcua": opcua_server.port,
        }

    @property
    def allocated_ports(self) -> dict[str, int]:
        """回傳本次 start 已成功取得的 ports，停止後仍可供清理斷言。"""
        return dict(self._allocated_ports)

    def start(self) -> None:
        if self._running:
            return
        self._allocated_ports = {}
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="plc-hmi-e2e-"
        )
        self._config_path = Path(self._temporary_directory.name) / "config.json"
        try:
            self.modbus_source = GatewayModbusTcpServer(
                port=self._requested_ports["modbus_source"]
            )
            self.modbus_source.set_holding_registers(
                10,
                [2468],
                target="simulated/modbus/temperature",
            )
            self.modbus_source.start()
            self._allocated_ports["modbus_source"] = self.modbus_source.port

            self.opcua_source = _OpcuaSourceRuntime(
                self._requested_ports["opcua_source"]
            )
            self.opcua_source.start()
            self._allocated_ports["opcua_source"] = self.opcua_source.port

            self._gateway_opcua_port = self._requested_ports["gateway_opcua"]
            self.config_manager = ConfigManager(self._config_path)
            self.config_manager.set_config(self._configuration())
            self.config_manager.save_config()
            self.value_bus = ValueBus()
            self.modbus_manager = ModbusTcpManager(
                self.config_manager,
                self.value_bus,
            )
            self.opcua_manager = OpcuaMultiServerManager(
                self.config_manager,
                self.value_bus,
                self._capture_log,
            )
            self.gateway_runtime = GatewayOutputRuntime(
                self.config_manager,
                self._capture_log,
                self.value_bus,
            )
            self.gateway_runtime.start()
            if self.gateway_runtime.opcua_server is None:
                raise RuntimeError("Gateway OPC UA輸出未建立")
            self._gateway_opcua_port = self.gateway_runtime.opcua_server.port
            self._allocated_ports.update(
                {
                    "gateway_modbus": self.gateway_modbus_port,
                    "gateway_opcua": self._gateway_opcua_port,
                }
            )
            self.value_bus.subscribe(self._bridge_modbus_value)
            if self._auto_start_opcua_collection:
                self.opcua_manager.subscribe_all().result(timeout=5)
            self._running = True
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        errors: list[BaseException] = []
        if self.value_bus is not None:
            self.value_bus.unsubscribe(self._bridge_modbus_value)
        if self.modbus_manager is not None:
            try:
                self.modbus_manager.stop_polling()
            except BaseException as exc:
                errors.append(exc)
        if self.opcua_manager is not None:
            try:
                self.opcua_manager.shutdown().result(timeout=10)
            except BaseException as exc:
                errors.append(exc)
        for component in (
            self.gateway_runtime,
            self.opcua_source,
            self.modbus_source,
        ):
            if component is None:
                continue
            try:
                component.stop()
            except BaseException as exc:
                errors.append(exc)

        self.gateway_runtime = None
        self.modbus_manager = None
        self.opcua_manager = None
        self.opcua_source = None
        self.modbus_source = None
        self.value_bus = None
        self.config_manager = None
        self._running = False
        temporary, self._temporary_directory = self._temporary_directory, None
        if temporary is not None:
            temporary.cleanup()
        if errors:
            raise RuntimeError("本機協定測試環境清理失敗") from errors[0]

    def poll_modbus_source_once(self) -> dict[str, int]:
        if self.modbus_manager is None:
            raise RuntimeError("測試環境尚未啟動")
        return self.modbus_manager.read_all_once()

    def set_modbus_source_registers(
        self,
        address: int,
        values: list[int],
    ) -> None:
        if self.modbus_source is None:
            raise RuntimeError("測試環境尚未啟動")
        self.modbus_source.set_holding_registers(
            address,
            values,
            target="simulated/modbus/registers",
        )

    def set_modbus_source_coils(
        self,
        address: int,
        values: list[bool],
    ) -> None:
        if self.modbus_source is None:
            raise RuntimeError("測試環境尚未啟動")
        self.modbus_source.set_coils(
            address,
            values,
            target="simulated/modbus/coils",
        )

    def poll_opcua_source_once(self) -> dict[str, Any]:
        if self.opcua_manager is None or self.opcua_source is None:
            raise RuntimeError("測試環境尚未啟動")
        return self.opcua_manager.read_node(
            "Simulated OPC UA Source",
            self.opcua_source.node_id.to_string(),
        ).result(timeout=5)

    def start_opcua_collection(self) -> dict[str, Any]:
        if self.opcua_manager is None:
            raise RuntimeError("測試環境尚未啟動")
        result = self.opcua_manager.subscribe_all().result(timeout=5)
        return result["Simulated OPC UA Source"]["nodes"][0]

    def stop_opcua_collection(self) -> dict[str, Any]:
        if self.opcua_manager is None or self.opcua_source is None:
            raise RuntimeError("測試環境尚未啟動")
        return self.opcua_manager.unsubscribe_node(
            "Simulated OPC UA Source",
            self.opcua_source.node_id.to_string(),
        ).result(timeout=5)

    def set_opcua_source_value(
        self,
        value: Any,
        *,
        variant_type: ua.VariantType = ua.VariantType.Double,
        status_code: int = ua.StatusCodes.Good,
        source_timestamp=None,
        server_timestamp=None,
    ) -> None:
        if self.opcua_source is None:
            raise RuntimeError("測試環境尚未啟動")
        self.opcua_source.set_data_value(
            value,
            variant_type=variant_type,
            status_code=status_code,
            source_timestamp=source_timestamp,
            server_timestamp=server_timestamp,
        )

    async def read_opcua_source(self) -> Any:
        if self.opcua_source is None or self.opcua_source.node_id is None:
            raise RuntimeError("模擬OPC UA來源尚未啟動")
        async with Client(self.opcua_source.endpoint) as client:
            return await client.get_node(self.opcua_source.node_id).read_value()

    async def read_gateway_opcua_system(self) -> Any:
        runtime = self.gateway_runtime
        if (
            runtime is None
            or runtime.opcua_server is None
            or runtime.opcua_system_node_id is None
        ):
            raise RuntimeError("Gateway OPC UA輸出尚未啟動")
        endpoint = f"opc.tcp://127.0.0.1:{runtime.opcua_server.port}"
        async with Client(endpoint) as client:
            node = client.get_node(runtime.opcua_system_node_id)
            return await node.read_value()

    def _configuration(self) -> dict[str, Any]:
        configuration = {
            "opcua": {
                "enable": True,
                "servers": [
                    {
                        "enable": True,
                        "name": "Simulated OPC UA Source",
                        "connection_id": "conn-simulated-opcua",
                        "device_id": "device-simulated-opcua",
                        "endpoint_url": self.opcua_source.endpoint,
                        "poll_interval": self._opcua_poll_interval,
                        "nodes": [
                            {
                                "enable": True,
                                "name": "Temperature",
                                "tag_id": "tag-simulated-temperature",
                                "connection_id": "conn-simulated-opcua",
                                "device_id": "device-simulated-opcua",
                                "node_id": self.opcua_source.node_id.to_string(),
                                "data_type": "Double",
                                "subscribe": self._opcua_subscribe,
                            },
                            {
                                "enable": True,
                                "name": "Running",
                                "tag_id": "tag-simulated-running",
                                "connection_id": "conn-simulated-opcua",
                                "device_id": "device-simulated-opcua",
                                "node_id": (
                                    self.opcua_source.boolean_node_id.to_string()
                                ),
                                "data_type": "Boolean",
                                "subscribe": self._opcua_subscribe,
                            },
                            {
                                "enable": True,
                                "name": "Count",
                                "tag_id": "tag-simulated-count",
                                "connection_id": "conn-simulated-opcua",
                                "device_id": "device-simulated-opcua",
                                "node_id": (
                                    self.opcua_source.uint16_node_id.to_string()
                                ),
                                "data_type": "UInt16",
                                "subscribe": self._opcua_subscribe,
                            }
                        ],
                    }
                ],
            },
            "modbus_tcp": {
                "enable": True,
                "timeout": 2.0,
                "poll_interval": 0.1,
                "devices": [
                    {
                        "enable": True,
                        "name": "Simulated Modbus Source",
                        "connection_id": "conn-simulated-modbus",
                        "device_id": "device-simulated-modbus",
                        "host": "127.0.0.1",
                        "port": self.modbus_source_port,
                        "station_id": 1,
                        "points": self._modbus_points
                        if self._modbus_points is not None
                        else [
                            {
                                "enable": True,
                                "name": "Temperature",
                                "type": "holding_register",
                                "address": 10,
                                "count": 1,
                                "data_type": "UInt16",
                            }
                        ],
                    }
                ],
            },
            "gateway_outputs": {
                "modbus_tcp_server": {
                    "enable": True,
                    "host": "127.0.0.1",
                    "port": self._requested_ports["gateway_modbus"],
                },
                "opcua_server": {
                    "enable": True,
                    "endpoint": (
                        f"opc.tcp://127.0.0.1:{self._gateway_opcua_port}"
                    ),
                },
            },
            "gateway_model": {
                "connections": [
                    {
                        "connection_id": "conn-simulated-opcua",
                        "name": "Simulated OPC UA Connection",
                        "protocol": "OPCUA",
                        "settings": {"endpoint": self.opcua_source.endpoint},
                    }
                ],
                "devices": [
                    {
                        "device_id": "device-simulated-opcua",
                        "connection_id": "conn-simulated-opcua",
                        "name": "Simulated OPC UA Device",
                    }
                ],
                "tags": [
                    {
                        "tag_id": "tag-simulated-running",
                        "point_key": make_opcua_point_key(
                            "Simulated OPC UA Source",
                            self.opcua_source.boolean_node_id.to_string(),
                        ),
                        "connection_id": "conn-simulated-opcua",
                        "device_id": "device-simulated-opcua",
                        "name": "Running",
                        "source_protocol": "OPCUA",
                        "source_address": (
                            self.opcua_source.boolean_node_id.to_string()
                        ),
                        "data_type": "Boolean",
                        "modbus_tcp_output": {
                            "enabled": True,
                            "address": 0,
                        },
                    },
                    {
                        "tag_id": "tag-simulated-count",
                        "point_key": make_opcua_point_key(
                            "Simulated OPC UA Source",
                            self.opcua_source.uint16_node_id.to_string(),
                        ),
                        "connection_id": "conn-simulated-opcua",
                        "device_id": "device-simulated-opcua",
                        "name": "Count",
                        "source_protocol": "OPCUA",
                        "source_address": (
                            self.opcua_source.uint16_node_id.to_string()
                        ),
                        "data_type": "UInt16",
                        "modbus_tcp_output": {
                            "enabled": True,
                            "area": "holding_register",
                            "address": 100,
                        },
                    },
                ],
            },
        }
        if self._gateway_model is not None:
            configuration["gateway_model"] = copy.deepcopy(self._gateway_model)
        return configuration

    def _bridge_modbus_value(self, point_value) -> None:
        runtime = self.gateway_runtime
        if (
            runtime is None
            or runtime.modbus_server is None
            or point_value.protocol != "MODBUS_TCP"
            or point_value.status_text != "Good"
        ):
            return
        value = point_value.value
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 0xFFFF
        ):
            return
        runtime.modbus_server.set_holding_registers(
            100,
            [value],
            target=point_value.point_key,
        )

    def _capture_log(self, message: str, level: str = "INFO") -> None:
        self.logs.append(f"{level}:{message}")
