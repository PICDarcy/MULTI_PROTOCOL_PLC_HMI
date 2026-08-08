"""Issue #13 映射編輯、持久化與重新掃描合併契約。"""

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
    OpcuaOutputMapping,
)
from core.gateway_mapping_manager import GatewayMappingManager


class GatewayMappingPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.config_path = Path(self.temporary_directory.name) / "config.json"
        self.config_manager = ConfigManager(self.config_path)
        config = self.config_manager.get_config()
        config["gateway_outputs"]["modbus_tcp_server"].update(
            {
                "register_start": 10,
                "register_end": 100,
            }
        )
        config["gateway_model"] = self._base_model().to_dict()
        self.config_manager.save_config(config)
        self.manager = GatewayMappingManager(self.config_manager)

    @staticmethod
    def _connection() -> Connection:
        return Connection(
            connection_id="conn-line-a",
            name="Line A",
            protocol="MODBUS_TCP",
        )

    @classmethod
    def _device(cls) -> Device:
        return Device(
            device_id="device-plc-a",
            connection_id=cls._connection().connection_id,
            name="PLC A",
        )

    @classmethod
    def _tag(
        cls,
        *,
        tag_id: str,
        point_key: str,
        name: str,
        data_type: str,
        address: int | None,
        auto_allocate: bool = False,
    ) -> CanonicalTag:
        return CanonicalTag(
            tag_id=tag_id,
            point_key=point_key,
            connection_id=cls._connection().connection_id,
            device_id=cls._device().device_id,
            name=name,
            source_protocol="MODBUS_TCP",
            source_address=point_key,
            data_type=data_type,
            modbus_tcp_output=ModbusTcpOutputMapping(
                enabled=True,
                address=address,
                auto_allocate=auto_allocate,
            ),
            opcua_output=OpcuaOutputMapping(
                enabled=True,
                node_id=tag_id,
                browse_name=name,
            ),
        )

    @classmethod
    def _base_model(cls) -> GatewayModel:
        return GatewayModel(
            connections=(cls._connection(),),
            devices=(cls._device(),),
            tags=(
                cls._tag(
                    tag_id="tag-pressure",
                    point_key="MODBUS_TCP::line-a::pressure",
                    name="Pressure",
                    data_type="UInt16",
                    address=10,
                ),
                cls._tag(
                    tag_id="tag-temperature",
                    point_key="MODBUS_TCP::line-a::temperature",
                    name="Temperature",
                    data_type="Float",
                    address=11,
                ),
            ),
        )

    def test_user_edits_both_outputs_independently_and_restart_restores_them(self):
        updated = self.manager.update_tag(
            "tag-pressure",
            name="Line Pressure",
            enabled=True,
            publish_modbus=False,
            publish_opcua=True,
            modbus_address=30,
            byte_order="little",
            word_order="little",
            opcua_browse_name="LinePressure",
        )

        self.assertEqual("Line Pressure", updated.name)
        self.assertFalse(updated.modbus_tcp_output.enabled)
        self.assertTrue(updated.opcua_output.enabled)
        self.assertEqual(30, updated.modbus_tcp_output.address)
        self.assertEqual("little", updated.modbus_tcp_output.byte_order)
        self.assertEqual("little", updated.modbus_tcp_output.word_order)
        self.assertEqual("LinePressure", updated.opcua_output.browse_name)

        restarted = GatewayMappingManager(ConfigManager(self.config_path))
        restored = restarted.get_tag("tag-pressure")
        self.assertEqual(updated, restored)
        self.assertEqual("tag-pressure", restored.tag_id)
        self.assertEqual(
            "MODBUS_TCP::line-a::pressure",
            restored.point_key,
        )

        modbus_only = restarted.update_tag(
            "tag-pressure",
            publish_modbus=True,
            publish_opcua=False,
        )
        self.assertTrue(modbus_only.modbus_tcp_output.enabled)
        self.assertFalse(modbus_only.opcua_output.enabled)

    def test_invalid_edit_does_not_replace_file_or_in_memory_model(self):
        before_bytes = self.config_path.read_bytes()
        before_model = self.config_manager.get_gateway_model()

        with self.assertRaisesRegex(ValueError, "位址重疊"):
            self.manager.update_tag(
                "tag-pressure",
                modbus_address=11,
            )

        self.assertEqual(before_bytes, self.config_path.read_bytes())
        self.assertEqual(before_model, self.config_manager.get_gateway_model())

    def test_rescan_preserves_custom_mapping_and_reserves_missing_tag_address(self):
        discovery = GatewayModel(
            connections=(self._connection(),),
            devices=(self._device(),),
            tags=(
                self._tag(
                    # Scanners may rediscover with a transient candidate ID;
                    # point_key is the stable source identity used for merging.
                    tag_id="candidate-pressure",
                    point_key="MODBUS_TCP::line-a::pressure",
                    name="Pressure From Device",
                    data_type="UInt16",
                    address=90,
                ),
                self._tag(
                    tag_id="tag-flow",
                    point_key="MODBUS_TCP::line-a::flow",
                    name="Flow",
                    data_type="Int32",
                    address=None,
                    auto_allocate=True,
                ),
            ),
        )

        merged = self.manager.merge_discovery(discovery)
        by_id = {str(tag.tag_id): tag for tag in merged.tags}

        pressure = by_id["tag-pressure"]
        self.assertEqual("Pressure", pressure.name)
        self.assertEqual(10, pressure.modbus_tcp_output.address)
        self.assertEqual("tag-pressure", pressure.opcua_output.node_id)
        self.assertTrue(pressure.source_online)

        missing = by_id["tag-temperature"]
        self.assertFalse(missing.source_online)
        self.assertEqual(11, missing.modbus_tcp_output.address)
        self.assertTrue(missing.modbus_tcp_output.enabled)

        new_tag = by_id["tag-flow"]
        self.assertTrue(new_tag.source_online)
        self.assertEqual(13, new_tag.modbus_tcp_output.address)

        restarted = ConfigManager(self.config_path).get_gateway_model()
        self.assertEqual(merged, restarted)

    def test_type_change_is_pending_and_cannot_silently_expand_fixed_mapping(self):
        changed = self._tag(
            tag_id="candidate-pressure",
            point_key="MODBUS_TCP::line-a::pressure",
            name="Pressure",
            data_type="Double",
            address=90,
        )
        discovery = GatewayModel(
            connections=(self._connection(),),
            devices=(self._device(),),
            tags=(changed,),
        )

        merged = self.manager.merge_discovery(discovery)
        pressure = next(
            tag for tag in merged.tags if tag.tag_id == "tag-pressure"
        )

        self.assertEqual("UInt16", pressure.data_type)
        self.assertEqual("Double", pressure.pending_source_data_type)
        self.assertTrue(pressure.mapping_confirmation_required)
        self.assertEqual(10, pressure.modbus_tcp_output.address)

        before_bytes = self.config_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "位址重疊"):
            self.manager.confirm_pending_data_type("tag-pressure")
        self.assertEqual(before_bytes, self.config_path.read_bytes())
        still_pending = self.manager.get_tag("tag-pressure")
        self.assertEqual("UInt16", still_pending.data_type)
        self.assertEqual("Double", still_pending.pending_source_data_type)


if __name__ == "__main__":
    unittest.main()
