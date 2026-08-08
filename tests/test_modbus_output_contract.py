"""Modbus TCP 輸出 scalar 編碼與安全位址配置契約測試。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.config_manager import ConfigManager
from core.data_model import (
    CanonicalTag,
    Connection,
    Device,
    GatewayModel,
    ModbusTcpOutputMapping,
    PointValue,
    allocate_modbus_output_addresses,
)
from core.gateway_modbus_adapter import GatewayModbusOutputAdapter
from core.modbus_codec import (
    decode_modbus_value,
    encode_modbus_value,
    modbus_output_description,
    modbus_output_register_count,
)
from core.value_bus import ValueBus


SCALAR_CASES = (
    ("Byte", 0x7A, 1),
    ("SByte", -12, 1),
    ("Int16", -1234, 1),
    ("UInt16", 54321, 1),
    ("Int32", -12345678, 2),
    ("UInt32", 0x89ABCDEF, 2),
    ("Float", 123.25, 2),
    ("Int64", -123456789012345, 4),
    ("UInt64", 0xFEDCBA9876543210, 4),
    ("Double", -9876.5, 4),
)


class _ModelConfig:
    def __init__(self, model: GatewayModel) -> None:
        self._model = model

    def get_gateway_model(self) -> GatewayModel:
        return self._model


class _RecordingServer:
    def __init__(self) -> None:
        self.coils: dict[int, tuple[list[bool], str]] = {}
        self.registers: dict[int, tuple[list[int], str]] = {}

    def set_coils(self, address, values, *, target):
        self.coils[int(address)] = (list(values), str(target))

    def set_holding_registers(self, address, values, *, target):
        self.registers[int(address)] = (list(values), str(target))


class ModbusOutputCodecContractTests(unittest.TestCase):
    def test_supported_scalars_use_fixed_register_counts_and_round_trip(self):
        for data_type, value, expected_count in SCALAR_CASES:
            with self.subTest(data_type=data_type):
                self.assertEqual(
                    expected_count,
                    modbus_output_register_count(data_type),
                )
                registers = encode_modbus_value(value, data_type)
                self.assertEqual(expected_count, len(registers))
                decoded = decode_modbus_value(
                    registers,
                    data_type,
                    area="holding_register",
                )
                if data_type in {"Float", "Double"}:
                    self.assertAlmostEqual(value, decoded, places=6)
                else:
                    self.assertEqual(value, decoded)

    def test_byte_and_word_order_combinations_have_known_register_layouts(self):
        expected = {
            ("big", "big"): [0x1122, 0x3344],
            ("little", "big"): [0x2211, 0x4433],
            ("big", "little"): [0x3344, 0x1122],
            ("little", "little"): [0x4433, 0x2211],
        }
        for orders, registers in expected.items():
            byte_order, word_order = orders
            with self.subTest(byte_order=byte_order, word_order=word_order):
                self.assertEqual(
                    registers,
                    encode_modbus_value(
                        0x11223344,
                        "UInt32",
                        byte_order=byte_order,
                        word_order=word_order,
                    ),
                )
                self.assertEqual(
                    0x11223344,
                    decode_modbus_value(
                        registers,
                        "UInt32",
                        area="holding_register",
                        byte_order=byte_order,
                        word_order=word_order,
                    ),
                )

    def test_every_supported_scalar_round_trips_all_order_combinations(self):
        for data_type, value, _expected_count in SCALAR_CASES:
            for byte_order in ("big", "little"):
                for word_order in ("big", "little"):
                    with self.subTest(
                        data_type=data_type,
                        byte_order=byte_order,
                        word_order=word_order,
                    ):
                        registers = encode_modbus_value(
                            value,
                            data_type,
                            byte_order=byte_order,
                            word_order=word_order,
                        )
                        decoded = decode_modbus_value(
                            registers,
                            data_type,
                            area="holding_register",
                            byte_order=byte_order,
                            word_order=word_order,
                        )
                        if data_type in {"Float", "Double"}:
                            self.assertAlmostEqual(value, decoded, places=6)
                        else:
                            self.assertEqual(value, decoded)

    def test_encoding_does_not_coerce_between_python_scalar_types(self):
        with self.assertRaisesRegex(TypeError, "FLOAT.*跨型別"):
            encode_modbus_value(1, "Float")
        with self.assertRaisesRegex(TypeError, "UINT16.*跨型別"):
            encode_modbus_value(1.0, "UInt16")
        with self.assertRaisesRegex(TypeError, "UINT16.*跨型別"):
            encode_modbus_value(True, "UInt16")

    def test_ui_support_description_is_explicit_for_supported_and_unsupported_types(self):
        self.assertEqual("支援：Coil（1 bit）", modbus_output_description("Boolean"))
        self.assertEqual(
            "支援：Holding Register × 2",
            modbus_output_description("UInt32"),
        )
        self.assertIn("不支援", modbus_output_description("String"))

    def test_variable_length_types_are_not_modbus_output_types(self):
        for data_type in ("String", "ByteString", "UInt16[]", "Structure"):
            with self.subTest(data_type=data_type):
                with self.assertRaisesRegex(ValueError, "不支援"):
                    modbus_output_register_count(data_type)
                with self.assertRaisesRegex(ValueError, "不支援"):
                    encode_modbus_value("value", data_type)


class ModbusOutputMappingContractTests(unittest.TestCase):
    def _base(self):
        connection = Connection("conn", "Source", "OPCUA")
        device = Device("device", connection.connection_id, "PLC")
        return connection, device

    def _tag(
        self,
        tag_id: str,
        data_type: str,
        *,
        mapping: ModbusTcpOutputMapping,
    ) -> CanonicalTag:
        return CanonicalTag(
            tag_id=tag_id,
            point_key=f"OPCUA::source::{tag_id}",
            connection_id="conn",
            device_id="device",
            name=tag_id,
            source_protocol="OPCUA",
            source_address=f"ns=2;s={tag_id}",
            data_type=data_type,
            modbus_tcp_output=mapping,
        )

    def _model(self, *tags: CanonicalTag) -> GatewayModel:
        connection, device = self._base()
        return GatewayModel((connection,), (device,), tuple(tags))

    def test_boolean_defaults_to_coil_numeric_defaults_to_register(self):
        boolean = self._tag(
            "running",
            "Boolean",
            mapping=ModbusTcpOutputMapping(enabled=True, address=0),
        )
        number = self._tag(
            "count",
            "UInt16",
            mapping=ModbusTcpOutputMapping(enabled=True, address=100),
        )

        model = self._model(boolean, number)

        self.assertEqual("coil", model.tags[0].modbus_tcp_output.area)
        self.assertEqual(
            "holding_register",
            model.tags[1].modbus_tcp_output.area,
        )

    def test_unsupported_type_is_disabled_and_serialized_with_clear_reason(self):
        unsupported = self._tag(
            "message",
            "String",
            mapping=ModbusTcpOutputMapping(),
        )

        model = self._model(unsupported)
        mapping = model.tags[0].modbus_tcp_output

        self.assertFalse(mapping.enabled)
        self.assertFalse(mapping.supported)
        self.assertIn("String", mapping.unsupported_reason)
        serialized = model.to_dict()["tags"][0]["modbus_tcp_output"]
        self.assertFalse(serialized["supported"])
        self.assertIn("不支援", serialized["unsupported_reason"])

    def test_enabled_unsupported_type_is_rejected_without_cross_type_conversion(self):
        with self.assertRaisesRegex(ValueError, "String.*不支援"):
            self._model(
                self._tag(
                    "message",
                    "String",
                    mapping=ModbusTcpOutputMapping(
                        enabled=True,
                        area="holding_register",
                        address=0,
                    ),
                )
            )

    def test_cross_area_mappings_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Boolean.*holding_register"):
            self._tag(
                "bad-bool",
                "Boolean",
                mapping=ModbusTcpOutputMapping(
                    enabled=True,
                    area="holding_register",
                    address=0,
                ),
            )
        with self.assertRaisesRegex(ValueError, "Int32.*coil"):
            self._tag(
                "bad-number",
                "Int32",
                mapping=ModbusTcpOutputMapping(
                    enabled=True,
                    area="coil",
                    address=0,
                ),
            )

    def test_auto_allocation_uses_first_contiguous_free_ranges(self):
        fixed = self._tag(
            "fixed-double",
            "Double",
            mapping=ModbusTcpOutputMapping(
                enabled=True,
                address=2,
            ),
        )
        auto_int32 = self._tag(
            "auto-int32",
            "Int32",
            mapping=ModbusTcpOutputMapping(
                enabled=True,
                auto_allocate=True,
            ),
        )
        auto_uint16 = self._tag(
            "auto-uint16",
            "UInt16",
            mapping=ModbusTcpOutputMapping(
                enabled=True,
                auto_allocate=True,
            ),
        )

        allocated = allocate_modbus_output_addresses(
            (fixed, auto_int32, auto_uint16),
            register_start=0,
            register_end=7,
        )
        by_id = {str(tag.tag_id): tag for tag in allocated}

        self.assertEqual(0, by_id["auto-int32"].modbus_tcp_output.address)
        self.assertEqual(6, by_id["auto-uint16"].modbus_tcp_output.address)
        self.assertEqual(2, by_id["fixed-double"].modbus_tcp_output.address)

    def test_overlap_out_of_range_and_insufficient_space_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "left.*right.*0-1"):
            self._model(
                self._tag(
                    "left",
                    "Int32",
                    mapping=ModbusTcpOutputMapping(enabled=True, address=0),
                ),
                self._tag(
                    "right",
                    "UInt16",
                    mapping=ModbusTcpOutputMapping(enabled=True, address=1),
                ),
            )

        with self.assertRaisesRegex(ValueError, "65534-65537"):
            self._model(
                self._tag(
                    "too-wide",
                    "Double",
                    mapping=ModbusTcpOutputMapping(enabled=True, address=65534),
                )
            )

        auto = self._tag(
            "no-room",
            "Double",
            mapping=ModbusTcpOutputMapping(enabled=True, auto_allocate=True),
        )
        with self.assertRaisesRegex(ValueError, "no-room.*連續空間"):
            allocate_modbus_output_addresses(
                (auto,),
                register_start=0,
                register_end=2,
            )


class ModbusOutputPersistenceContractTests(unittest.TestCase):
    @staticmethod
    def _raw_model(*tags):
        return {
            "connections": [
                {
                    "connection_id": "conn",
                    "name": "Source",
                    "protocol": "OPCUA",
                }
            ],
            "devices": [
                {
                    "device_id": "device",
                    "connection_id": "conn",
                    "name": "PLC",
                }
            ],
            "tags": list(tags),
        }

    @staticmethod
    def _raw_tag(tag_id, data_type, mapping):
        return {
            "tag_id": tag_id,
            "point_key": f"OPCUA::source::{tag_id}",
            "connection_id": "conn",
            "device_id": "device",
            "name": tag_id,
            "source_protocol": "OPCUA",
            "source_address": f"ns=2;s={tag_id}",
            "data_type": data_type,
            "modbus_tcp_output": mapping,
        }

    def test_save_allocates_with_configured_range_and_persists_address(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            manager = ConfigManager(path)
            config = manager.get_config()
            config["gateway_outputs"]["modbus_tcp_server"].update(
                {
                    "register_start": 100,
                    "register_end": 105,
                }
            )
            config["gateway_model"] = self._raw_model(
                self._raw_tag(
                    "auto-double",
                    "Double",
                    {
                        "enabled": True,
                        "auto_allocate": True,
                    },
                )
            )

            manager.save_config(config)
            reloaded = ConfigManager(path).get_gateway_model()

        self.assertEqual(100, reloaded.tags[0].modbus_tcp_output.address)
        self.assertEqual("big", reloaded.tags[0].modbus_tcp_output.byte_order)
        self.assertEqual("big", reloaded.tags[0].modbus_tcp_output.word_order)

    def test_save_rejects_raw_overlapping_mappings_before_replacing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            manager = ConfigManager(path)
            original = path.read_text(encoding="utf-8")
            config = manager.get_config()
            config["gateway_model"] = self._raw_model(
                self._raw_tag(
                    "left",
                    "Int32",
                    {"enabled": True, "address": 0},
                ),
                self._raw_tag(
                    "right",
                    "UInt16",
                    {"enabled": True, "address": 1},
                ),
            )

            with self.assertRaisesRegex(ValueError, "left.*right.*重疊"):
                manager.save_config(config)

            self.assertEqual(original, path.read_text(encoding="utf-8"))


class GatewayModbusOutputAdapterScalarTests(unittest.TestCase):
    def test_adapter_publishes_all_supported_scalar_types_without_conversion(self):
        connection = Connection("conn", "Source", "OPCUA")
        device = Device("device", connection.connection_id, "PLC")
        tags = [
            CanonicalTag(
                tag_id="bool",
                point_key="OPCUA::source::bool",
                connection_id=connection.connection_id,
                device_id=device.device_id,
                name="bool",
                source_protocol="OPCUA",
                source_address="ns=2;s=bool",
                data_type="Boolean",
                modbus_tcp_output=ModbusTcpOutputMapping(
                    enabled=True,
                    address=5,
                ),
            )
        ]
        address = 100
        expected_registers: dict[int, list[int]] = {}
        for index, (data_type, value, count) in enumerate(SCALAR_CASES):
            tag_id = f"value-{index}"
            mapping = ModbusTcpOutputMapping(
                enabled=True,
                address=address,
                byte_order="little" if index % 2 else "big",
                word_order="little" if index % 3 == 0 else "big",
            )
            tags.append(
                CanonicalTag(
                    tag_id=tag_id,
                    point_key=f"OPCUA::source::{tag_id}",
                    connection_id=connection.connection_id,
                    device_id=device.device_id,
                    name=tag_id,
                    source_protocol="OPCUA",
                    source_address=f"ns=2;s={tag_id}",
                    data_type=data_type,
                    modbus_tcp_output=mapping,
                )
            )
            expected_registers[address] = encode_modbus_value(
                value,
                data_type,
                byte_order=mapping.byte_order,
                word_order=mapping.word_order,
            )
            address += count

        model = GatewayModel((connection,), (device,), tuple(tags))
        bus = ValueBus()
        server = _RecordingServer()
        adapter = GatewayModbusOutputAdapter(_ModelConfig(model), bus, server)
        adapter.start()
        self.addCleanup(adapter.stop)

        bus.publish(self._point("bool", "Boolean", True))
        for index, (data_type, value, _count) in enumerate(SCALAR_CASES):
            bus.publish(self._point(f"value-{index}", data_type, value))

        self.assertEqual(([True], "bool"), server.coils[5])
        for start, registers in expected_registers.items():
            self.assertEqual((registers, self._tag_for_start(model, start)), server.registers[start])

    def test_adapter_allocates_pending_auto_mapping_from_plain_model_provider(self):
        connection = Connection("conn", "Source", "OPCUA")
        device = Device("device", connection.connection_id, "PLC")
        tag = CanonicalTag(
            tag_id="count",
            point_key="OPCUA::source::count",
            connection_id=connection.connection_id,
            device_id=device.device_id,
            name="count",
            source_protocol="OPCUA",
            source_address="ns=2;s=count",
            data_type="UInt16",
            modbus_tcp_output=ModbusTcpOutputMapping(
                enabled=True,
                auto_allocate=True,
            ),
        )
        model = GatewayModel((connection,), (device,), (tag,))
        self.assertIsNone(model.tags[0].modbus_tcp_output.address)
        bus = ValueBus()
        server = _RecordingServer()
        adapter = GatewayModbusOutputAdapter(_ModelConfig(model), bus, server)
        adapter.start()
        self.addCleanup(adapter.stop)

        bus.publish(self._point("count", "UInt16", 12))

        self.assertEqual(([12], "count"), server.registers[0])

    def test_adapter_rejects_cross_type_python_values_without_writing(self):
        connection = Connection("conn", "Source", "OPCUA")
        device = Device("device", connection.connection_id, "PLC")
        tag = CanonicalTag(
            tag_id="count",
            point_key="OPCUA::source::count",
            connection_id=connection.connection_id,
            device_id=device.device_id,
            name="count",
            source_protocol="OPCUA",
            source_address="ns=2;s=count",
            data_type="UInt16",
            modbus_tcp_output=ModbusTcpOutputMapping(
                enabled=True,
                address=100,
            ),
        )
        model = GatewayModel((connection,), (device,), (tag,))
        logs = []
        bus = ValueBus()
        server = _RecordingServer()
        adapter = GatewayModbusOutputAdapter(
            _ModelConfig(model),
            bus,
            server,
            lambda message, level: logs.append((level, message)),
        )
        adapter.start()
        self.addCleanup(adapter.stop)

        bus.publish(self._point("count", "UInt16", 12.0))

        self.assertEqual({}, server.registers)
        self.assertTrue(any(level == "ERROR" for level, _message in logs))

    @staticmethod
    def _point(tag_id: str, data_type: str, value) -> PointValue:
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
            connection_id="conn",
            device_id="device",
            quality="Good",
        )

    @staticmethod
    def _tag_for_start(model: GatewayModel, start: int) -> str:
        for tag in model.tags:
            if tag.modbus_tcp_output.address == start:
                return str(tag.tag_id)
        raise AssertionError(f"missing tag at {start}")


if __name__ == "__main__":
    unittest.main()
