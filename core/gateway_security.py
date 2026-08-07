"""第一版唯讀 Gateway 的能力宣告與寫入拒絕稽核。"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Callable


LOGGER = logging.getLogger(__name__)


class GatewayWriteRejected(PermissionError):
    """第一版 Gateway 拒絕任何設備或輸出端寫入。"""


@dataclass(frozen=True, slots=True)
class GatewayCapabilities:
    """可供 UI 與協定 Adapter 共用的產品能力邊界。"""

    device_writes: bool = False
    modbus_server_writes: bool = False
    opcua_server_writes: bool = False


@dataclass(frozen=True, slots=True)
class RejectedWriteEvent:
    """不包含要求值的結構化安全事件。"""

    protocol: str
    client: str
    target: str
    address: str
    request_type: str
    result: str = "rejected_read_only"


LogCallback = Callable[..., Any]


class ReadonlyGatewayPolicy:
    """集中拒絕並稽核第一版 Gateway 的所有寫入入口。"""

    capabilities = GatewayCapabilities()

    def __init__(self, log_callback: LogCallback | None = None) -> None:
        self._log_callback = log_callback

    def reject_write(
        self,
        *,
        protocol: str,
        client: str,
        target: str,
        address: str,
        request_type: str,
        requested_value: Any = None,
    ) -> None:
        """記錄必要脈絡後拒絕；requested_value 刻意不寫入事件。"""
        del requested_value
        event = RejectedWriteEvent(
            protocol=str(protocol or "UNKNOWN").upper(),
            client=str(client or "unknown"),
            target=str(target or "unknown"),
            address=str(address or "unknown"),
            request_type=str(request_type or "write"),
        )
        message = "SECURITY_WRITE_REJECTED " + json.dumps(
            asdict(event), ensure_ascii=False, sort_keys=True
        )
        self._emit(message)
        raise GatewayWriteRejected(
            f"第一版 Gateway 為唯讀，已拒絕 {event.protocol} 寫入："
            f"{event.target} ({event.address})"
        )

    def _emit(self, message: str) -> None:
        if self._log_callback is None:
            LOGGER.warning(message)
            return
        try:
            self._log_callback(message, "WARNING")
        except TypeError:
            self._log_callback(message)
