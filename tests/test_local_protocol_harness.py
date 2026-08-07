"""可重複啟停的本機協定端到端測試骨架。"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import unittest

from pymodbus.client import ModbusTcpClient

from test_support.protocol_harness import LocalProtocolHarness


class LocalProtocolHarnessTests(unittest.TestCase):
    def test_starts_simulated_sources_gateway_and_standard_clients(self):
        with LocalProtocolHarness() as harness:
            source_client = ModbusTcpClient(
                "127.0.0.1",
                port=harness.modbus_source_port,
                timeout=2,
            )
            try:
                self.assertTrue(source_client.connect())
                response = source_client.read_holding_registers(
                    10,
                    count=1,
                    device_id=1,
                )
                self.assertFalse(response.isError())
                self.assertEqual([2468], response.registers)
            finally:
                source_client.close()

            self.assertEqual(73.5, asyncio.run(harness.read_opcua_source()))
            self.assertIs(True, asyncio.run(harness.read_gateway_opcua_system()))

    def test_smoke_moves_source_value_through_gateway_and_cleans_up(self):
        harness = LocalProtocolHarness()
        with harness:
            bound_ports = tuple(harness.ports.values())
            result = harness.poll_modbus_source_once()
            self.assertEqual(1, result["success"])

            gateway_client = ModbusTcpClient(
                "127.0.0.1",
                port=harness.gateway_modbus_port,
                timeout=2,
            )
            try:
                self.assertTrue(gateway_client.connect())
                response = gateway_client.read_holding_registers(
                    100,
                    count=1,
                    device_id=1,
                )
                self.assertFalse(response.isError())
                self.assertEqual([2468], response.registers)
            finally:
                gateway_client.close()
            temporary_config = harness.config_path

        self.assertFalse(temporary_config.exists())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self._harness_threads():
            time.sleep(0.01)
        self.assertEqual([], self._harness_threads())
        for port in bound_ports:
            self._assert_port_is_released(port)

    def test_ports_and_temporary_configuration_are_isolated_per_run(self):
        with LocalProtocolHarness() as first:
            first_ports = first.ports
            first_path = first.config_path
        with LocalProtocolHarness() as second:
            second_ports = second.ports
            second_path = second.config_path

        self.assertNotEqual(first_path, second_path)
        self.assertEqual(4, len(set(first_ports.values())))
        self.assertEqual(4, len(set(second_ports.values())))

    def test_context_cleanup_runs_when_test_body_fails(self):
        harness = LocalProtocolHarness()

        with self.assertRaisesRegex(RuntimeError, "intentional"):
            with harness:
                temporary_config = harness.config_path
                bound_ports = tuple(harness.ports.values())
                raise RuntimeError("intentional test failure")

        self.assertFalse(temporary_config.exists())
        self.assertEqual([], self._harness_threads())
        for port in bound_ports:
            self._assert_port_is_released(port)

    def test_partial_start_failure_cleans_started_source_and_tempdir(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            blocked_port = int(blocker.getsockname()[1])
            harness = LocalProtocolHarness(
                opcua_source_port=blocked_port,
            )

            with self.assertRaisesRegex(RuntimeError, "OPC UA來源啟動失敗"):
                harness.start()
            temporary_config = harness.config_path
            partial_ports = harness.allocated_ports

        self.assertFalse(temporary_config.exists())
        self.assertEqual([], self._harness_threads())
        self.assertEqual({"modbus_source"}, set(partial_ports))
        self._assert_port_is_released(partial_ports["modbus_source"])

    def test_default_ephemeral_ports_do_not_use_preflight_reservations(self):
        with LocalProtocolHarness() as harness:
            self.assertNotIn(0, harness.ports.values())
            self.assertEqual(4, len(set(harness.ports.values())))

    @staticmethod
    def _harness_threads() -> list[str]:
        prefixes = (
            "ProtocolHarness",
            "GatewayModbusTcpServer",
            "GatewayOpcuaOutput",
        )
        return sorted(
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith(prefixes)
        )

    def _assert_port_is_released(self, port: int) -> None:
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))


if __name__ == "__main__":
    unittest.main()
