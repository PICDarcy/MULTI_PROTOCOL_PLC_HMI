"""Issue #12：完整 OPC UA 輸出契約與穩定節點。"""

from __future__ import annotations

import asyncio
import queue
import time
import unittest
from datetime import datetime, timedelta, timezone

from asyncua import Client, ua
from asyncua.ua.uaerrors import UaStatusCodeError

from core.data_model import GatewayModel, PointValue
from core.gateway_opcua_adapter import GatewayOpcuaOutputAdapter
from core.modbus_codec import encode_modbus_value
from core.value_bus import ValueBus
from test_support.protocol_harness import LocalProtocolHarness


_NAMESPACE_URI = "urn:picdarcy:multi-protocol-plc-hmi:gateway"


class _DetailedSubscriptionHandler:
    def __init__(self) -> None:
        self.events: queue.Queue[tuple[str, object, ua.DataValue]] = queue.Queue()

    def datachange_notification(self, node, value, data) -> None:
        data_value = data.monitored_item.Value
        self.events.put((node.nodeid.to_string(), value, data_value))

    def drain(self) -> None:
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                return


class _ModelConfig:
    def __init__(self, model: dict[str, object]) -> None:
        self._model = GatewayModel.from_dict(model)

    def get_gateway_model(self) -> GatewayModel:
        return self._model


class _RecordingOpcuaServer:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, object]] = {}
        self.values: dict[str, dict[str, object]] = {}

    async def add_readonly_variable(
        self,
        *,
        tag_id,
        display_name,
        value,
        variant_type,
        device_id="",
        device_name="",
    ):
        self.nodes[str(tag_id)] = {
            "display_name": display_name,
            "value": value,
            "variant_type": variant_type,
            "device_id": str(device_id),
            "device_name": str(device_name),
        }
        return ua.NodeId(str(tag_id), 2)

    async def publish_value(
        self,
        *,
        tag_id,
        value,
        variant_type,
        quality="Good",
        source_timestamp=None,
        server_timestamp=None,
    ) -> None:
        self.values[str(tag_id)] = {
            "value": value,
            "variant_type": variant_type,
            "quality": quality,
            "source_timestamp": source_timestamp,
            "server_timestamp": server_timestamp,
        }


def _point(
    *,
    tag_id: str,
    data_type: str,
    value: object,
    quality: str = "Good",
    source_timestamp: datetime | None = None,
    server_timestamp: datetime | None = None,
) -> PointValue:
    timestamp = source_timestamp or datetime.now(timezone.utc)
    return PointValue(
        point_key=f"MODBUS_TCP::contract::{tag_id}",
        protocol="MODBUS_TCP",
        source_name="source",
        device_name="device",
        point_name=tag_id,
        address_text=tag_id,
        value=value,
        data_type=data_type,
        tag_id=tag_id,
        connection_id="conn-contract",
        device_id="device-contract",
        quality=quality,
        source_timestamp=timestamp,
        server_timestamp=server_timestamp or timestamp,
    )


def _gateway_model(
    *,
    device_name: str = "Line A PLC",
    tag_prefix: str = "",
    configured_node_suffix: str = "legacy",
) -> dict[str, object]:
    tag_specs = (
        ("running", "Running", "Boolean", "coil:0"),
        ("count", "Count", "UInt16", "holding_register:10"),
        ("signed", "Signed", "Int32", "holding_register:20"),
        ("ratio", "Ratio", "Float", "holding_register:30"),
        ("total", "Total", "Double", "holding_register:40"),
    )
    return {
        "connections": [
            {
                "connection_id": "conn-contract",
                "name": "Contract Modbus",
                "protocol": "MODBUS_TCP",
            }
        ],
        "devices": [
            {
                "device_id": "device-contract",
                "connection_id": "conn-contract",
                "name": device_name,
            }
        ],
        "tags": [
            {
                "tag_id": f"tag-{key}",
                "point_key": f"MODBUS_TCP::contract::{key}",
                "connection_id": "conn-contract",
                "device_id": "device-contract",
                "name": f"{tag_prefix}{display_name}",
                "source_protocol": "MODBUS_TCP",
                "source_address": source_address,
                "data_type": data_type,
                "opcua_output": {
                    "enabled": True,
                    # #12 requires the public NodeId to derive from tag_id,
                    # not this mutable legacy destination field.
                    "node_id": f"{configured_node_suffix}-{key}",
                    "browse_name": f"{tag_prefix}{display_name}",
                },
            }
            for key, display_name, data_type, source_address in tag_specs
        ],
    }


