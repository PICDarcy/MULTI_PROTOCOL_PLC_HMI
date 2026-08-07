"""Modbus RTU canonical 來源與可控制序列 Transport 測試。"""

from __future__ import annotations

import struct
import threading
import unittest

from core.modbus_manager import ModbusRtuManager
from core.value_bus import ValueBus


class _Config:
    def __init__(self, section):
        self.section = section

    def get_section(self, name, default=None):
        return self.section if name == "modbus_rtu" else default


class _Response:
    def __init__(self, *, registers=None, bits=None):
        self.registers = list(registers or [])
        self.bits = list(bits or [])

    def isError(self):
        return False


class ControllableRtuTransport:
    """以記憶體資料區模擬同一條序列線上的多個 Station。"""

    def __init__(self):
        self.registers = {}
        self.bits = {}
        self.failed_stations = set()
        self.calls = []
        self.connect_count = 0
        self.close_count = 0
        self.read_event = threading.Event()

    def connect(self):
        self.connect_count += 1
        return True

    def close(self):
        self.close_count += 1

    def set_registers(self, station, area, address, values):
        memory = self.registers.setdefault((station, area), {})
        for offset, value in enumerate(values):
            memory[address + offset] = value

    def set_bits(self, station, area, address, values):
        memory = self.bits.setdefault((station, area), {})
        for offset, value in enumerate(values):
            memory[address + offset] = bool(value)

    @staticmethod
    def _station(kwargs):
        for key in ("device_id", "slave", "unit"):
            if key in kwargs:
                return int(kwargs[key])
        raise AssertionError("缺少 Modbus station/unit 參數")

    def _read(self, area, *, address, count, **kwargs):
        station = self._station(kwargs)
        self.calls.append((station, area, address, count))
        self.read_event.set()
        if station in self.failed_stations:
            raise ConnectionError(f"station {station} disconnected")
        if area in {"coil", "discrete_input"}:
            memory = self.bits.get((station, area), {})
            return _Response(
                bits=[memory.get(address + offset, False) for offset in range(count)]
            )
        memory = self.registers.get((station, area), {})
        return _Response(
            registers=[memory.get(address + offset, 0) for offset in range(count)]
        )

    def read_holding_registers(self, **kwargs):
        return self._read("holding_register", **kwargs)

    def read_input_registers(self, **kwargs):
        return self._read("input_register", **kwargs)

    def read_coils(self, **kwargs):
        return self._read("coil", **kwargs)

    def read_discrete_inputs(self, **kwargs):
        return self._read("discrete_input", **kwargs)


def _registers(payload: bytes):
    return [
        int.from_bytes(payload[index : index + 2], "big")
        for index in range(0, len(payload), 2)
    ]


def _section(*, enable=False, devices):
    return {
        "enable": enable,
        "port": "virtual://rs485-line-1",
        "baudrate": 19200,
        "poll_interval": 0.05,
        "timeout": 0.05,
        "connection_id": "conn-rs485-line-1",
        "devices": devices,
    }


