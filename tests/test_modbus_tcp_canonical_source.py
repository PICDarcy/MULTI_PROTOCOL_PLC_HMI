"""Modbus TCP canonical 來源解碼與真實協定 E2E。"""

from __future__ import annotations

import struct
import unittest

from core.modbus_codec import decode_modbus_value, register_count_for_type
from core.modbus_manager import ModbusRtuManager
from core.value_bus import ValueBus
from test_support.protocol_harness import LocalProtocolHarness


def _registers(payload: bytes) -> list[int]:
    return [
        int.from_bytes(payload[index : index + 2], "big")
        for index in range(0, len(payload), 2)
    ]


class ModbusCodecTests(unittest.TestCase):
    def test_scalar_types_and_explicit_orders(self):
        self.assertIs(True, decode_modbus_value([1], "Boolean"))
        self.assertEqual(0x12, decode_modbus_value([0x1234], "Byte"))
        self.assertEqual(-2, decode_modbus_value([0xFE00], "SByte"))
        self.assertEqual(-1234, decode_modbus_value([0xFB2E], "Int16"))
        self.assertEqual(50000, decode_modbus_value([50000], "UInt16"))

        int32_bytes = struct.pack(">i", -12345678)
        int32_words = _registers(int32_bytes)
        self.assertEqual(
            -12345678,
            decode_modbus_value(int32_words, "Int32"),
        )
        self.assertEqual(
            0x12345678,
            decode_modbus_value([0x1234, 0x5678], "UInt32"),
        )

        float_bytes = struct.pack(">f", 12.5)
        little_bytes_in_words = b"".join(
            float_bytes[index : index + 2][::-1]
            for index in range(0, len(float_bytes), 2)
        )
        self.assertAlmostEqual(
            12.5,
            decode_modbus_value(
                _registers(little_bytes_in_words),
                "Float",
                byte_order="little",
                word_order="big",
            ),
        )

        self.assertEqual(
            -123456789012345,
            decode_modbus_value(
                _registers(struct.pack(">q", -123456789012345)),
                "Int64",
            ),
        )
        self.assertEqual(
            123456789012345,
            decode_modbus_value(
                _registers(struct.pack(">Q", 123456789012345)),
                "UInt64",
            ),
        )
        double_words = _registers(struct.pack(">d", 1234.5))
        self.assertAlmostEqual(
            1234.5,
            decode_modbus_value(
                list(reversed(double_words)),
                "Double",
                word_order="little",
            ),
        )

    def test_coils_are_boolean_and_area_is_explicit(self):
        self.assertIs(True, decode_modbus_value([1], "Boolean", area="coil"))
        self.assertIs(
            False,
            decode_modbus_value([0], "Boolean", area="discrete_input"),
        )

    def test_legacy_string_raw_and_multi_value_contract_remains_compatible(self):
        self.assertEqual(
            "ABCD",
            decode_modbus_value([0x4142, 0x4344], "String"),
        )
        self.assertEqual([1, 2], decode_modbus_value([1, 2], "Raw"))
        self.assertEqual([1, 2], decode_modbus_value([1, 2], "Auto"))
        self.assertEqual([1, 2], decode_modbus_value([1, 2], "UInt16"))
        self.assertEqual(
            [True, False],
            decode_modbus_value([1, 0], "Boolean", area="coil"),
        )

    def test_public_codec_rejects_invalid_area_order_and_short_response(self):
        with self.assertRaisesRegex(ValueError, "資料區"):
            register_count_for_type("UInt16", area="register-ish")
        with self.assertRaisesRegex(ValueError, "byte_order"):
            decode_modbus_value([1], "UInt16", byte_order="middle")
        with self.assertRaisesRegex(ValueError, "需要2個Register"):
            decode_modbus_value([1], "Int32")

    def test_legacy_rtu_point_without_type_keeps_holding_register_default(self):
        class Config:
            def get_section(self, name, default=None):
                if name != "modbus_rtu":
                    return default
                return {
                    "enable": True,
                    "port": "SIMULATED",
                    "devices": [
                        {
                            "name": "Legacy",
                            "station_id": 1,
                            "points": [
                                {
                                    "name": "LegacyPoint",
                                    "address": 0,
                                    "data_type": "UInt16",
                                }
                            ],
                        }
                    ],
                }

        class StubManager(ModbusRtuManager):
            def _ensure_client_unlocked(self):
                return object()

            def _read_raw_unlocked(self, client, device, point):
                return [321]

        bus = ValueBus()
        manager = StubManager(Config(), bus)

        result = manager.read_all_once()

        self.assertEqual(1, result["success"])
        self.assertEqual(321, bus.get_latest_list()[0].value)