def _modbus_points() -> list[dict[str, object]]:
    return [
        {
            "enable": True,
            "name": "Running",
            "tag_id": "tag-running",
            "connection_id": "conn-contract",
            "device_id": "device-contract",
            "type": "coil",
            "address": 0,
            "count": 1,
            "data_type": "Boolean",
        },
        {
            "enable": True,
            "name": "Count",
            "tag_id": "tag-count",
            "connection_id": "conn-contract",
            "device_id": "device-contract",
            "type": "holding_register",
            "address": 10,
            "count": 1,
            "data_type": "UInt16",
        },
        {
            "enable": True,
            "name": "Signed",
            "tag_id": "tag-signed",
            "connection_id": "conn-contract",
            "device_id": "device-contract",
            "type": "holding_register",
            "address": 20,
            "count": 2,
            "data_type": "Int32",
        },
        {
            "enable": True,
            "name": "Ratio",
            "tag_id": "tag-ratio",
            "connection_id": "conn-contract",
            "device_id": "device-contract",
            "type": "holding_register",
            "address": 30,
            "count": 2,
            "data_type": "Float",
        },
        {
            "enable": True,
            "name": "Total",
            "tag_id": "tag-total",
            "connection_id": "conn-contract",
            "device_id": "device-contract",
            "type": "holding_register",
            "address": 40,
            "count": 4,
            "data_type": "Double",
        },
    ]


def _write_source_values(
    harness: LocalProtocolHarness,
    *,
    running: bool = True,
    count: int = 65000,
    signed: int = -1234567,
    ratio: float = 12.5,
    total: float = 987654.125,
) -> dict[str, object]:
    harness.set_modbus_source_coils(0, [running])
    harness.set_modbus_source_registers(10, encode_modbus_value(count, "UInt16"))
    harness.set_modbus_source_registers(20, encode_modbus_value(signed, "Int32"))
    harness.set_modbus_source_registers(30, encode_modbus_value(ratio, "Float"))
    harness.set_modbus_source_registers(40, encode_modbus_value(total, "Double"))
    return {
        "tag-running": running,
        "tag-count": count,
        "tag-signed": signed,
        "tag-ratio": ratio,
        "tag-total": total,
    }


class GatewayOpcuaOutputAdapterFullScalarTests(unittest.IsolatedAsyncioTestCase):
    async def test_bindings_use_stable_tag_ids_device_hierarchy_and_all_scalar_types(self):
        model = _gateway_model(
            device_name="Renamed Device",
            tag_prefix="Renamed ",
            configured_node_suffix="changed-mapping",
        )
        bus = ValueBus()
        server = _RecordingOpcuaServer()
        adapter = GatewayOpcuaOutputAdapter(
            _ModelConfig(model),
            bus,
            server,
            asyncio.get_running_loop(),
        )
        await adapter.start()
        self.addCleanup(adapter.stop)

        expected_types = {
            "tag-running": ua.VariantType.Boolean,
            "tag-count": ua.VariantType.UInt16,
            "tag-signed": ua.VariantType.Int32,
            "tag-ratio": ua.VariantType.Float,
            "tag-total": ua.VariantType.Double,
        }
        self.assertEqual(set(expected_types), set(server.nodes))
        for tag_id, variant_type in expected_types.items():
            self.assertEqual(variant_type, server.nodes[tag_id]["variant_type"])
            self.assertEqual("device-contract", server.nodes[tag_id]["device_id"])
            self.assertEqual("Renamed Device", server.nodes[tag_id]["device_name"])

        source_time = datetime(2026, 8, 8, 1, 2, 3, tzinfo=timezone.utc)
        server_time = source_time + timedelta(milliseconds=50)
        points = (
            _point(tag_id="tag-running", data_type="Boolean", value=True),
            _point(tag_id="tag-count", data_type="UInt16", value=7),
            _point(tag_id="tag-signed", data_type="Int32", value=-8),
            _point(tag_id="tag-ratio", data_type="Float", value=1.25),
            _point(
                tag_id="tag-total",
                data_type="Double",
                value=2.5,
                quality="BadNoCommunication",
                source_timestamp=source_time,
                server_timestamp=server_time,
            ),
        )
        for point in points:
            bus.publish(point)

        deadline = time.monotonic() + 2.0
        while len(server.values) < len(points) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        self.assertEqual(set(expected_types), set(server.values))
        self.assertEqual(
            "BadNoCommunication",
            server.values["tag-total"]["quality"],
        )
        self.assertEqual(
            source_time,
            server.values["tag-total"]["source_timestamp"],
        )
        self.assertEqual(
            server_time,
            server.values["tag-total"]["server_timestamp"],
        )


