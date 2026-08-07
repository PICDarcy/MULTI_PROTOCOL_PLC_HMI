"""第一版 Gateway 的唯讀 OPC UA 輸出骨架。"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any

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
    """建立唯讀 Variable，並在真實 Write Service path 稽核拒絕。"""

    def __init__(self, endpoint: str, log_callback=None) -> None:
        self.endpoint = str(endpoint)
        self._policy = ReadonlyGatewayPolicy(log_callback)
        self._server = _PeerAwareServer()
        self._namespace_index: int | None = None
        self._started = False
        self._original_write = None

    async def start(self) -> None:
        if self._started:
            return
        await self._server.init()
        self._server.set_endpoint(self.endpoint)
        self._server.set_server_name("MULTI_PROTOCOL_PLC_HMI Readonly Gateway")
        self._namespace_index = await self._server.register_namespace(
            "urn:picdarcy:multi-protocol-plc-hmi:gateway"
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

    async def add_readonly_variable(
        self,
        *,
        tag_id: str,
        display_name: str,
        value: Any,
        variant_type: ua.VariantType,
    ) -> ua.NodeId:
        if not self._started or self._namespace_index is None:
            raise RuntimeError("OPC UA Gateway Server 尚未啟動")
        node_id = ua.NodeId(str(tag_id), self._namespace_index)
        node = await self._server.nodes.objects.add_variable(
            node_id,
            ua.QualifiedName(str(display_name), self._namespace_index),
            value,
            variant_type,
        )
        return node.nodeid

    def _install_readonly_write_service(self) -> None:
        service = self._server.iserver.attribute_service
        self._original_write = service.write

        async def readonly_write(
            params: ua.WriteParameters,
            user: User = User(role=UserRole.Admin),
        ) -> list[ua.StatusCode]:
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
