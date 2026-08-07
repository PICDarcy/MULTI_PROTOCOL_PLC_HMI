"""第一版 Gateway 唯讀安全邊界的公開契約測試。"""

from __future__ import annotations

import json
import unittest

from core.gateway_security import (
    GatewayWriteRejected,
    ReadonlyGatewayPolicy,
)
from core.modbus_manager import ModbusRtuManager
from core.modbus_tcp_manager import ModbusTcpManager
from core.opcua_manager import OpcuaMultiServerManager
from core.value_bus import ValueBus


class _Config:
    def __init__(self, sections):
        self.sections = sections

    def get_section(self, name, default=None):
        return self.sections.get(name, default)


class ReadonlyGatewayPolicyTests(unittest.TestCase):
    def test_rejected_write_records_required_context_without_requested_value(self):
        records: list[tuple[str, str]] = []
        policy = ReadonlyGatewayPolicy(
            lambda message, level="INFO": records.append((level, message))
        )

        with self.assertRaises(GatewayWriteRejected):
            policy.reject_write(
                protocol="MODBUS_TCP",
                client="192.0.2.50:53000",
                target="PLC_1/速度設定",
                address="holding_register:10",
                request_type="function_code_6",
                requested_value="super-secret-value",
            )

        self.assertEqual(records[0][0], "WARNING")
        prefix, payload = records[0][1].split(" ", 1)
        self.assertEqual(prefix, "SECURITY_WRITE_REJECTED")
        event = json.loads(payload)
        self.assertEqual(event["protocol"], "MODBUS_TCP")
        self.assertEqual(event["client"], "192.0.2.50:53000")
        self.assertEqual(event["target"], "PLC_1/速度設定")
        self.assertEqual(event["address"], "holding_register:10")
        self.assertEqual(event["request_type"], "function_code_6")
        self.assertEqual(event["result"], "rejected_read_only")
        self.assertNotIn("super-secret-value", records[0][1])

    def test_gateway_capabilities_never_enable_device_or_output_writes(self):
        policy = ReadonlyGatewayPolicy()

        self.assertFalse(policy.capabilities.device_writes)
        self.assertFalse(policy.capabilities.modbus_server_writes)
        self.assertFalse(policy.capabilities.opcua_server_writes)


class SourceManagerWriteBoundaryTests(unittest.TestCase):
    def test_modbus_rtu_write_is_rejected_before_transport_access(self):
        logs = []
        manager = ModbusRtuManager(
            _Config(
                {
                    "modbus_rtu": {
                        "devices": [
                            {
                                "name": "RTU_PLC",
                                "station_id": 1,
                                "points": [
                                    {
                                        "name": "速度",
                                        "type": "holding_register",
                                        "address": 10,
                                        "writable": True,
                                    }
                                ],
                            }
                        ]
                    }
                }
            ),
            ValueBus(),
            lambda message, level="INFO": logs.append((level, message)),
        )
        point_key = next(iter(manager._points))
        manager._ensure_client_unlocked = lambda: self.fail("不應接觸 RTU Transport")

        with self.assertRaises(GatewayWriteRejected):
            manager.write_point(point_key, "123")

        self.assertIn("MODBUS_RTU", logs[-1][1])
        self.assertIn("holding_register:10", logs[-1][1])

    def test_modbus_tcp_write_is_rejected_before_socket_access(self):
        logs = []
        manager = ModbusTcpManager(
            _Config(
                {
                    "modbus_tcp": {
                        "devices": [
                            {
                                "name": "TCP_PLC",
                                "host": "192.0.2.10",
                                "port": 502,
                                "station_id": 1,
                                "points": [
                                    {
                                        "name": "啟動",
                                        "type": "coil",
                                        "address": 2,
                                        "writable": True,
                                    }
                                ],
                            }
                        ]
                    }
                }
            ),
            ValueBus(),
            lambda message, level="INFO": logs.append((level, message)),
        )
        point_key = next(iter(manager._points))
        manager._ensure_client_unlocked = lambda _device: self.fail(
            "不應接觸 TCP Socket"
        )

        with self.assertRaises(GatewayWriteRejected):
            manager.write_point(point_key, "true")

        self.assertIn("MODBUS_TCP", logs[-1][1])
        self.assertIn("coil:2", logs[-1][1])

    def test_opcua_write_is_rejected_before_background_io(self):
        logs = []
        manager = OpcuaMultiServerManager(
            _Config({"opcua": {"servers": []}}),
            ValueBus(),
            lambda message, level="INFO": logs.append((level, message)),
        )
        try:
            with self.assertRaises(GatewayWriteRejected):
                manager.write_node("source", "ns=2;s=Speed", "99", "UInt16")
        finally:
            manager.shutdown()

        self.assertIn("OPCUA", logs[-1][1])
        self.assertIn("ns=2;s=Speed", logs[-1][1])


if __name__ == "__main__":
    unittest.main()
