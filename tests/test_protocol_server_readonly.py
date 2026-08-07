"""以標準協定 Client 驗證兩種輸出 Server 的唯讀邊界。"""

from __future__ import annotations

import json
import socket
import unittest

from asyncua import Client, ua
from pymodbus.client import ModbusTcpClient
from pymodbus.pdu.file_message import FileRecord

from core.gateway_modbus_server import GatewayModbusTcpServer
from core.gateway_opcua_server import GatewayOpcuaServer
from core.gateway_runtime import GatewayOutputRuntime


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Config:
    def __init__(self, gateway_outputs):
        self.gateway_outputs = gateway_outputs

    def get_section(self, name, default=None):
        if name == "gateway_outputs":
            return self.gateway_outputs
        return default


class ModbusServerReadonlyContractTests(unittest.TestCase):
    def test_standard_client_write_fails_and_source_value_is_unchanged(self):
        logs = []
        server = GatewayModbusTcpServer(
            host="127.0.0.1",
            port=0,
            log_callback=lambda message, level="INFO": logs.append(
                (level, message)
            ),
        )
        server.set_holding_registers(100, [321], target="source/tag-1")
        server.set_holding_registers(200, [654], target="source/tag-2")
        server.set_file_record(1, 33, [777], target="source/file-tag")
        server.start()
        client = ModbusTcpClient("127.0.0.1", port=server.port, timeout=2)
        try:
            self.assertTrue(client.connect())
            before = client.read_holding_registers(100, count=1, device_id=1)
            self.assertEqual(before.registers, [321])

            rejected = client.write_register(100, 999, device_id=1)
            self.assertTrue(rejected.isError())
            self.assertTrue(
                client.mask_write_register(
                    address=100,
                    and_mask=0xFFFF,
                    or_mask=1,
                    device_id=1,
                ).isError()
            )
            self.assertTrue(
                client.readwrite_registers(
                    read_address=100,
                    read_count=1,
                    write_address=200,
                    values=[999],
                    device_id=1,
                ).isError()
            )
            self.assertTrue(
                client.write_file_record(
                    [
                        FileRecord(
                            file_number=1,
                            record_number=33,
                            record_data=b"\x03\xe7",
                        )
                    ],
                    device_id=1,
                ).isError()
            )

            after = client.read_holding_registers(100, count=1, device_id=1)
            self.assertEqual(after.registers, [321])
            after_fc23 = client.read_holding_registers(
                200, count=1, device_id=1
            )
            self.assertEqual(after_fc23.registers, [654])
            after_fc21 = client.read_file_record(
                [
                    FileRecord(
                        file_number=1,
                        record_number=33,
                        record_length=2,
                    )
                ],
                device_id=1,
            )
            self.assertEqual(after_fc21.records[0].record_data, b"\x03\x09")
        finally:
            client.close()
            server.stop()

        security_logs = [
            message for _level, message in logs
            if message.startswith("SECURITY_WRITE_REJECTED")
        ]
        by_function = {
            function_code: next(
                message for message in security_logs
                if f'"request_type": "function_code_{function_code}"'
                in message
            )
            for function_code in (6, 21, 22, 23)
        }
        self.assertIn('"protocol": "MODBUS_TCP"', by_function[6])
        self.assertIn('"client": "127.0.0.1:', by_function[6])
        self.assertIn('"target": "source/tag-1"', by_function[6])
        self.assertIn('"address": "file:1/record:33"', by_function[21])
        self.assertIn('"target": "source/file-tag"', by_function[21])
        self.assertIn('"address": "holding_register:100"', by_function[22])
        self.assertIn('"target": "source/tag-2"', by_function[23])
        self.assertIn('"address": "holding_register:200"', by_function[23])


class OpcuaServerReadonlyContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.logs = []
        self.endpoint = f"opc.tcp://127.0.0.1:{_available_port()}"
        self.server = GatewayOpcuaServer(
            endpoint=self.endpoint,
            log_callback=lambda message, level="INFO": self.logs.append(
                (level, message)
            ),
        )
        await self.server.start()
        self.node_id = await self.server.add_readonly_variable(
            tag_id="tag-1",
            display_name="Speed",
            value=321,
            variant_type=ua.VariantType.UInt16,
        )

    async def asyncTearDown(self):
        await self.server.stop()

    async def _assert_client_write_is_rejected(self, client: Client) -> None:
        async with client:
            node = client.get_node(self.node_id)
            self.assertEqual(await node.read_value(), 321)
            with self.assertRaises(ua.UaStatusCodeError):
                await node.write_value(999, ua.VariantType.UInt16)
            self.assertEqual(await node.read_value(), 321)

    def _security_events(self) -> list[dict]:
        return [
            json.loads(message.split(" ", 1)[1])
            for _level, message in self.logs
            if message.startswith("SECURITY_WRITE_REJECTED")
        ]

    async def test_standard_clients_are_distinct_and_source_value_is_unchanged(self):
        await self._assert_client_write_is_rejected(Client(self.endpoint))
        await self._assert_client_write_is_rejected(Client(self.endpoint))

        events = self._security_events()
        self.assertEqual(len(events), 2)
        self.assertEqual({event["protocol"] for event in events}, {"OPCUA"})
        self.assertEqual(
            {event["request_type"] for event in events}, {"node_write"}
        )
        self.assertTrue(all(event["address"].startswith("ns=") for event in events))
        clients = {event["client"] for event in events}
        self.assertEqual(len(clients), 2)
        self.assertTrue(all(client.startswith("opcua-127.0.0.1:") for client in clients))

    async def test_remote_admin_cannot_bypass_readonly_boundary(self):
        client = Client(self.endpoint)
        client.set_user("admin")
        client.set_password("not-used-for-authorization")
        await self._assert_client_write_is_rejected(client)
        self.assertEqual(len(self._security_events()), 1)


class GatewayRuntimeProductIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_starts_both_readonly_outputs_and_stops_them(self):
        opcua_endpoint = f"opc.tcp://127.0.0.1:{_available_port()}"
        runtime = GatewayOutputRuntime(
            _Config(
                {
                    "modbus_tcp_server": {
                        "enable": True,
                        "host": "127.0.0.1",
                        "port": 0,
                    },
                    "opcua_server": {
                        "enable": True,
                        "endpoint": opcua_endpoint,
                    },
                }
            ),
        )
        runtime.start()
        try:
            self.assertTrue(runtime.is_running())
            self.assertIsNotNone(runtime.modbus_server)
            self.assertIsNotNone(runtime.opcua_server)
            self.assertIsNotNone(runtime.opcua_system_node_id)

            modbus_client = ModbusTcpClient(
                "127.0.0.1",
                port=runtime.modbus_server.port,
                timeout=2,
            )
            try:
                self.assertTrue(modbus_client.connect())
                read_only = modbus_client.read_holding_registers(
                    0, count=1, device_id=1
                )
                self.assertEqual(read_only.registers, [1])
                self.assertTrue(
                    modbus_client.write_register(
                        0, 0, device_id=1
                    ).isError()
                )
            finally:
                modbus_client.close()

            async with Client(opcua_endpoint) as opcua_client:
                node = opcua_client.get_node(runtime.opcua_system_node_id)
                self.assertTrue(await node.read_value())
                with self.assertRaises(ua.UaStatusCodeError):
                    await node.write_value(False, ua.VariantType.Boolean)
        finally:
            runtime.stop()

        self.assertFalse(runtime.is_running())


if __name__ == "__main__":
    unittest.main()
