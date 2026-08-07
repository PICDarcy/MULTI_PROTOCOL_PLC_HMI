"""Gateway canonical model 的相容與設定往返測試。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.config_manager import ConfigManager
from core.data_model import (
    CanonicalTag,
    Connection,
    Device,
    GatewayModel,
    ModbusTcpOutputMapping,
    OpcuaOutputMapping,
    PointValue,
)


class CanonicalModelTests(unittest.TestCase):
    def _model(self) -> GatewayModel:
        connection = Connection(
            connection_id="conn-line-a",
            name="Line A OPC UA",
            protocol="OPCUA",
            settings={"endpoint": "opc.tcp://127.0.0.1:4840"},
        )
        device = Device(
            device_id="device-boiler-1",
            connection_id=connection.connection_id,
            name="Boiler 1",
        )
        tag = CanonicalTag(
            tag_id="tag-boiler-temperature",
            point_key="OPCUA::Line%20A::ns%3D2%3Bs%3Dtemperature",
            connection_id=connection.connection_id,
            device_id=device.device_id,
            name="Temperature",
            source_protocol="OPCUA",
            source_address="ns=2;s=temperature",
            data_type="Float",
            quality="Good",
            source_timestamp=datetime(2026, 8, 8, 1, 2, 3, tzinfo=timezone.utc),
            server_timestamp=datetime(2026, 8, 8, 1, 2, 4, tzinfo=timezone.utc),
            modbus_tcp_output=ModbusTcpOutputMapping(
                enabled=True,
                area="holding_register",
                address=100,
            ),
            opcua_output=OpcuaOutputMapping(
                enabled=True,
                node_id="ns=2;s=boiler-temperature",
                browse_name="BoilerTemperature",
            ),
        )
        return GatewayModel(
            connections=(connection,),
            devices=(device,),
            tags=(tag,),
        )

    def test_model_round_trip_preserves_ids_source_and_both_output_mappings(self):
        original = self._model()

        restored = GatewayModel.from_dict(original.to_dict())

        self.assertEqual(original, restored)
        tag = restored.tags[0]
        self.assertEqual("conn-line-a", tag.connection_id)
        self.assertEqual("device-boiler-1", tag.device_id)
        self.assertEqual("OPCUA", tag.source_protocol)
        self.assertEqual("ns=2;s=temperature", tag.source_address)
        self.assertTrue(tag.modbus_tcp_output.enabled)
        self.assertEqual(100, tag.modbus_tcp_output.address)
        self.assertTrue(tag.opcua_output.enabled)
        self.assertEqual("ns=2;s=boiler-temperature", tag.opcua_output.node_id)

    def test_config_manager_persists_canonical_model_without_identifier_changes(self):
        model = self._model()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            manager = ConfigManager(path)
            manager.set_gateway_model(model)
            manager.save_config()

            reloaded = ConfigManager(path).get_gateway_model()

        self.assertEqual(model, reloaded)
        self.assertEqual(
            "tag-boiler-temperature",
            reloaded.tags[0].tag_id,
        )
        self.assertEqual(
            "OPCUA::Line%20A::ns%3D2%3Bs%3Dtemperature",
            reloaded.tags[0].point_key,
        )

    def test_canonical_tag_adapts_to_existing_point_value_consumers(self):
        tag = self._model().tags[0]

        point = tag.to_point_value(
            value=42.5,
            source_name="Line A OPC UA",
            device_name="Boiler 1",
        )

        self.assertIsInstance(point, PointValue)
        self.assertEqual(tag.point_key, point.point_key)
        self.assertEqual("42.5", point.value_text)
        self.assertEqual("Good", point.status_text)
        self.assertEqual(tag.tag_id, point.tag_id)
        self.assertEqual(tag.connection_id, point.connection_id)
        self.assertEqual(tag.device_id, point.device_id)
        self.assertEqual(tag.source_timestamp, point.source_timestamp)
        self.assertEqual(tag.server_timestamp, point.server_timestamp)

    def test_model_rejects_broken_connection_and_device_references(self):
        model = self._model()
        broken = CanonicalTag.from_dict(
            {
                **model.tags[0].to_dict(),
                "device_id": "missing-device",
            }
        )

        with self.assertRaisesRegex(ValueError, "device_id"):
            GatewayModel(
                connections=model.connections,
                devices=model.devices,
                tags=(broken,),
            )

    def test_enabled_output_mappings_require_a_destination(self):
        with self.assertRaisesRegex(ValueError, "address"):
            ModbusTcpOutputMapping(enabled=True)
        with self.assertRaisesRegex(ValueError, "node_id"):
            OpcuaOutputMapping(enabled=True)

    def test_frozen_canonical_metadata_cannot_be_mutated_through_nested_values(self):
        tag = CanonicalTag.from_dict(
            {
                **self._model().tags[0].to_dict(),
                "metadata": {"engineering": {"unit": "C"}},
            }
        )

        with self.assertRaises(TypeError):
            tag.metadata["engineering"]["unit"] = "F"

    def test_new_gateway_timestamp_keeps_old_positional_constructor_order(self):
        source_time = datetime(2026, 8, 8, tzinfo=timezone.utc)
        server_time = datetime(2026, 8, 8, 0, 0, 1, tzinfo=timezone.utc)
        modbus = ModbusTcpOutputMapping(enabled=True, address=100)
        opcua = OpcuaOutputMapping(enabled=True, node_id="ns=2;s=legacy")

        tag = CanonicalTag(
            "tag-legacy",
            "OPCUA::legacy::node",
            "conn-line-a",
            "device-boiler-1",
            "Legacy",
            "OPCUA",
            "ns=2;s=legacy",
            "Double",
            "Good",
            source_time,
            server_time,
            False,
            modbus,
            opcua,
            {"legacy": True},
        )

        self.assertFalse(tag.enabled)
        self.assertEqual(modbus, tag.modbus_tcp_output)
        self.assertEqual(opcua, tag.opcua_output)
        self.assertEqual({"legacy": True}, tag.metadata)
        self.assertIsNone(tag.gateway_timestamp)


if __name__ == "__main__":
    unittest.main()
