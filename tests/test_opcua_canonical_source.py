"""OPC UA 來源經真實協定邊界發布 canonical PointValue。"""

from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from asyncua import Client, ua

from test_support.protocol_harness import LocalProtocolHarness


class OpcuaCanonicalSourceE2ETests(unittest.TestCase):
    def test_poll_preserves_variant_quality_timestamps_and_stable_ids(self):
        received = []
        future_source_time = datetime.now(timezone.utc) + timedelta(minutes=2)
        opcua_server_time = future_source_time - timedelta(seconds=1)

        with LocalProtocolHarness() as harness:
            harness.set_opcua_source_value(
                81.25,
                variant_type=ua.VariantType.Double,
                status_code=ua.StatusCodes.Good,
                source_timestamp=future_source_time,
                server_timestamp=opcua_server_time,
            )
            harness.value_bus.subscribe(received.append)

            result = harness.poll_opcua_source_once()

            point = received[-1]
            self.assertEqual(81.25, result["value"])
            self.assertEqual(81.25, point.value)
            self.assertEqual("Double", point.data_type)
            self.assertEqual("Good", point.quality)
            self.assertEqual("Good", point.status_text)
            self.assertEqual(future_source_time, point.source_timestamp)
            self.assertIsInstance(point.server_timestamp, datetime)
            self.assertLessEqual(point.server_timestamp, point.gateway_timestamp)
            self.assertLess(point.gateway_timestamp, future_source_time)
            self.assertEqual("tag-simulated-temperature", point.tag_id)
            self.assertEqual("conn-simulated-opcua", point.connection_id)
            self.assertEqual("device-simulated-opcua", point.device_id)
            self.assertTrue(
                any("來源時間超前" in message for message in harness.logs)
            )

    def test_subscription_keeps_backward_source_time_and_uncertain_quality(self):
        received = []
        changed = threading.Event()
        old_source_time = datetime(2020, 1, 2, tzinfo=timezone.utc)

        def consume(point) -> None:
            if point.protocol == "OPCUA" and point.value == 79.5:
                received.append(point)
                changed.set()

        with LocalProtocolHarness() as harness:
            harness.value_bus.subscribe(consume)
            subscribed = harness.start_opcua_collection()
            self.assertTrue(subscribed["subscribed"])

            harness.set_opcua_source_value(
                79.5,
                variant_type=ua.VariantType.Double,
                status_code=ua.StatusCodes.Uncertain,
                source_timestamp=old_source_time,
                server_timestamp=datetime.now(timezone.utc),
            )

            self.assertTrue(changed.wait(3), "未收到OPC UA subscription資料")
            point = received[-1]
            self.assertEqual(old_source_time, point.source_timestamp)
            self.assertEqual("Uncertain", point.quality)
            self.assertEqual("Double", point.data_type)

    def test_subscription_disabled_falls_back_to_repeated_polling_contract(self):
        received = []
        changed = threading.Event()

        def consume(point) -> None:
            if point.protocol == "OPCUA" and point.value == 77.0:
                received.append(point)
                changed.set()

        with LocalProtocolHarness(
            opcua_subscribe=False,
            opcua_poll_interval=0.05,
        ) as harness:
            harness.value_bus.subscribe(consume)
            result = harness.start_opcua_collection()
            self.assertEqual("poll", result["mode"])

            harness.set_opcua_source_value(
                77.0,
                variant_type=ua.VariantType.Double,
                source_timestamp=None,
                server_timestamp=None,
            )

            self.assertTrue(changed.wait(3), "OPC UA fallback polling未發布資料")
            point = received[-1]
            self.assertEqual(point.gateway_timestamp, point.source_timestamp)
            self.assertEqual("Good", point.quality)
            stopped = harness.stop_opcua_collection()
            self.assertFalse(stopped["already_unsubscribed"])

    def test_invalid_opcua_null_time_falls_back_to_gateway_timestamp(self):
        received = []
        null_time = datetime(1601, 1, 1, tzinfo=timezone.utc)
        with LocalProtocolHarness() as harness:
            harness.set_opcua_source_value(
                75.0,
                source_timestamp=null_time,
                server_timestamp=None,
            )
            harness.value_bus.subscribe(received.append)

            harness.poll_opcua_source_once()

            point = received[-1]
            self.assertNotEqual(null_time, point.source_timestamp)
            self.assertEqual(point.gateway_timestamp, point.source_timestamp)

    def test_unsupported_subscription_falls_back_then_recovers_without_polling(self):
        changed = threading.Event()
        received = []

        def consume(point) -> None:
            if point.protocol == "OPCUA" and point.value == 76.0:
                received.append(point)
                changed.set()

        with LocalProtocolHarness(opcua_poll_interval=0.05) as harness:
            harness.value_bus.subscribe(consume)
            with patch.object(
                Client,
                "create_subscription",
                new_callable=AsyncMock,
                side_effect=RuntimeError("simulated subscription unsupported"),
            ):
                fallback = harness.start_opcua_collection()
            self.assertEqual("poll", fallback["mode"])

            recovered = harness.start_opcua_collection()
            self.assertTrue(recovered["subscribed"])
            harness.set_opcua_source_value(76.0)

            self.assertTrue(changed.wait(3))
            threading.Event().wait(0.2)
            self.assertEqual(1, len(received))


if __name__ == "__main__":
    unittest.main()
