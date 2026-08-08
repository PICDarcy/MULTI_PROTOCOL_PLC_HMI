"""第一版 Gateway 的唯讀 OPC UA 輸出 Server。"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from asyncua import Server, ua
from asyncua.crypto.permission_rules import User, UserRole
from asyncua.server.binary_server_asyncio import BinaryServer, OPCUAProtocol
from asyncua.server.uaprocessor import UaProcessor

from .gateway_security import GatewayWriteRejected, ReadonlyGatewayPolicy


_OPCUA_CLIENT_PEER: ContextVar[str] = ContextVar(
    "gateway_opcua_client_peer",
    default="opcua-unknown",
)


class _PeerAwareUaProcessor(UaProcessor):
    async def process(self, header, body):
        peer = self.name
        if isinstance(peer, tuple) and len(peer) >= 2:
            peer_text = f"{peer[0]}:{peer[1]}"
        else:
            peer_text = str(peer or "unknown")
        token = _OPCUA_CLIENT_PEER.set(f"opcua-{peer_text}")
        try:
            return await super().process(header, body)
        finally:
            _OPCUA_CLIENT_PEER.reset(token)


class _PeerAwareOpcuaProtocol(OPCUAProtocol):
    def connection_made(self, transport) -> None:
        self.peer_name = transport.get_extra_info("peername")
        self.transport = transport
        self.processor = _PeerAwareUaProcessor(
            self.iserver,
            self.transport,
            self.limits,
        )
        self.processor.set_policies(self.policies)
        self.iserver.asyncio_transports.append(transport)
        self.clients.append(self)
        self._task = asyncio.create_task(self._process_received_message_loop())


class _PeerAwareBinaryServer(BinaryServer):
    def _make_protocol(self):
        return _PeerAwareOpcuaProtocol(
            iserver=self.iserver,
            policies=self._policies,
            clients=self.clients,
            closing_tasks=self.closing_tasks,
            limits=self.limits,
        )


class _PeerAwareServer(Server):
    async def start(self) -> None:
        await self._setup_server_nodes()
        await self.iserver.start()
        try:
            ip_address, port = self._get_bind_socket_info()
            self.bserver = _PeerAwareBinaryServer(
                self.iserver,
                ip_address,
                port,
                self.limits,
            )
            self.bserver.set_policies(self._policies)
            await self.bserver.start()
        except Exception:
            await self.iserver.stop()
            raise


class GatewayOpcuaServer:
    """建立穩定且可瀏覽的唯讀 Variable，並稽核拒絕外部寫入。"""

    NAMESPACE_URI = "urn:picdarcy:multi-protocol-plc-hmi:gateway"

    def __init__(self, endpoint: str, log_callback=None) -> None:
        self.endpoint = str(endpoint)
        self._policy = ReadonlyGatewayPolicy(log_callback)
        self._server = _PeerAwareServer()
        self._namespace_index: int | None = None
        self._started = False
        self._original_write = None
        self._gateway_root: Any | None = None
        self._device_nodes: dict[str, Any] = {}
        self._nodes: dict[str, Any] = {}

    @property
    def port(self) -> int:
        """回傳實際監聽 port，支援 endpoint 指定 port 0。"""
        binary_server = getattr(self._server, "bserver", None)
        bound_port = getattr(binary_server, "port", None)
        if bound_port is not None:
            return int(bound_port)
        return int(urlsplit(self.endpoint).port or 0)

    @property
    def namespace_index(self) -> int:
        if self._namespace_index is None:
            raise RuntimeError("OPC UA Gateway Server 尚未啟動")
        return self._namespace_index

    async def start(self) -> None:
        if self._started:
            return
        await self._server.init()
        self._server.set_endpoint(self.endpoint)
        self._server.set_server_name("MULTI_PROTOCOL_PLC_HMI Readonly Gateway")
        self._namespace_index = await self._server.register_namespace(
            self.NAMESPACE_URI
        )
        await self._server.start()
        self._install_readonly_write_service()
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._original_write is not None:
            self._server.iserver.attribute_service.write = self._original_write
            self._original_write = None
        await self._server.stop()
        self._gateway_root = None
        self._device_nodes.clear()
        self._nodes.clear()

    async def _ensure_gateway_root(self):
        if self._gateway_root is not None:
            return self._gateway_root
        namespace = self.namespace_index
        self._gateway_root = await self._server.nodes.objects.add_object(
            ua.NodeId("gateway", namespace),
            ua.QualifiedName("Gateway", namespace),
        )
        return self._gateway_root

    async def _ensure_device_object(self, device_id: str, device_name: str):
        key = str(device_id).strip()
        if not key:
            return await self._ensure_gateway_root()
        existing = self._device_nodes.get(key)
        if existing is not None:
            return existing
        root = await self._ensure_gateway_root()
        namespace = self.namespace_index
        node = await root.add_object(
            ua.NodeId(f"device/{key}", namespace),
            ua.QualifiedName(str(device_name or key), namespace),
        )
        self._device_nodes[key] = node
        return node

    async def add_readonly_variable(
        self,
        *,
        tag_id: str,
        display_name: str,
        value: Any,
        variant_type: ua.VariantType,
        device_id: str = "",
        device_name: str = "",
    ) -> ua.NodeId:
        if not self._started or self._namespace_index is None:
            raise RuntimeError("OPC UA Gateway Server 尚未啟動")
        key = str(tag_id)
        existing = self._nodes.get(key)
        if existing is not None:
            return existing.nodeid
        parent = await self._ensure_device_object(device_id, device_name)
        node_id = ua.NodeId(key, self._namespace_index)
        node = await parent.add_variable(
            node_id,
            ua.QualifiedName(str(display_name), self._namespace_index),
            value,
            variant_type,
        )
        # asyncua Variables are read-only by default. The installed Write
        # Service guard additionally rejects and audits every client write.
        self._nodes[key] = node
        return node.nodeid

    @staticmethod
    def _timestamp(value: Any, *, default: datetime | None = None) -> datetime | None:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        if isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                return default
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        return default

    @staticmethod
    def _status_code(quality: Any) -> ua.StatusCode:
        text = str(quality or "Unknown").strip().lower()
        if text.startswith("good"):
            return ua.StatusCode(ua.StatusCodes.Good)
        if text.startswith("uncertain"):
            return ua.StatusCode(ua.StatusCodes.Uncertain)
        if "nocommunication" in text or "no_communication" in text:
            return ua.StatusCode(ua.StatusCodes.BadNoCommunication)
        if text.startswith("bad"):
            return ua.StatusCode(ua.StatusCodes.Bad)
        return ua.StatusCode(ua.StatusCodes.BadUnexpectedError)

    async def publish_value(
        self,
        *,
        tag_id: str,
        value: Any,
        variant_type: ua.VariantType,
        quality: str = "Good",
        source_timestamp=None,
        server_timestamp=None,
    ) -> None:
        """更新唯讀節點的完整 Canonical DataValue。"""
        if not self._started or self._original_write is None:
            raise RuntimeError("OPC UA Gateway Server 尚未啟動")
        node = self._nodes.get(str(tag_id))
        if node is None:
            raise KeyError(f"OPC UA輸出節點不存在：{tag_id}")
        received_at = datetime.now(timezone.utc)
        data_value = ua.DataValue(
            Value=ua.Variant(value, variant_type),
            StatusCode_=self._status_code(quality),
            SourceTimestamp=self._timestamp(source_timestamp, default=received_at),
            ServerTimestamp=self._timestamp(server_timestamp, default=received_at),
        )
        await self._write_internal_data_value(node, data_value)

    async def _write_internal_data_value(
        self,
        node,
        data_value: ua.DataValue,
    ) -> None:
        """更新 Server 自有節點，同時保留 Value、Quality 與雙時間戳。

        asyncua 的一般 AttributeService 會強制覆寫 ServerTimestamp，
        並在 Bad StatusCode 時清除 Value。Gateway 的正式契約必須保留
        Canonical 最後值與 Gateway 產生的時間，因此這個深層接縫只供
        Server 自己的輸出 Adapter 使用；所有網路 Write Service 仍走
        `_install_readonly_write_service` 並被拒絕及稽核。
        """
        address_space = self._server.iserver.aspace
        node_data = address_space._nodes.get(node.nodeid)
        if node_data is None:
            raise KeyError(f"OPC UA輸出節點不存在：{node.nodeid}")
        attribute = node_data.attributes.get(ua.AttributeIds.Value)
        if attribute is None:
            raise KeyError(f"OPC UA輸出節點沒有Value屬性：{node.nodeid}")
        if attribute.value_setter is not None:
            attribute.value_setter(node_data, ua.AttributeIds.Value, data_value)
        else:
            attribute.value = data_value
            attribute.value_callback = None
        for handle, callback in tuple(attribute.datachange_callbacks.items()):
            await callback(handle, data_value)

    def _install_readonly_write_service(self) -> None:
        service = self._server.iserver.attribute_service
        self._original_write = service.write

        async def readonly_write(
            params: ua.WriteParameters,
            user: User = User(role=UserRole.Admin),
        ) -> list[ua.StatusCode]:
            del user
            results: list[ua.StatusCode] = []
            client_peer = _OPCUA_CLIENT_PEER.get()
            for request in params.NodesToWrite:
                node_id = request.NodeId.to_string()
                try:
                    self._policy.reject_write(
                        protocol="OPCUA",
                        client=client_peer,
                        target=node_id,
                        address=node_id,
                        request_type="node_write",
                        requested_value=request.Value,
                    )
                except GatewayWriteRejected:
                    results.append(
                        ua.StatusCode(ua.StatusCodes.BadUserAccessDenied)
                    )
            return results

        service.write = readonly_write