class ModbusTcpCanonicalSourceE2ETests(unittest.TestCase):
    def test_coil_and_register_scalars_publish_with_shared_response_time(self):
        points = [
            {
                "name": "Run",
                "tag_id": "tag-run",
                "type": "coil",
                "address": 0,
                "data_type": "Boolean",
            },
            {
                "name": "Count",
                "tag_id": "tag-count",
                "type": "holding_register",
                "address": 10,
                "data_type": "UInt16",
            },
            {
                "name": "SignedCount",
                "tag_id": "tag-signed-count",
                "type": "holding_register",
                "address": 11,
                "data_type": "Int32",
            },
            {
                "name": "Rate",
                "tag_id": "tag-rate",
                "type": "holding_register",
                "address": 13,
                "data_type": "Float",
                "byte_order": "little",
                "word_order": "big",
            },
            {
                "name": "Total",
                "tag_id": "tag-total",
                "type": "holding_register",
                "address": 15,
                "data_type": "Double",
                "word_order": "little",
            },
        ]
        with LocalProtocolHarness(modbus_points=points) as harness:
            harness.set_modbus_source_coils(0, [True])
            signed_words = _registers(struct.pack(">i", -7654321))
            float_bytes = struct.pack(">f", 98.25)
            float_words = _registers(
                b"".join(
                    float_bytes[index : index + 2][::-1]
                    for index in range(0, len(float_bytes), 2)
                )
            )
            double_words = list(reversed(_registers(struct.pack(">d", 4567.5))))
            harness.set_modbus_source_registers(
                10,
                [54321, *signed_words, *float_words, *double_words],
            )

            result = harness.poll_modbus_source_once()
            values = {
                point.tag_id: point
                for point in harness.value_bus.get_latest_list()
                if point.protocol == "MODBUS_TCP"
            }

        self.assertEqual(5, result["success"])
        self.assertIs(True, values["tag-run"].value)
        self.assertEqual(54321, values["tag-count"].value)
        self.assertEqual(-7654321, values["tag-signed-count"].value)
        self.assertAlmostEqual(98.25, values["tag-rate"].value)
        self.assertAlmostEqual(4567.5, values["tag-total"].value)
        register_points = [
            values[tag_id]
            for tag_id in (
                "tag-count",
                "tag-signed-count",
                "tag-rate",
                "tag-total",
            )
        ]
        self.assertEqual(1, len({point.source_timestamp for point in register_points}))
        for point in values.values():
            self.assertEqual("conn-simulated-modbus", point.connection_id)
            self.assertEqual("device-simulated-modbus", point.device_id)
            self.assertEqual("Good", point.quality)
            self.assertIsNotNone(point.server_timestamp)
            self.assertIn("response_address", point.raw_config)
            self.assertIn("raw_values", point.raw_config)

    def test_bad_point_and_failed_area_do_not_block_valid_group(self):
        points = [
            {
                "name": "Valid",
                "tag_id": "tag-valid",
                "type": "holding_register",
                "address": 10,
                "data_type": "UInt16",
            },
            {
                "name": "BadConfig",
                "tag_id": "tag-bad-config",
                "type": "holding_register",
                "address": "not-an-address",
                "data_type": "UInt16",
            },
            {
                "name": "UnsupportedBySimulator",
                "tag_id": "tag-failed-area",
                "type": "input_register",
                "address": 20,
                "data_type": "UInt16",
            },
        ]
        with LocalProtocolHarness(modbus_points=points) as harness:
            harness.set_modbus_source_registers(10, [123])

            result = harness.poll_modbus_source_once()
            values = {
                point.tag_id: point
                for point in harness.value_bus.get_latest_list()
                if point.protocol == "MODBUS_TCP"
            }

        self.assertEqual({"success": 1, "failed": 2, "total": 3}, result)
        self.assertEqual(123, values["tag-valid"].value)
        self.assertEqual("Good", values["tag-valid"].quality)
        self.assertEqual("Bad", values["tag-bad-config"].quality)
        self.assertEqual("Bad", values["tag-failed-area"].quality)


if __name__ == "__main__":
    unittest.main()
