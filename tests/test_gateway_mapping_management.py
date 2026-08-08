"""Issue #13：Canonical Tag 映射管理、持久化與重新掃描合併。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.config_manager import ConfigManager
from core.data_model import GatewayModel
from core.gateway_mapping_manager import GatewayMappingManager


def _initial_model() -> dict[str, object]:
    return {
        "connections": [
            {
                "connection_id": "conn-source",
                "name": "Source",
                "protocol": "MODBUS_TCP",
            }
        ],
        "devices": [
            {
                "device_id": "device-source",
                "connection_id": "conn-source",
                "name": "PLC",
            }
        ],
        "tags": [
            {
                "tag_id": "tag-count",
                "point_key": "MODBUS_TCP::source::count",
                "connection_id": "conn-source",
                "device_id": "device-source",
                "name": "User Count",
                "source_protocol": "MODBUS_TCP",
                "source_address": "holding_register:10",
                "data_type": "UInt16",
                "enabled": True,
                "modbus_tcp_output": {
                    "enabled": True,
                    "area": "holding_register",
                    "address": 10,
                    "byte_order": "big",
                    "word_order": "big",
                },
                "opcua_output": {
                    "enabled": True,
                    "node_id": "legacy-count-node",
                    "browse_name": "PublicCount",
                },
            },
            {
                "tag_id": "tag-running",
                "point_key": "MODBUS_TCP::source::running",
                "connection_id": "conn-source",
                "device_id": "device-source",
                "name": "Running",
                "source_protocol": "MODBUS_TCP",
                "source_address": "coil:0",
                "data_type": "Boolean",
                "enabled": True,
                "modbus_tcp_output": {
                    "enabled": True,
                    "area": "coil",
                    "address": 5,
                },
                "opcua_output": {
                    "enabled": True,
                    "node_id": "legacy-running-node",
                    "browse_name": "Running",
                },
            },
            {
                "tag_id": "tag-pressure",
                "point_key": "MODBUS_TCP::source::pressure",
                "connection_id": "conn-source",
                "device_id": "device-source",
                "name": "Pressure",
                "source_protocol": "MODBUS_TCP",
                "source_address": "holding_register:12",
                "data_type": "Int32",
                "enabled": True,
                "modbus_tcp_output": {
                    "enabled": True,
                    "area": "holding_register",
                    "address": 12,
                },
                "opcua_output": {
                    "enabled": True,
                    "node_id": "legacy-pressure-node",
                    "browse_name": "Pressure",
                },
            },
        ],
    }


class GatewayMappingManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.config_path = Path(self.temporary_directory.name) / "config.json"
        self.config = ConfigManager(self.config_path)
        candidate = self.config.get_config()
        candidate["gateway_outputs"]["modbus_tcp_server"].update(
            {
                "coil_start": 0,
                "coil_end": 20,
                "register_start": 10,
                "register_end": 30,
            }
        )
        candidate["gateway_model"] = _initial_model()
        self.config.save_config(candidate)
        self.reload_calls: list[str] = []
        self.manager = GatewayMappingManager(
            self.config,
            reload_callback=lambda: self.reload_calls.append("reload"),
        )

    @staticmethod
    def _tag(model: GatewayModel, tag_id: str):
        return next(tag for tag in model.tags if str(tag.tag_id) == tag_id)

    def test_user_edits_both_outputs_independently_and_restart_round_trips(self):
        updated = self.manager.update_tag(
            "tag-count",
            name="Production Count",
            enabled=False,
            publish_modbus=False,
            modbus_address=20,
            modbus_byte_order="little",
            modbus_word_order="little",
            publish_opcua=True,
            opcua_browse_name="ProductionCount",
        )

        self.assertEqual("Production Count", updated.name)
        self.assertFalse(updated.enabled)
        self.assertFalse(updated.modbus_tcp_output.enabled)
        self.assertEqual(20, updated.modbus_tcp_output.address)
        self.assertEqual("little", updated.modbus_tcp_output.byte_order)
        self.assertEqual("little", updated.modbus_tcp_output.word_order)
        self.assertTrue(updated.opcua_output.enabled)
        self.assertEqual("ProductionCount", updated.opcua_output.browse_name)
        self.assertEqual("legacy-count-node", updated.opcua_output.node_id)
        self.assertEqual(["reload"], self.reload_calls)

        self.manager.update_tag(
            "tag-count",
            publish_modbus=True,
            publish_opcua=False,
        )
        restarted = ConfigManager(self.config_path).get_gateway_model()
        reloaded = self._tag(restarted, "tag-count")
        self.assertEqual("Production Count", reloaded.name)
        self.assertTrue(reloaded.modbus_tcp_output.enabled)
        self.assertFalse(reloaded.opcua_output.enabled)
        self.assertEqual(20, reloaded.modbus_tcp_output.address)
        self.assertEqual("little", reloaded.modbus_tcp_output.byte_order)
        self.assertEqual("little", reloaded.modbus_tcp_output.word_order)
        self.assertEqual("legacy-count-node", reloaded.opcua_output.node_id)
        self.assertEqual("MODBUS_TCP::source::count", str(reloaded.point_key))
        self.assertEqual(["reload", "reload"], self.reload_calls)

    def test_conflicting_edit_is_atomic_and_does_not_reload_runtime(self):
        before_bytes = self.config_path.read_bytes()
        before_memory = self.config.get_config()

        with self.assertRaisesRegex(ValueError, "重疊"):
            self.manager.update_tag(
                "tag-pressure",
                modbus_address=10,
            )

        self.assertEqual(before_bytes, self.config_path.read_bytes())
        self.assertEqual(before_memory, self.config.get_config())
        self.assertEqual([], self.reload_calls)

    def test_rescan_preserves_user_mapping_marks_missing_offline_and_reserves_address(self):
        result = self.manager.merge_discovered_tags(
            [
                {
                    "point_key": "MODBUS_TCP::source::count",
                    "name": "Scanner Count Name",
                    "source_protocol": "MODBUS_TCP",
                    "source_address": "holding_register:100",
                    "data_type": "UInt16",
                },
                {
                    "point_key": "MODBUS_TCP::source::energy",
                    "name": "Energy",
                    "source_protocol": "MODBUS_TCP",
                    "source_address": "holding_register:40",
                    "data_type": "Double",
                },
            ],
            connection_id="conn-source",
            device_id="device-source",
        )

        self.assertEqual(("tag-count",), result.updated_tag_ids)
        self.assertEqual(("tag-pressure", "tag-running"), result.offline_tag_ids)
        self.assertEqual(1, len(result.added_tag_ids))

        count = self._tag(result.model, "tag-count")
        self.assertEqual("User Count", count.name)
        self.assertEqual("holding_register:100", count.source_address)
        self.assertEqual(10, count.modbus_tcp_output.address)
        self.assertEqual("legacy-count-node", count.opcua_output.node_id)
        self.assertTrue(count.enabled)
        self.assertTrue(count.metadata["source_online"])

        pressure = self._tag(result.model, "tag-pressure")
        self.assertFalse(pressure.enabled)
        self.assertFalse(pressure.metadata["source_online"])
        self.assertEqual("offline", pressure.metadata["source_state"])
        self.assertEqual(12, pressure.modbus_tcp_output.address)

        running = self._tag(result.model, "tag-running")
        self.assertFalse(running.enabled)
        self.assertEqual(5, running.modbus_tcp_output.address)

        added = self._tag(result.model, result.added_tag_ids[0])
        self.assertEqual("Double", added.data_type)
        self.assertEqual(14, added.modbus_tcp_output.address)
        self.assertEqual(str(added.tag_id), added.opcua_output.node_id)
        self.assertTrue(added.modbus_tcp_output.enabled)
        self.assertTrue(added.opcua_output.enabled)

        restarted = ConfigManager(self.config_path).get_gateway_model()
        restarted_added = self._tag(restarted, str(added.tag_id))
        self.assertEqual(14, restarted_added.modbus_tcp_output.address)
        self.assertEqual(str(added.tag_id), restarted_added.opcua_output.node_id)
        self.assertEqual(["reload"], self.reload_calls)

    def test_type_change_is_pending_disabled_and_keeps_old_width_and_address(self):
        result = self.manager.merge_discovered_tags(
            [
                {
                    "point_key": "MODBUS_TCP::source::count",
                    "name": "Count",
                    "source_protocol": "MODBUS_TCP",
                    "source_address": "holding_register:10",
                    "data_type": "Double",
                },
                {
                    "point_key": "MODBUS_TCP::source::running",
                    "name": "Running",
                    "source_protocol": "MODBUS_TCP",
                    "source_address": "coil:0",
                    "data_type": "Boolean",
                },
                {
                    "point_key": "MODBUS_TCP::source::pressure",
                    "name": "Pressure",
                    "source_protocol": "MODBUS_TCP",
                    "source_address": "holding_register:12",
                    "data_type": "Int32",
                },
            ],
            connection_id="conn-source",
            device_id="device-source",
        )

        self.assertEqual(("tag-count",), result.pending_type_change_tag_ids)
        count = self._tag(result.model, "tag-count")
        self.assertEqual("UInt16", count.data_type)
        self.assertEqual(10, count.modbus_tcp_output.address)
        self.assertFalse(count.enabled)
        self.assertEqual("pending_type_change", count.metadata["mapping_state"])
        self.assertEqual("Double", count.metadata["observed_data_type"])
        self.assertTrue(count.metadata["desired_enabled"])

        recovered = self.manager.merge_discovered_tags(
            [
                {
                    "point_key": "MODBUS_TCP::source::count",
                    "name": "Count",
                    "source_protocol": "MODBUS_TCP",
                    "source_address": "holding_register:10",
                    "data_type": "UInt16",
                },
                {
                    "point_key": "MODBUS_TCP::source::running",
                    "name": "Running",
                    "source_protocol": "MODBUS_TCP",
                    "source_address": "coil:0",
                    "data_type": "Boolean",
                },
                {
                    "point_key": "MODBUS_TCP::source::pressure",
                    "name": "Pressure",
                    "source_protocol": "MODBUS_TCP",
                    "source_address": "holding_register:12",
                    "data_type": "Int32",
                },
            ],
            connection_id="conn-source",
            device_id="device-source",
        )
        count = self._tag(recovered.model, "tag-count")
        self.assertTrue(count.enabled)
        self.assertEqual("confirmed", count.metadata["mapping_state"])
        self.assertEqual("UInt16", count.metadata["observed_data_type"])
        self.assertEqual(10, count.modbus_tcp_output.address)

    def test_offline_or_pending_tag_cannot_be_reactivated_before_confirmation(self):
        offline = self.manager.merge_discovered_tags(
            [],
            connection_id="conn-source",
            device_id="device-source",
        )
        self.assertFalse(self._tag(offline.model, "tag-count").enabled)

        edited = self.manager.update_tag("tag-count", enabled=True)
        self.assertFalse(edited.enabled)
        self.assertTrue(edited.metadata["desired_enabled"])

        recovered = self.manager.merge_discovered_tags(
            [
                {
                    "point_key": "MODBUS_TCP::source::count",
                    "name": "Count",
                    "source_protocol": "MODBUS_TCP",
                    "source_address": "holding_register:10",
                    "data_type": "UInt16",
                }
            ],
            connection_id="conn-source",
            device_id="device-source",
        )
        self.assertTrue(self._tag(recovered.model, "tag-count").enabled)

    def test_tag_id_for_new_source_identity_is_deterministic(self):
        discovery = [
            {
                "point_key": "MODBUS_TCP::source::new-point",
                "name": "New Point",
                "source_protocol": "MODBUS_TCP",
                "source_address": "holding_register:50",
                "data_type": "UInt16",
            }
        ]
        first = self.manager.merge_discovered_tags(
            discovery,
            connection_id="conn-source",
            device_id="device-source",
        )
        first_id = first.added_tag_ids[0]

        # Restore the original file and merge the exact same stable source again.
        candidate = self.config.get_config()
        candidate["gateway_model"] = _initial_model()
        self.config.save_config(candidate)
        second = self.manager.merge_discovered_tags(
            discovery,
            connection_id="conn-source",
            device_id="device-source",
        )
        self.assertEqual(first_id, second.added_tag_ids[0])


class GatewayRuntimeReloadTests(unittest.TestCase):
    def test_reload_restarts_only_an_already_running_runtime(self):
        import threading

        from core.gateway_runtime import GatewayOutputRuntime

        runtime = object.__new__(GatewayOutputRuntime)
        runtime._lock = threading.RLock()
        runtime._running = True
        calls: list[str] = []

        def stop():
            calls.append("stop")
            runtime._running = False

        def start():
            calls.append("start")
            runtime._running = True

        runtime.stop = stop
        runtime.start = start
        GatewayOutputRuntime.reload(runtime)
        self.assertEqual(["stop", "start"], calls)

        calls.clear()
        runtime._running = False
        GatewayOutputRuntime.reload(runtime)
        self.assertEqual(["stop"], calls)


class GatewayMappingUiWiringTests(unittest.TestCase):
    def test_app_exposes_gateway_mapping_page(self):
        from ui.app import App

        self.assertIn(
            (
                "gateway_mapping",
                "Gateway Tag映射",
                "ui.gateway_mapping_page",
                "GatewayMappingPage",
            ),
            App.PAGE_SPECS,
        )

    def test_mapping_page_imports_without_starting_tk(self):
        from ui.gateway_mapping_page import GatewayMappingPage

        self.assertEqual("GatewayMappingPage", GatewayMappingPage.__name__)


if __name__ == "__main__":
    unittest.main()
