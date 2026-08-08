"""Modbus TCP → Canonical Tag → OPC UA 第二條標準協定 Tracer Path。"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
import unittest

from asyncua import Client, ua
from asyncua.ua.uaerrors import UaStatusCodeError
from pymodbus.client import ModbusTcpClient

from core.data_model import GatewayModel, PointValue
from core.gateway_opcua_adapter import GatewayOpcuaOutputAdapter
from core.value_bus import ValueBus
from test_support.protocol_harness import LocalProtocolHarness


class _SubscriptionHandler:
    def __init__(self) -> None:
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()

    def datachange_notification(self, node, value, data) -> None:
        self.events.put((node.nodeid.to_string(), value))


class _ModelConfig:
    def get_gateway_model(self):
        return GatewayModel.from_dict(
            ModbusToOpcuaTracerE2ETests._gateway_model()
        )


class _RecordingOpcuaServer:
    def __init__(self) -> None:
        self.nodes: dict[str, tuple[str, object, ua.VariantType]] = {}
        self.values: dict[str, object] = {}
        self.updates: list[tuple[str, object]] = []

    async def add_readonly_variable(
        self,
        *,
        tag_id,
        display_name,
        value,
        variant_type,
    ):
        self.nodes[str(tag_id)] = (display_name, value, variant_type)
        self.values[str(tag_id)] = value
        return ua.NodeId(str(tag_id), 2)

    async def publish_value(
        self,
        *,
        tag_id,
        value,
        variant_type,
        source_timestamp=None,
        server_timestamp=None,
    ) -> None:
        del variant_type, source_timestamp, server_timestamp
        if value == 1:
            await asyncio.sleep(0.05)
        self.values[str(tag_id)] = value
        self.updates.append((str(tag_id), value))


class _PausedSnapshotValueBus(ValueBus):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot_taken = threading.Event()
        self.resume_snapshot = threading.Event()

    def get_latest_list(self):
        values = super().get_latest_list()
        self.snapshot_taken.set()
        if not self.resume_snapshot.wait(2.0):
            raise TimeoutError("test did not resume the ValueBus snapshot")
        return values


def _point(*, tag_id, data_type, value, quality="Good") -> PointValue:
    return PointValue(
        point_key=f"MODBUS_TCP::source::{tag_id}",
        protocol="MODBUS_TCP",
        source_name="source",
        device_name="device",
        point_name=tag_id,
        address_text=tag_id,
        value=value,
        data_type=data_type,
        tag_id=tag_id,
        connection_id="conn-simulated-modbus",
        device_id="device-simulated-modbus",
        quality=quality,
    )


class GatewayOpcuaOutputAdapterContractTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for_update_count(
        self,
        server: _RecordingOpcuaServer,
        count: int,
    ) -> None:
        deadline = time.monotonic() + 2.0
        while len(server.updates) < count and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        self.assertGreaterEqual(len(server.updates), count)

    async def _wait_for_value(
        self,
        server: _RecordingOpcuaServer,
        tag_id: str,
        expected: object,
    ) -> None:
        deadline = time.monotonic() + 2.0
        while (
            server.values.get(tag_id) != expected
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        self.assertEqual(expected, server.values.get(tag_id))

    async def test_mapping_is_type_safe_and_stop_unsubscribes(self):
        bus = ValueBus()
        server = _RecordingOpcuaServer()
        adapter = GatewayOpcuaOutputAdapter(
            _ModelConfig(),
            bus,
            server,
            asyncio.get_running_loop(),
        )
        await adapter.start()
        self.addCleanup(adapter.stop)

        bus.publish(
            _point(tag_id="tag-modbus-running", data_type="Boolean", value=True)
        )
        bus.publish(
            _point(tag_id="tag-modbus-count", data_type="UInt16", value=42)
        )
        await self._wait_for_update_count(server, 2)

        bus.publish(
            _point(
                tag_id="tag-modbus-running",
                data_type="Boolean",
                value=False,
                quality="Bad",
            )
        )
        bus.publish(
            _point(tag_id="tag-modbus-running", data_type="UInt16", value=1)
        )
        bus.publish(
            _point(tag_id="tag-modbus-count", data_type="UInt16", value=True)
        )
        await asyncio.sleep(0.05)

        self.assertEqual(True, server.values["tag-modbus-running"])
        self.assertEqual(42, server.values["tag-modbus-count"])

        adapter.stop()
        bus.publish(
            _point(tag_id="tag-modbus-running", data_type="Boolean", value=False)
        )
        await asyncio.sleep(0.05)
        self.assertEqual(True, server.values["tag-modbus-running"])

    async def test_start_replay_cannot_overwrite_a_concurrent_newer_value(self):
        bus = _PausedSnapshotValueBus()
        server = _RecordingOpcuaServer()
        adapter = GatewayOpcuaOutputAdapter(
            _ModelConfig(),
            bus,
            server,
            asyncio.get_running_loop(),
        )
        bus.publish(
            _point(tag_id="tag-modbus-count", data_type="UInt16", value=1)
        )
        snapshot_observed = threading.Event()
        publisher_finished = threading.Event()

        def publish_during_snapshot() -> None:
            if not bus.snapshot_taken.wait(2.0):
                return
            snapshot_observed.set()
            publisher = threading.Thread(
                target=lambda: bus.publish(
                    _point(
                        tag_id="tag-modbus-count",
                        data_type="UInt16",
                        value=2,
                    )
                )
            )
            publisher.start()
            time.sleep(0.02)
            bus.resume_snapshot.set()
            publisher.join(2.0)
            if not publisher.is_alive():
                publisher_finished.set()

        coordinator = threading.Thread(target=publish_during_snapshot)
        coordinator.start()
        await adapter.start()
        self.addCleanup(adapter.stop)
        coordinator.join(2.0)
        self.assertFalse(coordinator.is_alive())
        self.assertTrue(snapshot_observed.is_set())
        self.assertTrue(publisher_finished.is_set())
        await self._wait_for_value(server, "tag-modbus-count", 2)

        self.assertEqual(2, server.values["tag-modbus-count"])


class ModbusToOpcuaTracerE2ETests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _modbus_points() -> list[dict[str, object]]:
        return [
            {
                "enable": True,
                "name": "Running",
                "tag_id": "tag-modbus-running",
                "connection_id": "conn-simulated-modbus",
                "device_id": "device-simulated-modbus",
                "type": "coil",
                "address": 0,
                "count": 1,
                "data_type": "Boolean",
            },
            {
                "enable": True,
                "name": "Count",
                "tag_id": "tag-modbus-count",
                "connection_id": "conn-simulated-modbus",
                "device_id": "device-simulated-modbus",
                "type": "holding_register",
                "address": 10,
                "count": 1,
                "data_type": "UInt16",
            },
        ]

    @staticmethod
    def _gateway_model() -> dict[str, object]:
        return {
            "connections": [
                {
                    "connection_id": "conn-simulated-modbus",
                    "name": "Simulated Modbus Connection",
                    "protocol": "MODBUS_TCP",
                }
            ],
            "devices": [
                {
                    "device_id": "device-simulated-modbus",
                    "connection_id": "conn-simulated-modbus",
                    "name": "Simulated Modbus Device",
                }
            ],
            "tags": [
                {
                    "tag_id": "tag-modbus-running",
                    "point_key": "MODBUS_TCP::simulated::running",
                    "connection_id": "conn-simulated-modbus",
                    "device_id": "device-simulated-modbus",
                    "name": "Running",
                    "source_protocol": "MODBUS_TCP",
                    "source_address": "coil:0",
                    "data_type": "Boolean",
                    "opcua_output": {
                        "enabled": True,
                        "node_id": "tag-modbus-running",
                        "browse_name": "Running",
                    },
                },
                {
                    "tag_id": "tag-modbus-count",
                    "point_key": "MODBUS_TCP::simulated::count",
                    "connection_id": "conn-simulated-modbus",
                    "device_id": "device-simulated-modbus",
                    "name": "Count",
                    "source_protocol": "MODBUS_TCP",
                    "source_address": "holding_register:10",
                    "data_type": "UInt16",
                    "opcua_output": {
                        "enabled": True,
                        "node_id": "tag-modbus-count",
                        "browse_name": "Count",
                    },
                },
            ],
        }

    async def _wait_for_subscription_values(
        self,
        handler: _SubscriptionHandler,
        expected: dict[str, object],
    ) -> None:
        seen: dict[str, object] = {}
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not all(
            seen.get(node_id) == value for node_id, value in expected.items()
        ):
            timeout = max(0.01, deadline - time.monotonic())
            try:
                node_id, value = await asyncio.to_thread(
                    handler.events.get,
                    True,
                    timeout,
                )
            except queue.Empty:
                break
            seen[node_id] = value
        self.assertTrue(
            all(seen.get(node_id) == value for node_id, value in expected.items()),
            f"未收到完整OPC UA DataChange：seen={seen}, expected={expected}",
        )

    async def test_coil_and_uint16_read_subscribe_and_write_rejection(self):
        with LocalProtocolHarness(
            modbus_points=self._modbus_points(),
            gateway_model=self._gateway_model(),
        ) as harness:
            harness.set_modbus_source_coils(0, [False])
            harness.set_modbus_source_registers(10, [123])
            await asyncio.to_thread(harness.poll_modbus_source_once)

            async with Client(harness.gateway_opcua_endpoint) as client:
                namespace = await client.get_namespace_index(
                    "urn:picdarcy:multi-protocol-plc-hmi:gateway"
                )
                running = client.get_node(
                    ua.NodeId("tag-modbus-running", namespace)
                )
                count = client.get_node(ua.NodeId("tag-modbus-count", namespace))

                self.assertIs(await running.read_value(), False)
                self.assertEqual(123, await count.read_value())
                self.assertEqual(
                    ua.VariantType.Boolean,
                    await running.read_data_type_as_variant_type(),
                )
                self.assertEqual(
                    ua.VariantType.UInt16,
                    await count.read_data_type_as_variant_type(),
                )

                handler = _SubscriptionHandler()
                subscription = await client.create_subscription(50, handler)
                await subscription.subscribe_data_change([running, count])
                try:
                    harness.set_modbus_source_coils(0, [True])
                    harness.set_modbus_source_registers(10, [456])
                    await asyncio.to_thread(harness.poll_modbus_source_once)
                    await self._wait_for_subscription_values(
                        handler,
                        {
                            running.nodeid.to_string(): True,
                            count.nodeid.to_string(): 456,
                        },
                    )

                    with self.assertRaises(UaStatusCodeError):
                        await running.write_value(False, ua.VariantType.Boolean)
                    with self.assertRaises(UaStatusCodeError):
                        await count.write_value(999, ua.VariantType.UInt16)
                finally:
                    await subscription.delete()

            source = ModbusTcpClient(
                "127.0.0.1",
                port=harness.modbus_source_port,
                timeout=2,
            )
            try:
                self.assertTrue(source.connect())
                coil = source.read_coils(0, count=1, device_id=1)
                register = source.read_holding_registers(
                    10,
                    count=1,
                    device_id=1,
                )
                self.assertFalse(coil.isError())
                self.assertFalse(register.isError())
                self.assertIs(bool(coil.bits[0]), True)
                self.assertEqual([456], register.registers)
            finally:
                source.close()

            security_logs = [
                message
                for message in harness.logs
                if "SECURITY_WRITE_REJECTED" in message
            ]
            self.assertTrue(
                any("tag-modbus-running" in message for message in security_logs)
            )
            self.assertTrue(
                any("tag-modbus-count" in message for message in security_logs)
            )


if __name__ == "__main__":
    unittest.main()