class ModbusRtuCanonicalSourceTests(unittest.TestCase):
    def test_multi_station_grouped_reads_publish_canonical_values(self):
        float_payload = struct.pack(">f", 12.5)
        float_words_with_little_bytes = [
            int.from_bytes(float_payload[index : index + 2][::-1], "big")
            for index in range(0, len(float_payload), 2)
        ]
        transport = ControllableRtuTransport()
        transport.set_registers(
            1,
            "holding_register",
            10,
            [
                321,
                *float_words_with_little_bytes,
                *reversed(_registers(struct.pack(">I", 0x12345678))),
            ],
        )
        transport.set_bits(2, "coil", 3, [True])
        transport.set_bits(2, "discrete_input", 7, [False])
        section = _section(
            devices=[
                {
                    "name": "Mixer",
                    "device_id": "device-mixer",
                    "station_id": 1,
                    "points": [
                        {
                            "name": "BatchCount",
                            "tag_id": "tag-batch-count",
                            "type": "holding_register",
                            "address": 10,
                            "address_base": 0,
                            "data_type": "UInt16",
                        },
                        {
                            "name": "Temperature",
                            "tag_id": "tag-temperature",
                            "type": "holding_register",
                            "address": 11,
                            "address_base": 0,
                            "count": 2,
                            "data_type": "Float",
                            "byte_order": "little",
                            "word_order": "big",
                        },
                        {
                            "name": "Total",
                            "tag_id": "tag-total",
                            "type": "holding_register",
                            "address": 13,
                            "address_base": 0,
                            "count": 2,
                            "data_type": "UInt32",
                            "byte_order": "big",
                            "word_order": "little",
                        },
                    ],
                },
                {
                    "name": "Pump",
                    "device_id": "device-pump",
                    "station_id": 2,
                    "points": [
                        {
                            "name": "Running",
                            "tag_id": "tag-running",
                            "type": "coil",
                            "address": 3,
                            "address_base": 0,
                            "data_type": "Boolean",
                        },
                        {
                            "name": "Alarm",
                            "tag_id": "tag-alarm",
                            "type": "discrete_input",
                            "address": 7,
                            "address_base": 0,
                            "data_type": "Boolean",
                        }
                    ],
                },
            ]
        )
        bus = ValueBus()
        consumed = []
        bus.subscribe(consumed.append)
        manager = ModbusRtuManager(
            _Config(section),
            bus,
            client_factory=lambda: transport,
        )

        result = manager.read_all_once()
        values = {item.tag_id: item for item in bus.get_latest_list()}

        self.assertEqual({"success": 5, "failed": 0, "total": 5}, result)
        self.assertEqual(321, values["tag-batch-count"].value)
        self.assertAlmostEqual(12.5, values["tag-temperature"].value)
        self.assertEqual(0x12345678, values["tag-total"].value)
        self.assertIs(True, values["tag-running"].value)
        self.assertIs(False, values["tag-alarm"].value)
        self.assertEqual(
            [
                (1, "holding_register", 10, 5),
                (2, "coil", 3, 1),
                (2, "discrete_input", 7, 1),
            ],
            transport.calls,
        )
        mixer_values = [
            values["tag-batch-count"],
            values["tag-temperature"],
            values["tag-total"],
        ]
        self.assertEqual(1, len({item.source_timestamp for item in mixer_values}))
        self.assertTrue(all(item.server_timestamp for item in mixer_values))
        self.assertEqual(5, len(consumed))
        for item in values.values():
            self.assertEqual("MODBUS_RTU", item.protocol)
            self.assertTrue(item.point_key.startswith("MODBUS_RTU::"))
            self.assertEqual("conn-rs485-line-1", item.connection_id)
            self.assertEqual("Good", item.quality)
            self.assertEqual(0, item.raw_config["address_base"])
            self.assertIn("raw_values", item.raw_config)
        self.assertEqual("device-mixer", values["tag-batch-count"].device_id)
        self.assertEqual("device-pump", values["tag-running"].device_id)

    def test_disconnected_station_is_bad_and_does_not_block_next_station(self):
        transport = ControllableRtuTransport()
        transport.failed_stations.add(1)
        transport.set_registers(2, "input_register", 5, [777])
        section = _section(
            devices=[
                {
                    "name": "Offline",
                    "station_id": 1,
                    "points": [
                        {
                            "name": "Lost",
                            "tag_id": "tag-lost",
                            "type": "holding_register",
                            "address": 0,
                            "data_type": "UInt16",
                        }
                    ],
                },
                {
                    "name": "Online",
                    "station_id": 2,
                    "points": [
                        {
                            "name": "Pressure",
                            "tag_id": "tag-pressure",
                            "type": "input_register",
                            "address": 5,
                            "data_type": "UInt16",
                        }
                    ],
                },
            ]
        )
        bus = ValueBus()
        manager = ModbusRtuManager(
            _Config(section),
            bus,
            client_factory=lambda: transport,
        )

        result = manager.read_all_once()
        values = {item.tag_id: item for item in bus.get_latest_list()}

        self.assertEqual({"success": 1, "failed": 1, "total": 2}, result)
        self.assertEqual("Bad", values["tag-lost"].quality)
        self.assertIn("disconnected", values["tag-lost"].status_text)
        self.assertIn("error", values["tag-lost"].raw_config)
        self.assertEqual(777, values["tag-pressure"].value)
        self.assertEqual("Good", values["tag-pressure"].quality)
        self.assertGreaterEqual(transport.close_count, 1)

    def test_invalid_only_configuration_is_reported_without_transport_io(self):
        section = _section(
            devices=[
                {
                    "name": "Invalid",
                    "station_id": 1,
                    "points": [
                        {
                            "name": "BrokenAddress",
                            "tag_id": "tag-broken-address",
                            "type": "holding_register",
                            "address": "not-an-address",
                            "data_type": "UInt16",
                        }
                    ],
                }
            ]
        )
        bus = ValueBus()
        manager = ModbusRtuManager(
            _Config(section),
            bus,
            client_factory=lambda: self.fail("無效設定不應建立 Transport"),
        )

        result = manager.read_all_once()
        value = bus.get_latest_list()[0]

        self.assertEqual({"success": 0, "failed": 1, "total": 1}, result)
        self.assertEqual("tag-broken-address", value.tag_id)
        self.assertEqual("Bad", value.quality)
        self.assertIn("設定錯誤", value.status_text)
        self.assertIn("error", value.raw_config)

    def test_reload_during_decode_cannot_publish_retired_point(self):
        decode_entered = threading.Event()
        allow_decode = threading.Event()

        class PausingManager(ModbusRtuManager):
            def _decode_point(self, raw_values, point):
                decode_entered.set()
                if not allow_decode.wait(timeout=2.0):
                    raise TimeoutError("測試未允許 decoder 繼續")
                return ModbusRtuManager._decode_point(raw_values, point)

        transport = ControllableRtuTransport()
        transport.set_registers(1, "holding_register", 0, [123])
        old_section = _section(
            devices=[
                {
                    "name": "OldDevice",
                    "station_id": 1,
                    "points": [
                        {
                            "name": "OldPoint",
                            "type": "holding_register",
                            "address": 0,
                            "data_type": "UInt16",
                        }
                    ],
                }
            ]
        )
        config = _Config(old_section)
        bus = ValueBus()
        manager = PausingManager(
            config,
            bus,
            client_factory=lambda: transport,
        )
        result_holder = []
        reader = threading.Thread(
            target=lambda: result_holder.append(manager.read_all_once()),
            daemon=True,
        )
        reader.start()
        self.assertTrue(decode_entered.wait(timeout=2.0))

        try:
            new_section = _section(
                devices=[
                    {
                        "name": "NewDevice",
                        "station_id": 2,
                        "points": [
                            {
                                "name": "NewPoint",
                                "type": "holding_register",
                                "address": 10,
                                "data_type": "UInt16",
                            }
                        ],
                    }
                ]
            )
            new_section["port"] = "virtual://rs485-line-2"
            config.section = new_section
            manager.reload_config()
        finally:
            allow_decode.set()
            reader.join(timeout=2.0)

        self.assertFalse(reader.is_alive())
        self.assertEqual({"success": 0, "failed": 0, "total": 0}, result_holder[0])
        self.assertEqual([], bus.get_latest_list())
        self.assertTrue(all("NewDevice" in key for key in manager._points))

    def test_reader_started_during_reload_uses_no_intermediate_snapshot(self):
        reload_started = threading.Event()
        planning_started = threading.Event()
        allow_planning = threading.Event()

        class CoordinatedManager(ModbusRtuManager):
            def __init__(self, *args, **kwargs):
                self.signal_reload = False
                self.pause_planning = False
                super().__init__(*args, **kwargs)

            def is_running(self):
                if self.signal_reload:
                    reload_started.set()
                return ModbusRtuManager.is_running(self)

            def _point_record(self, point):
                if self.pause_planning:
                    planning_started.set()
                    if not allow_planning.wait(timeout=2.0):
                        raise TimeoutError("測試未允許 point planning 繼續")
                return ModbusRtuManager._point_record(point)

        transport = ControllableRtuTransport()
        transport.set_registers(1, "holding_register", 0, [123])
        config = _Config(
            _section(
                devices=[
                    {
                        "name": "OldDevice",
                        "station_id": 1,
                        "points": [
                            {
                                "name": "OldPoint",
                                "type": "holding_register",
                                "address": 0,
                                "data_type": "UInt16",
                            }
                        ],
                    }
                ]
            )
        )
        bus = ValueBus()
        manager = CoordinatedManager(
            config,
            bus,
            client_factory=lambda: transport,
        )
        config.section = _section(
            devices=[
                {
                    "name": "NewDevice",
                    "station_id": 2,
                    "points": [
                        {
                            "name": "NewPoint",
                            "type": "holding_register",
                            "address": 10,
                            "data_type": "UInt16",
                        }
                    ],
                }
            ]
        )
        reload_result = []
        read_result = []
        manager.signal_reload = True
        manager.pause_planning = True
        manager._io_lock.acquire()
        reloader = threading.Thread(
            target=lambda: reload_result.append(manager.reload_config()),
            daemon=True,
        )
        reader = threading.Thread(
            target=lambda: read_result.append(manager.read_all_once()),
            daemon=True,
        )
        reader_started = False
        try:
            reloader.start()
            self.assertTrue(reload_started.wait(timeout=2.0))
            reader.start()
            reader_started = True
            self.assertTrue(planning_started.wait(timeout=2.0))
        finally:
            manager._io_lock.release()
            allow_planning.set()
            reloader.join(timeout=2.0)
            if reader_started:
                reader.join(timeout=2.0)

        self.assertFalse(reloader.is_alive())
        self.assertFalse(reader.is_alive())
        self.assertEqual(1, reload_result[0]["point_count"])
        self.assertEqual({"success": 0, "failed": 0, "total": 0}, read_result[0])
        self.assertEqual([], bus.get_latest_list())

    def test_invalid_reader_during_reload_emits_no_stale_callback(self):
        reload_started = threading.Event()
        allow_reload = threading.Event()

        class PausingReloadManager(ModbusRtuManager):
            def __init__(self, *args, **kwargs):
                self.pause_reload = False
                super().__init__(*args, **kwargs)

            def is_running(self):
                if self.pause_reload:
                    reload_started.set()
                    if not allow_reload.wait(timeout=2.0):
                        raise TimeoutError("測試未允許 reload 繼續")
                return ModbusRtuManager.is_running(self)

        old_section = _section(
            devices=[
                {
                    "name": "OldBroken",
                    "station_id": 1,
                    "points": [
                        {
                            "name": "OldInvalidPoint",
                            "type": "holding_register",
                            "address": "invalid",
                            "data_type": "UInt16",
                        }
                    ],
                }
            ]
        )
        config = _Config(old_section)
        bus = ValueBus()
        callbacks = []
        bus.subscribe(callbacks.append)
        manager = PausingReloadManager(
            config,
            bus,
            client_factory=lambda: self.fail("無效設定不應建立 Transport"),
        )
        config.section = _section(devices=[])
        manager.pause_reload = True
        reload_result = []
        reloader = threading.Thread(
            target=lambda: reload_result.append(manager.reload_config()),
            daemon=True,
        )
        reloader.start()
        try:
            self.assertTrue(reload_started.wait(timeout=2.0))
            read_result = manager.read_all_once()
        finally:
            allow_reload.set()
            reloader.join(timeout=2.0)

        self.assertFalse(reloader.is_alive())
        self.assertEqual({"success": 0, "failed": 0, "total": 0}, read_result)
        self.assertEqual(0, reload_result[0]["point_count"])
        self.assertEqual([], callbacks)
        self.assertEqual([], bus.get_latest_list())

    def test_failed_reload_can_recover_and_publish_corrected_configuration(self):
        transport = ControllableRtuTransport()
        transport.set_registers(1, "holding_register", 0, [10])
        config = _Config(
            _section(
                devices=[
                    {
                        "name": "Original",
                        "station_id": 1,
                        "points": [
                            {
                                "name": "Value",
                                "type": "holding_register",
                                "address": 0,
                                "data_type": "UInt16",
                            }
                        ],
                    }
                ]
            )
        )
        bus = ValueBus()
        manager = ModbusRtuManager(
            config,
            bus,
            client_factory=lambda: transport,
        )
        self.assertEqual(1, manager.read_all_once()["success"])
        original_keys = set(manager._points)

        duplicate_point = {
            "name": "Duplicate",
            "type": "holding_register",
            "address": 1,
            "data_type": "UInt16",
        }
        config.section = _section(
            devices=[
                {
                    "name": "Broken",
                    "station_id": 1,
                    "points": [duplicate_point, dict(duplicate_point)],
                }
            ]
        )
        with self.assertRaisesRegex(ValueError, "point_key重複"):
            manager.reload_config()

        self.assertEqual(1, manager._config_generation % 2)
        self.assertEqual(original_keys, set(manager._points))

        transport.set_registers(1, "holding_register", 5, [999])
        config.section = _section(
            devices=[
                {
                    "name": "Recovered",
                    "station_id": 1,
                    "points": [
                        {
                            "name": "RecoveredValue",
                            "tag_id": "tag-recovered",
                            "type": "holding_register",
                            "address": 5,
                            "data_type": "UInt16",
                        }
                    ],
                }
            ]
        )
        manager.reload_config()
        result = manager.read_all_once()
        values = {item.tag_id: item for item in bus.get_latest_list()}

        self.assertEqual(0, manager._config_generation % 2)
        self.assertEqual({"success": 1, "failed": 0, "total": 1}, result)
        self.assertEqual(999, values["tag-recovered"].value)

    def test_polling_stop_closes_virtual_serial_transport(self):
        transport = ControllableRtuTransport()
        transport.set_registers(1, "holding_register", 0, [1])
        section = _section(
            enable=True,
            devices=[
                {
                    "name": "Closable",
                    "station_id": 1,
                    "points": [
                        {
                            "name": "Value",
                            "type": "holding_register",
                            "address": 0,
                            "data_type": "UInt16",
                        }
                    ],
                }
            ],
        )
        manager = ModbusRtuManager(
            _Config(section),
            ValueBus(),
            client_factory=lambda: transport,
        )
        self.addCleanup(manager.stop_polling)

        self.assertEqual("Modbus輪詢已啟動", manager.start_polling())
        self.assertTrue(transport.read_event.wait(timeout=2.0))
        self.assertEqual("Modbus輪詢已停止", manager.stop_polling())

        self.assertFalse(manager.is_running())
        self.assertGreaterEqual(transport.close_count, 1)


if __name__ == "__main__":
    unittest.main()