class OpcuaOutputContractE2ETests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for_event(
        self,
        handler: _DetailedSubscriptionHandler,
        *,
        node_id: ua.NodeId,
        expected_value: object | None = None,
        expect_bad: bool | None = None,
        timeout: float = 5.0,
    ) -> tuple[object, ua.DataValue]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                event_node, value, data_value = await asyncio.to_thread(
                    handler.events.get,
                    True,
                    remaining,
                )
            except queue.Empty:
                break
            if event_node != node_id.to_string():
                continue
            if expected_value is not None and value != expected_value:
                continue
            if expect_bad is not None and data_value.StatusCode.is_bad() != expect_bad:
                continue
            return value, data_value
        self.fail(
            f"期限內未收到 node={node_id.to_string()} value={expected_value} "
            f"bad={expect_bad} 的 DataChange"
        )

    async def test_browse_tree_types_multi_client_reads_and_write_rejection(self):
        with LocalProtocolHarness(
            modbus_points=_modbus_points(),
            gateway_model=_gateway_model(),
        ) as harness:
            expected = _write_source_values(harness)
            await asyncio.to_thread(harness.poll_modbus_source_once)

            async with (
                Client(harness.gateway_opcua_endpoint) as first,
                Client(harness.gateway_opcua_endpoint) as second,
            ):
                namespace = await first.get_namespace_index(_NAMESPACE_URI)
                gateway_root = first.get_node(ua.NodeId("gateway", namespace))
                device = first.get_node(
                    ua.NodeId("device/device-contract", namespace)
                )
                root_children = {child.nodeid for child in await gateway_root.get_children()}
                self.assertIn(device.nodeid, root_children)
                self.assertEqual("Line A PLC", (await device.read_display_name()).Text)

                device_children = {
                    child.nodeid: (await child.read_browse_name()).Name
                    for child in await device.get_children()
                }
                expected_types = {
                    "tag-running": ua.VariantType.Boolean,
                    "tag-count": ua.VariantType.UInt16,
                    "tag-signed": ua.VariantType.Int32,
                    "tag-ratio": ua.VariantType.Float,
                    "tag-total": ua.VariantType.Double,
                }
                self.assertEqual(
                    {ua.NodeId(tag_id, namespace) for tag_id in expected_types},
                    set(device_children),
                )

                for tag_id, variant_type in expected_types.items():
                    first_node = first.get_node(ua.NodeId(tag_id, namespace))
                    second_node = second.get_node(ua.NodeId(tag_id, namespace))
                    first_value, second_value = await asyncio.gather(
                        first_node.read_value(),
                        second_node.read_value(),
                    )
                    self.assertEqual(expected[tag_id], first_value)
                    self.assertEqual(expected[tag_id], second_value)
                    self.assertEqual(
                        variant_type,
                        await first_node.read_data_type_as_variant_type(),
                    )

                running = first.get_node(ua.NodeId("tag-running", namespace))
                running_second = second.get_node(
                    ua.NodeId("tag-running", namespace)
                )
                first_handler = _DetailedSubscriptionHandler()
                second_handler = _DetailedSubscriptionHandler()
                first_subscription = await first.create_subscription(50, first_handler)
                second_subscription = await second.create_subscription(
                    50,
                    second_handler,
                )
                await first_subscription.subscribe_data_change(running)
                await second_subscription.subscribe_data_change(running_second)
                try:
                    await asyncio.sleep(0.2)
                    first_handler.drain()
                    second_handler.drain()
                    harness.set_modbus_source_coils(0, [False])
                    await asyncio.to_thread(harness.poll_modbus_source_once)
                    await asyncio.gather(
                        self._wait_for_event(
                            first_handler,
                            node_id=running.nodeid,
                            expected_value=False,
                            expect_bad=False,
                        ),
                        self._wait_for_event(
                            second_handler,
                            node_id=running_second.nodeid,
                            expected_value=False,
                            expect_bad=False,
                        ),
                    )
                    with self.assertRaises(UaStatusCodeError):
                        await running.write_value(True, ua.VariantType.Boolean)
                finally:
                    await first_subscription.delete()
                    await second_subscription.delete()

            self.assertTrue(
                any(
                    "SECURITY_WRITE_REJECTED" in message
                    and "tag-running" in message
                    for message in harness.logs
                )
            )

    async def test_value_quality_and_timestamps_follow_datachange_contract(self):
        with LocalProtocolHarness(
            modbus_points=_modbus_points(),
            gateway_model=_gateway_model(),
        ) as harness:
            _write_source_values(harness, count=100)
            await asyncio.to_thread(harness.poll_modbus_source_once)
            assert harness.value_bus is not None

            async with Client(harness.gateway_opcua_endpoint) as client:
                namespace = await client.get_namespace_index(_NAMESPACE_URI)
                node = client.get_node(ua.NodeId("tag-count", namespace))
                initial = await node.read_data_value(raise_on_bad_status=False)
                self.assertTrue(initial.StatusCode.is_good())
                self.assertIsNotNone(initial.SourceTimestamp)
                self.assertIsNotNone(initial.ServerTimestamp)

                handler = _DetailedSubscriptionHandler()
                subscription = await client.create_subscription(50, handler)
                await subscription.subscribe_data_change(node)
                try:
                    await asyncio.sleep(0.2)
                    handler.drain()
                    source_time = datetime(
                        2026,
                        8,
                        8,
                        2,
                        3,
                        4,
                        tzinfo=timezone.utc,
                    )
                    first_server_time = source_time + timedelta(milliseconds=10)
                    second_server_time = source_time + timedelta(milliseconds=20)

                    harness.value_bus.publish(
                        _point(
                            tag_id="tag-count",
                            data_type="UInt16",
                            value=100,
                            quality="Good",
                            source_timestamp=source_time,
                            server_timestamp=first_server_time,
                        )
                    )
                    await asyncio.sleep(0.2)
                    handler.drain()

                    # Only ServerTimestamp changes: default StatusValue monitored
                    # item must not report a DataChange.
                    harness.value_bus.publish(
                        _point(
                            tag_id="tag-count",
                            data_type="UInt16",
                            value=100,
                            quality="Good",
                            source_timestamp=source_time,
                            server_timestamp=second_server_time,
                        )
                    )
                    with self.assertRaises(queue.Empty):
                        await asyncio.to_thread(handler.events.get, True, 0.4)

                    harness.value_bus.publish(
                        _point(
                            tag_id="tag-count",
                            data_type="UInt16",
                            value=101,
                            quality="Good",
                            source_timestamp=source_time,
                            server_timestamp=second_server_time,
                        )
                    )
                    _, value_change = await self._wait_for_event(
                        handler,
                        node_id=node.nodeid,
                        expected_value=101,
                        expect_bad=False,
                    )
                    self.assertEqual(source_time, value_change.SourceTimestamp)
                    self.assertEqual(second_server_time, value_change.ServerTimestamp)

                    bad_server_time = second_server_time + timedelta(milliseconds=10)
                    harness.value_bus.publish(
                        _point(
                            tag_id="tag-count",
                            data_type="UInt16",
                            value=101,
                            quality="BadNoCommunication",
                            source_timestamp=source_time,
                            server_timestamp=bad_server_time,
                        )
                    )
                    _, quality_change = await self._wait_for_event(
                        handler,
                        node_id=node.nodeid,
                        expected_value=101,
                        expect_bad=True,
                    )
                    self.assertEqual(source_time, quality_change.SourceTimestamp)
                    self.assertEqual(bad_server_time, quality_change.ServerTimestamp)
                    current = await node.read_data_value(raise_on_bad_status=False)
                    self.assertTrue(current.StatusCode.is_bad())
                    self.assertEqual(101, current.Value.Value)
                finally:
                    await subscription.delete()

    async def test_stable_node_id_survives_display_rename_restart_and_legacy_mapping_change(self):
        observed: list[str] = []
        for model in (
            _gateway_model(
                device_name="Original Device",
                tag_prefix="Original ",
                configured_node_suffix="legacy-a",
            ),
            _gateway_model(
                device_name="Renamed Device",
                tag_prefix="Renamed ",
                configured_node_suffix="legacy-b",
            ),
        ):
            with LocalProtocolHarness(
                modbus_points=_modbus_points(),
                gateway_model=model,
            ) as harness:
                async with Client(harness.gateway_opcua_endpoint) as client:
                    namespace = await client.get_namespace_index(_NAMESPACE_URI)
                    node = client.get_node(ua.NodeId("tag-count", namespace))
                    observed.append(node.nodeid.to_string())
                    self.assertEqual(
                        model["tags"][1]["opcua_output"]["browse_name"],
                        (await node.read_browse_name()).Name,
                    )

        self.assertEqual(observed[0], observed[1])
        self.assertTrue(observed[0].endswith(";s=tag-count"))


if __name__ == "__main__":
    unittest.main()
