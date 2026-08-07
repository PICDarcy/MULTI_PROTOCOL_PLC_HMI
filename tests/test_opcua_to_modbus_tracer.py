"""OPC UA → Canonical Tag → Modbus TCP 第一條標準協定 Tracer Path。"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest

from asyncua import Client, ua
from pymodbus.client import ModbusTcpClient

from core.data_model import GatewayModel, PointValue
from core.gateway_modbus_adapter import GatewayModbusOutputAdapter
from core.value_bus import ValueBus
from test_support.protocol_harness import LocalProtocolHarness


class _ModelConfig:
    def get_gateway_model(self):
        return GatewayModel.from_dict(
            {
                "connections": [
                    {
                        "connection_id": "conn-opcua",
                        "name": "OPC UA",
                        "protocol": "OPCUA",
                    }
                ],
                "devices": [
                    {
                        "device_id": "device-opcua",
                        "connection_id": "conn-opcua",
                        "name": "Device",
                    }
                ],
                "tags": [
                    {
                        "tag_id": "tag-running",
                        "point_key": "OPCUA::source::running",
                        "connection_id": "conn-opcua",
                        "device_id": "device-opcua",
                        "name": "Running",
                        "source_protocol": "OPCUA",
                        "source_address": "ns=2;s=Running",
                        "data_type": "Boolean",
                        "modbus_tcp_output": {
                            "enabled": True,
                            "address": 0,
                        },
                    },
                    {
                        "tag_id": "tag-count",
                        "point_key": "OPCUA::source::count",
                        "connection_id": "conn-opcua",
                        "device_id": "device-opcua",
                        "name": "Count",
                        "source_protocol": "OPCUA",
                        "source_address": "ns=2;s=Count",
                        "data_type": "UInt16",
                        "modbus_tcp_output": {
                            "enabled": True,
                            "area": "holding_register",
                            "address": 100,
                        },
                    },
                ],
            }
        )


class _RecordingModbusServer:
    def __init__(self):
        self.coils = {}
        self.registers = {}
        self.register_writes = []

    def set_coils(self, address, values, *, target):
        self.coils[int(address)] = (list(values), target)

    def set_holding_registers(self, address, values, *, target):
        self.registers[int(address)] = (list(values), target)
        self.register_writes.append((int(address), list(values), target))


class _PausedSnapshotValueBus(ValueBus):
    def __init__(self):
        super().__init__()
        self.snapshot_taken = threading.Event()
        self.resume_snapshot = threading.Event()

    def get_latest_list(self):
        values = super().get_latest_list()
        self.snapshot_taken.set()
        if not self.resume_snapshot.wait(2.0):
            raise TimeoutError("test did not resume the ValueBus snapshot")
        return values


def _point(*, tag_id, data_type, value, quality="Good"):
    return PointValue(
        point_key=f"OPCUA::source::{tag_id}",
        protocol="OPCUA",
        source_name="source",
        device_name="device",
        point_name=tag_id,
        address_text=tag_id,
        value=value,
        data_type=data_type,
        tag_id=tag_id,
        connection_id="conn-opcua",
        device_id="device-opcua",
        quality=quality,
    )


class GatewayModbusOutputAdapterContractTests(unittest.TestCase):
    def test_start_replay_cannot_overwrite_a_concurrent_newer_value(self):
        bus = _PausedSnapshotValueBus()
        server = _RecordingModbusServer()
        adapter = GatewayModbusOutputAdapter(_ModelConfig(), bus, server)
        bus.publish(_point(tag_id="tag-count", data_type="UInt16", value=1))

        start_thread = threading.Thread(target=adapter.start)
        start_thread.start()
        self.addCleanup(adapter.stop)
        self.addCleanup(lambda: start_thread.join(2.0))
        self.assertTrue(bus.snapshot_taken.wait(2.0))

        publish_thread = threading.Thread(
            target=lambda: bus.publish(
                _point(tag_id="tag-count", data_type="UInt16", value=2)
            )
        )
        publish_thread.start()
        bus.resume_snapshot.set()
        start_thread.join(2.0)
        publish_thread.join(2.0)

        self.assertFalse(start_thread.is_alive())
        self.assertFalse(publish_thread.is_alive())
        self.assertEqual(
            [(100, [1], "tag-count"), (100, [2], "tag-count")],
            server.register_writes,
        )
        self.assertEqual(([2], "tag-count"), server.registers[100])

    def test_mapping_is_type_safe_and_stop_unsubscribes(self):
        bus = ValueBus()
        server = _RecordingModbusServer()
        adapter = GatewayModbusOutputAdapter(_ModelConfig(), bus, server)
        adapter.start()
        self.addCleanup(adapter.stop)

        bus.publish(
            _point(tag_id="tag-running", data_type="Boolean", value=False)
        )
        bus.publish(_point(tag_id="tag-count", data_type="UInt16", value=42))
        bus.publish(
            _point(
                tag_id="tag-running",
                data_type="Boolean",
                value=True,
                quality="Bad",
            )
        )
        bus.publish(
            _point(tag_id="tag-running", data_type="UInt16", value=1)
        )
        bus.publish(
            _point(tag_id="tag-count", data_type="UInt16", value=True)
        )

        self.assertEqual({0: ([False], "tag-running")}, server.coils)
        self.assertEqual({100: ([42], "tag-count")}, server.registers)

        adapter.stop()
        bus.publish(
            _point(tag_id="tag-running", data_type="Boolean", value=True)
        )
        bus.publish(_point(tag_id="tag-count", data_type="UInt16", value=99))

        self.assertEqual({0: ([False], "tag-running")}, server.coils)
        self.assertEqual({100: ([42], "tag-count")}, server.registers)


class OpcuaToModbusTracerE2ETests(unittest.TestCase):
    async def _write_opcua_source(
        self,
        harness: LocalProtocolHarness,
        *,
        running: bool,
        count: int,
    ) -> None:
        async with Client(harness.opcua_source_endpoint) as client:
            await client.get_node(harness.opcua_boolean_node_id).write_value(
                running,
                ua.VariantType.Boolean,
            )
            await client.get_node(harness.opcua_uint16_node_id).write_value(
                count,
                ua.VariantType.UInt16,
            )

    async def _read_opcua_source(self, harness: LocalProtocolHarness):
        async with Client(harness.opcua_source_endpoint) as client:
            running = await client.get_node(
                harness.opcua_boolean_node_id
            ).read_value()
            count = await client.get_node(
                harness.opcua_uint16_node_id
            ).read_value()
        return running, count

    def _wait_for_modbus_values(
        self,
        client: ModbusTcpClient,
        *,
        running: bool,
        count: int,
    ) -> None:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            coil = client.read_coils(0, count=1, device_id=1)
            register = client.read_holding_registers(100, count=1, device_id=1)
            if (
                not coil.isError()
                and not register.isError()
                and bool(coil.bits[0]) is running
                and register.registers == [count]
            ):
                return
            time.sleep(0.05)
        self.fail(
            f"Modbus輸出未在期限內更新為 coil={running}, register={count}"
        )

    def test_boolean_and_uint16_flow_and_writes_never_reach_source(self):
        with LocalProtocolHarness(auto_start_opcua_collection=True) as harness:
            modbus = ModbusTcpClient(
                "127.0.0.1",
                port=harness.gateway_modbus_port,
                timeout=2,
            )
            try:
                self.assertTrue(modbus.connect())

                asyncio.run(
                    self._write_opcua_source(
                        harness,
                        running=False,
                        count=123,
                    )
                )
                self._wait_for_modbus_values(
                    modbus,
                    running=False,
                    count=123,
                )

                asyncio.run(
                    self._write_opcua_source(
                        harness,
                        running=True,
                        count=456,
                    )
                )
                self._wait_for_modbus_values(
                    modbus,
                    running=True,
                    count=456,
                )

                self.assertTrue(
                    modbus.write_coil(0, False, device_id=1).isError()
                )
                self.assertTrue(
                    modbus.write_register(100, 999, device_id=1).isError()
                )
                self._wait_for_modbus_values(
                    modbus,
                    running=True,
                    count=456,
                )
                self.assertEqual(
                    (True, 456),
                    asyncio.run(self._read_opcua_source(harness)),
                )
            finally:
                modbus.close()

            security_logs = [
                message
                for message in harness.logs
                if "SECURITY_WRITE_REJECTED" in message
            ]
            self.assertTrue(
                any(
                    '"request_type": "function_code_5"' in item
                    for item in security_logs
                )
            )
            self.assertTrue(
                any(
                    '"request_type": "function_code_6"' in item
                    for item in security_logs
                )
            )
            self.assertTrue(
                any(
                    '"target": "tag-simulated-running"' in item
                    for item in security_logs
                )
            )
            self.assertTrue(
                any(
                    '"target": "tag-simulated-count"' in item
                    for item in security_logs
                )
            )


if __name__ == "__main__":
    unittest.main()
