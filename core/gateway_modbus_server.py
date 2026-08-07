"""第一版 Gateway 的唯讀 Modbus TCP 輸出骨架。"""

from __future__ import annotations

import socketserver
import struct
import threading
from dataclasses import dataclass
from typing import Any

from .gateway_security import GatewayWriteRejected, ReadonlyGatewayPolicy


def _receive_exact(sock, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("Modbus TCP Client 已中斷")
        data.extend(chunk)
    return bytes(data)


class _ThreadingModbusServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@dataclass(frozen=True, slots=True)
class _MappedValue:
    value: Any
    target: str


class _ModbusRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        gateway: GatewayModbusTcpServer = self.server.gateway  # type: ignore[attr-defined]
        while True:
            try:
                header = _receive_exact(self.request, 7)
                transaction_id, protocol_id, length, unit_id = struct.unpack(
                    ">HHHB", header
                )
                if protocol_id != 0 or length < 2:
                    return
                pdu = _receive_exact(self.request, length - 1)
                response_pdu = gateway.handle_pdu(
                    pdu,
                    client=f"{self.client_address[0]}:{self.client_address[1]}",
                )
                response_header = struct.pack(
                    ">HHHB",
                    transaction_id,
                    0,
                    len(response_pdu) + 1,
                    unit_id,
                )
                self.request.sendall(response_header + response_pdu)
            except (ConnectionError, OSError, struct.error):
                return


class GatewayModbusTcpServer:
    """支援標準讀取並拒絕所有 Modbus 寫入 Function Code。"""

    WRITE_FUNCTIONS = {
        5: "function_code_5",
        6: "function_code_6",
        15: "function_code_15",
        16: "function_code_16",
        21: "function_code_21",
        22: "function_code_22",
        23: "function_code_23",
    }

    def __init__(self, host="127.0.0.1", port=0, log_callback=None) -> None:
        self.host = str(host)
        self._requested_port = int(port)
        self._policy = ReadonlyGatewayPolicy(log_callback)
        self._lock = threading.RLock()
        self._coils: dict[int, _MappedValue] = {}
        self._holding_registers: dict[int, _MappedValue] = {}
        self._file_records: dict[tuple[int, int], _MappedValue] = {}
        self._server: _ThreadingModbusServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            return self._requested_port
        return int(self._server.server_address[1])

    def set_coils(
        self,
        address: int,
        values: list[bool],
        *,
        target: str = "gateway-output",
    ) -> None:
        self._store_mapped_values(
            self._coils,
            address,
            [bool(value) for value in values],
            target,
        )

    def set_holding_registers(
        self,
        address: int,
        values: list[int],
        *,
        target: str = "gateway-output",
    ) -> None:
        numbers = [int(value) for value in values]
        if any(not 0 <= number <= 0xFFFF for number in numbers):
            raise ValueError("Modbus Register 必須介於 0 到 65535")
        self._store_mapped_values(
            self._holding_registers,
            address,
            numbers,
            target,
        )

    def _store_mapped_values(
        self,
        store: dict[int, _MappedValue],
        address: int,
        values: list[Any],
        target: str,
    ) -> None:
        with self._lock:
            for offset, value in enumerate(values):
                store[int(address) + offset] = _MappedValue(value, str(target))

    def set_file_record(
        self,
        file_number: int,
        record_number: int,
        registers: list[int],
        *,
        target: str = "gateway-output",
    ) -> None:
        payload = b"".join(struct.pack(">H", int(value)) for value in registers)
        with self._lock:
            self._file_records[(int(file_number), int(record_number))] = (
                _MappedValue(payload, str(target))
            )

    def start(self) -> None:
        if self._server is not None:
            return
        server = _ThreadingModbusServer(
            (self.host, self._requested_port), _ModbusRequestHandler
        )
        server.gateway = self  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name="GatewayModbusTcpServer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        server, self._server = self._server, None
        thread, self._thread = self._thread, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2)

    def handle_pdu(self, pdu: bytes, *, client: str) -> bytes:
        if not pdu:
            return bytes((0x80, 3))
        function_code = pdu[0]
        if function_code in self.WRITE_FUNCTIONS:
            target, address_text = self._write_context(function_code, pdu)
            try:
                self._policy.reject_write(
                    protocol="MODBUS_TCP",
                    client=client,
                    target=target,
                    address=address_text,
                    request_type=self.WRITE_FUNCTIONS[function_code],
                    requested_value=pdu[3:],
                )
            except GatewayWriteRejected:
                return bytes((function_code | 0x80, 1))
        if function_code == 1:
            return self._read_coils(pdu)
        if function_code == 3:
            return self._read_holding_registers(pdu)
        if function_code == 20:
            return self._read_file_records(pdu)
        return bytes((function_code | 0x80, 1))

    def _write_context(self, function_code: int, pdu: bytes) -> tuple[str, str]:
        if function_code == 21:
            if len(pdu) >= 7:
                file_number = struct.unpack(">H", pdu[3:5])[0]
                record_number = struct.unpack(">H", pdu[5:7])[0]
                location = f"file:{file_number}/record:{record_number}"
            else:
                location = "file:unknown/record:unknown"
            mapped = self._file_records.get((file_number, record_number))
            target = (
                mapped.target
                if mapped is not None
                else f"gateway-output/{location}"
            )
            return target, location

        address_offset = 5 if function_code == 23 else 1
        address = (
            struct.unpack(">H", pdu[address_offset : address_offset + 2])[0]
            if len(pdu) >= address_offset + 2
            else -1
        )
        if function_code in {5, 15}:
            area = "coil"
            mapped = self._coils.get(address)
        else:
            area = "holding_register"
            mapped = self._holding_registers.get(address)
        target = mapped.target if mapped is not None else "gateway-output/unmapped"
        return target, f"{area}:{address}"

    def _read_coils(self, pdu: bytes) -> bytes:
        if len(pdu) != 5:
            return bytes((0x81, 3))
        address, count = struct.unpack(">HH", pdu[1:])
        if not 1 <= count <= 2000:
            return bytes((0x81, 3))
        with self._lock:
            values = [
                bool(self._coils.get(address + offset, _MappedValue(False, "")).value)
                for offset in range(count)
            ]
        packed = bytearray((count + 7) // 8)
        for index, value in enumerate(values):
            if value:
                packed[index // 8] |= 1 << (index % 8)
        return bytes((1, len(packed))) + bytes(packed)

    def _read_holding_registers(self, pdu: bytes) -> bytes:
        if len(pdu) != 5:
            return bytes((0x83, 3))
        address, count = struct.unpack(">HH", pdu[1:])
        if not 1 <= count <= 125:
            return bytes((0x83, 3))
        with self._lock:
            values = [
                int(
                    self._holding_registers.get(
                        address + offset, _MappedValue(0, "")
                    ).value
                )
                for offset in range(count)
            ]
        payload = b"".join(struct.pack(">H", value) for value in values)
        return bytes((3, len(payload))) + payload

    def _read_file_records(self, pdu: bytes) -> bytes:
        if len(pdu) < 9 or pdu[1] != len(pdu) - 2:
            return bytes((0x94, 3))
        offset = 2
        responses = bytearray()
        while offset + 7 <= len(pdu):
            reference_type = pdu[offset]
            file_number, record_number, record_length = struct.unpack(
                ">HHH", pdu[offset + 1 : offset + 7]
            )
            if reference_type != 6 or record_length < 1:
                return bytes((0x94, 3))
            with self._lock:
                mapped = self._file_records.get((file_number, record_number))
                data = bytes(mapped.value) if mapped is not None else b""
            data = data[: record_length * 2].ljust(record_length * 2, b"\x00")
            responses.extend((len(data) + 1, 6))
            responses.extend(data)
            offset += 7
        return bytes((20, len(responses))) + bytes(responses)
