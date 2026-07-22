"""OPC UA多Server管理模組。

本模組使用asyncua，並在獨立asyncio事件迴圈執行緒中處理網路I/O，
避免OPC UA連線、讀寫、訂閱及瀏覽操作阻塞Tkinter主執行緒。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import json
import math
import numbers
import threading
from collections import deque
from collections.abc import Coroutine, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from asyncua import Client, ua

from .data_model import PointValue, make_opcua_point_key


PROTOCOL_OPCUA = "OPCUA"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_SUBSCRIPTION_INTERVAL_MS = 500.0

WRITE_TYPES = {
    "BOOLEAN": ua.VariantType.Boolean,
    "BYTE": ua.VariantType.Byte,
    "SBYTE": ua.VariantType.SByte,
    "INT16": ua.VariantType.Int16,
    "INT32": ua.VariantType.Int32,
    "INT64": ua.VariantType.Int64,
    "UINT16": ua.VariantType.UInt16,
    "UINT32": ua.VariantType.UInt32,
    "UINT64": ua.VariantType.UInt64,
    "FLOAT": ua.VariantType.Float,
    "DOUBLE": ua.VariantType.Double,
    "STRING": ua.VariantType.String,
    "DATETIME": ua.VariantType.DateTime,
}

INTEGER_LIMITS = {
    ua.VariantType.SByte: (-(2**7), 2**7 - 1),
    ua.VariantType.Byte: (0, 2**8 - 1),
    ua.VariantType.Int16: (-(2**15), 2**15 - 1),
    ua.VariantType.Int32: (-(2**31), 2**31 - 1),
    ua.VariantType.Int64: (-(2**63), 2**63 - 1),
    ua.VariantType.UInt16: (0, 2**16 - 1),
    ua.VariantType.UInt32: (0, 2**32 - 1),
    ua.VariantType.UInt64: (0, 2**64 - 1),
}

SECRET_KEYS = {
    "password",
    "passwd",
    "pwd",
    "token",
    "api_key",
    "apikey",
    "secret",
    "client_secret",
}


class _ServerSubscriptionHandler:
    """單一OPC UA Server專用的Subscription Handler。"""

    def __init__(self, manager: "OpcuaMultiServerManager", server_name: str):
        self.manager = manager
        self.server_name = server_name

    async def datachange_notification(self, node, value, data):
        await self.manager._on_datachange(self.server_name, node, value, data)

    async def status_change_notification(self, status):
        self.manager._set_server_status(
            self.server_name,
            self.manager.is_connected(self.server_name),
            f"Subscription狀態：{getattr(status, 'Status', status)}",
        )

    async def event_notification(self, event):
        self.manager._log(
            f"Server「{self.server_name}」收到事件通知：{event}",
            "DEBUG",
        )


class OpcuaMultiServerManager:
    """在單一背景asyncio事件迴圈管理多台OPC UA Server。"""

    def __init__(self, config_manager, value_bus, log_callback=None):
        self.config_manager = config_manager
        self.value_bus = value_bus
        self.log_callback = log_callback

        self._state_lock = threading.RLock()
        self._loop_ready = threading.Event()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread = threading.Thread(
            target=self._loop_worker,
            name="OpcuaMultiServerLoop",
            daemon=True,
        )

        self._server_configs: dict[str, dict[str, Any]] = self._load_server_configs()
        self._clients: dict[str, Client] = {}
        self._subscriptions: dict[str, Any] = {}
        self._handlers: dict[str, _ServerSubscriptionHandler] = {}
        self._handles: dict[str, dict[str, Any]] = {}
        self._node_configs: dict[str, dict[str, dict[str, Any]]] = {}
        self._connected: dict[str, bool] = {}
        self._operation_locks: dict[str, asyncio.Lock] = {}

        self.latest_values: dict[str, PointValue] = {}
        self.latest_status: dict[str, dict[str, Any]] = {}
        self.latest_point_status: dict[str, dict[str, Any]] = {}

        for server_name in self._server_configs:
            self._connected[server_name] = False
            self._set_server_status(server_name, False, "尚未連線")

        self._loop_thread.start()
        if not self._loop_ready.wait(5.0):
            raise RuntimeError("OPC UA背景事件迴圈啟動逾時")

    # ------------------------------------------------------------------
    # 公開介面：網路操作回傳concurrent.futures.Future
    # ------------------------------------------------------------------
    def connect_server(self, server_name):
        return self._submit(self._connect_server(str(server_name)))

    def disconnect_server(self, server_name):
        return self._submit(self._disconnect_server(str(server_name)))

    def connect_all(self):
        return self._submit(self._connect_all())

    def disconnect_all(self):
        return self._submit(self._disconnect_all())

    def read_node(self, server_name, node_id):
        return self._submit(self._read_node(str(server_name), str(node_id)))

    def write_node(self, server_name, node_id, value_text, data_type="Auto"):
        return self._submit(
            self._write_node(
                str(server_name),
                str(node_id),
                value_text,
                str(data_type or "Auto"),
            )
        )

    def subscribe_node(self, server_name, node_config):
        return self._submit(self._subscribe_node(str(server_name), node_config))

    def unsubscribe_node(self, server_name, node_id):
        return self._submit(
            self._unsubscribe_node(str(server_name), str(node_id))
        )

    def subscribe_all(self):
        return self._submit(self._subscribe_all())

    def browse_node(self, server_name, node_id):
        return self._submit(self._browse_node(str(server_name), str(node_id)))

    def scan_all_nodes(
        self,
        server_name,
        start_node_id="i=85",
        max_depth=8,
        max_nodes=3000,
        only_variables=True,
        include_ns0=False,
    ):
        return self._submit(
            self._scan_all_nodes(
                str(server_name),
                str(start_node_id),
                int(max_depth),
                int(max_nodes),
                bool(only_variables),
                bool(include_ns0),
            )
        )

    def reload_config(self):
        return self._submit(self._reload_config())

    def get_server_names(self) -> list[str]:
        with self._state_lock:
            return list(self._server_configs)

    def get_latest_values(self) -> dict[str, PointValue]:
        with self._state_lock:
            return dict(self.latest_values)

    def get_latest_status(self) -> dict[str, dict[str, Any]]:
        with self._state_lock:
            return copy.deepcopy(self.latest_status)

    def get_latest_point_status(self) -> dict[str, dict[str, Any]]:
        with self._state_lock:
            return copy.deepcopy(self.latest_point_status)

    def is_connected(self, server_name: str) -> bool:
        with self._state_lock:
            return bool(self._connected.get(server_name, False))

    def shutdown(self):
        """斷開全部Server，停止事件迴圈並等待背景執行緒結束。"""
        with self._state_lock:
            if self._closed:
                completed: concurrent.futures.Future = concurrent.futures.Future()
                completed.set_result({})
                return completed

        disconnect_future = self.disconnect_all()
        completion: concurrent.futures.Future = concurrent.futures.Future()

        def stop_loop(done_future: concurrent.futures.Future) -> None:
            try:
                result = done_future.result()
                failure: BaseException | None = None
            except BaseException as exc:  # noqa: BLE001
                result = None
                failure = exc

            with self._state_lock:
                self._closed = True
                loop = self._loop
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except RuntimeError:
                    pass

            def wait_loop_thread() -> None:
                if self._loop_thread is not threading.current_thread():
                    self._loop_thread.join(timeout=5.0)
                if self._loop_thread.is_alive():
                    completion.set_exception(
                        RuntimeError("OPC UA背景事件迴圈未在5秒內停止")
                    )
                elif failure is not None:
                    completion.set_exception(failure)
                else:
                    completion.set_result(result)

            threading.Thread(
                target=wait_loop_thread,
                name="OpcuaShutdownWaiter",
                daemon=True,
            ).start()

        disconnect_future.add_done_callback(stop_loop)
        return completion

    # ------------------------------------------------------------------
    # 背景asyncio事件迴圈
    # ------------------------------------------------------------------
    def _loop_worker(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()

    def _submit(
        self,
        coroutine: Coroutine[Any, Any, Any],
    ) -> concurrent.futures.Future:
        with self._state_lock:
            if self._closed or self._loop is None or not self._loop.is_running():
                coroutine.close()
                future: concurrent.futures.Future = concurrent.futures.Future()
                future.set_exception(RuntimeError("OPC UA管理模組已停止"))
                return future
            loop = self._loop
        return asyncio.run_coroutine_threadsafe(coroutine, loop)

    def _server_lock(self, server_name: str) -> asyncio.Lock:
        lock = self._operation_locks.get(server_name)
        if lock is None:
            lock = asyncio.Lock()
            self._operation_locks[server_name] = lock
        return lock

    # ------------------------------------------------------------------
    # 連線與中斷
    # ------------------------------------------------------------------
    async def _connect_server(self, server_name: str) -> dict[str, Any]:
        config = self._require_server_config(server_name)
        async with self._server_lock(server_name):
            if self.is_connected(server_name) and server_name in self._clients:
                return {
                    "server_name": server_name,
                    "endpoint_url": self._safe_endpoint(
                        config.get("endpoint_url")
                    ),
                    "connected": True,
                    "status_text": "已連線",
                }

            endpoint_url = str(config.get("endpoint_url", "")).strip()
            if not endpoint_url:
                raise ValueError(
                    f"Server「{server_name}」未設定endpoint_url"
                )

            timeout = self._to_float(
                config.get(
                    "timeout_seconds",
                    config.get("timeout", DEFAULT_TIMEOUT_SECONDS),
                ),
                DEFAULT_TIMEOUT_SECONDS,
                minimum=0.1,
            )
            client = Client(endpoint_url, timeout=timeout)

            auth = (
                config.get("auth")
                if isinstance(config.get("auth"), Mapping)
                else {}
            )
            username = str(
                config.get("username", auth.get("username", "")) or ""
            )
            password = str(
                config.get("password", auth.get("password", "")) or ""
            )
            use_username = self._to_bool(
                config.get("use_username", bool(username)),
                bool(username),
            )
            if use_username:
                if not username:
                    raise ValueError(
                        f"Server「{server_name}」已啟用帳號登入但username為空白"
                    )
                client.set_user(username)
                client.set_password(password)

            self._set_server_status(server_name, False, "連線中")
            try:
                await client.connect()
            except Exception as exc:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                error_text = self._redact_error(config, exc)
                self._set_server_status(
                    server_name,
                    False,
                    f"連線失敗：{error_text}",
                    error_text,
                )
                self._log(
                    f"Server「{server_name}」連線失敗：{error_text}",
                    "ERROR",
                )
                raise

            with self._state_lock:
                self._clients[server_name] = client
                self._connected[server_name] = True
                self._handles.setdefault(server_name, {})
                self._node_configs.setdefault(server_name, {})
            self._set_server_status(server_name, True, "已連線")
            self._log(
                f"Server「{server_name}」已連線："
                f"{self._safe_endpoint(endpoint_url)}",
                "INFO",
            )
            return {
                "server_name": server_name,
                "endpoint_url": self._safe_endpoint(endpoint_url),
                "connected": True,
                "status_text": "已連線",
            }

    async def _disconnect_server(self, server_name: str) -> dict[str, Any]:
        async with self._server_lock(server_name):
            subscription = self._subscriptions.pop(server_name, None)
            client = self._clients.pop(server_name, None)
            self._handlers.pop(server_name, None)
            self._handles.pop(server_name, None)
            self._node_configs.pop(server_name, None)

            errors: list[str] = []
            if subscription is not None:
                try:
                    await subscription.delete()
                except Exception as exc:
                    errors.append(f"刪除Subscription失敗：{exc}")
            if client is not None:
                try:
                    await client.disconnect()
                except Exception as exc:
                    errors.append(f"中斷Client失敗：{exc}")

            with self._state_lock:
                self._connected[server_name] = False
            status_text = (
                "已中斷" if not errors else "已中斷，但清理時發生錯誤"
            )
            self._set_server_status(
                server_name,
                False,
                status_text,
                "；".join(errors),
            )
            self._log(
                f"Server「{server_name}」{status_text}",
                "WARNING" if errors else "INFO",
            )
            return {
                "server_name": server_name,
                "connected": False,
                "status_text": status_text,
                "errors": errors,
            }

    async def _connect_all(self) -> dict[str, Any]:
        names = self.get_server_names()
        if not names:
            return {}
        results = await asyncio.gather(
            *(self._connect_server(name) for name in names),
            return_exceptions=True,
        )
        output: dict[str, Any] = {}
        for name, result in zip(names, results):
            output[name] = (
                {
                    "server_name": name,
                    "connected": False,
                    "error": str(result),
                }
                if isinstance(result, Exception)
                else result
            )
        return output

    async def _disconnect_all(self) -> dict[str, Any]:
        with self._state_lock:
            names = list(
                dict.fromkeys(
                    [
                        *self._server_configs,
                        *self._clients,
                        *self._subscriptions,
                    ]
                )
            )
        if not names:
            return {}
        results = await asyncio.gather(
            *(self._disconnect_server(name) for name in names),
            return_exceptions=True,
        )
        return {
            name: (
                {"server_name": name, "error": str(result)}
                if isinstance(result, Exception)
                else result
            )
            for name, result in zip(names, results)
        }

    # ------------------------------------------------------------------
    # Node讀寫
    # ------------------------------------------------------------------
    async def _read_node(
        self,
        server_name: str,
        node_id: str,
    ) -> dict[str, Any]:
        client = self._require_connected(server_name)
        node_id = self._canonical_node_id(node_id)
        config = self._configured_node_config(server_name, node_id)
        try:
            node = client.get_node(node_id)
            data_value = await node.read_data_value()
            point_value = await self._publish_data_value(
                server_name,
                node_id,
                data_value,
                config,
            )
            return self._point_to_dict(point_value)
        except Exception as exc:
            self._record_point_error(server_name, node_id, "讀取", exc)
            raise

    async def _write_node(
        self,
        server_name: str,
        node_id: str,
        value_text: Any,
        data_type: str,
    ) -> dict[str, Any]:
        client = self._require_connected(server_name)
        node_id = self._canonical_node_id(node_id)
        config = self._configured_node_config(server_name, node_id)
        node = client.get_node(node_id)
        try:
            variant_type = await self._resolve_variant_type(node, data_type)
            converted_value = self._convert_write_value(
                value_text,
                variant_type,
            )
            await node.write_value(ua.Variant(converted_value, variant_type))
            data_value = await node.read_data_value()
            point_value = await self._publish_data_value(
                server_name,
                node_id,
                data_value,
                config,
            )
            self._set_point_status(
                server_name,
                node_id,
                "寫入成功",
                "",
                self._now(),
            )
            return self._point_to_dict(point_value)
        except Exception as exc:
            self._record_point_error(server_name, node_id, "寫入", exc)
            raise

    async def _resolve_variant_type(self, node, data_type: str):
        normalized = str(data_type or "Auto").strip().upper()
        if normalized == "AUTO":
            return await node.read_data_type_as_variant_type()
        variant_type = WRITE_TYPES.get(normalized)
        if variant_type is None:
            supported = (
                "Auto、Boolean、Byte、SByte、Int16、Int32、Int64、"
                "UInt16、UInt32、UInt64、Float、Double、String、DateTime"
            )
            raise ValueError(
                f"不支援的OPC UA寫入型別「{data_type}」，"
                f"支援型別：{supported}"
            )
        return variant_type

    def _convert_write_value(self, value_text: Any, variant_type):
        if variant_type == ua.VariantType.Boolean:
            if isinstance(value_text, bool):
                return value_text
            text = str(value_text).strip().lower()
            if text in {
                "1", "true", "on", "yes", "y", "是", "開", "啟用"
            }:
                return True
            if text in {
                "0", "false", "off", "no", "n", "否", "關", "停用"
            }:
                return False
            raise ValueError(f"無法將「{value_text}」轉換為Boolean")

        if variant_type in INTEGER_LIMITS:
            try:
                value = int(str(value_text).strip(), 0)
            except ValueError:
                value = int(str(value_text).strip())
            minimum, maximum = INTEGER_LIMITS[variant_type]
            if not minimum <= value <= maximum:
                raise ValueError(
                    f"整數{value}超出範圍{minimum}～{maximum}"
                )
            return value

        if variant_type in {
            ua.VariantType.Float,
            ua.VariantType.Double,
        }:
            value = float(value_text)
            if not math.isfinite(value):
                raise ValueError("Float或Double不可為NaN或Infinity")
            return value

        if variant_type == ua.VariantType.String:
            return str(value_text)

        if variant_type == ua.VariantType.DateTime:
            if isinstance(value_text, datetime):
                return value_text
            text = str(value_text).strip().replace("Z", "+00:00")
            if not text:
                raise ValueError("DateTime不可為空白")
            try:
                return datetime.fromisoformat(text)
            except ValueError as exc:
                raise ValueError(
                    "DateTime請使用ISO 8601格式，例如2026-07-22T15:30:00+08:00"
                ) from exc

        raise ValueError(
            f"Auto解析到目前未支援的寫入型別：{variant_type}"
        )

    # ------------------------------------------------------------------
    # Subscription管理
    # ------------------------------------------------------------------
    async def _subscribe_node(
        self,
        server_name: str,
        node_config: Any,
    ) -> dict[str, Any]:
        config = self._normalize_node_config(server_name, node_config)
        if not self._to_bool(
            config.get("enable", config.get("enabled", True)),
            True,
        ):
            raise RuntimeError(
                f"Server「{server_name}」Node「{config['node_id']}」未啟用"
            )

        node_id = config["node_id"]
        if not self.is_connected(server_name):
            await self._connect_server(server_name)

        async with self._server_lock(server_name):
            client = self._require_connected(server_name)
            subscription = self._subscriptions.get(server_name)
            if subscription is None:
                server_config = self._require_server_config(server_name)
                interval_ms = self._to_float(
                    server_config.get(
                        "subscription_interval_ms",
                        server_config.get(
                            "publishing_interval_ms",
                            DEFAULT_SUBSCRIPTION_INTERVAL_MS,
                        ),
                    ),
                    DEFAULT_SUBSCRIPTION_INTERVAL_MS,
                    minimum=1.0,
                )
                handler = _ServerSubscriptionHandler(self, server_name)
                subscription = await client.create_subscription(
                    interval_ms,
                    handler,
                )
                self._subscriptions[server_name] = subscription
                self._handlers[server_name] = handler

            old_handle = self._handles.setdefault(server_name, {}).get(node_id)
            if old_handle is not None:
                self._node_configs.setdefault(server_name, {})[
                    node_id
                ] = config
                return {
                    "server_name": server_name,
                    "node_id": node_id,
                    "subscribed": True,
                    "already_subscribed": True,
                }

            node = client.get_node(node_id)
            handle = await subscription.subscribe_data_change(node)
            self._handles.setdefault(server_name, {})[node_id] = handle
            self._node_configs.setdefault(server_name, {})[
                node_id
            ] = config
            self._set_point_status(
                server_name,
                node_id,
                "已訂閱",
                "",
                self._now(),
            )

        try:
            data_value = await node.read_data_value()
            await self._publish_data_value(
                server_name,
                node_id,
                data_value,
                config,
            )
        except Exception as exc:
            self._log(
                f"Server「{server_name}」Node「{node_id}」"
                f"初始讀取失敗：{exc}",
                "WARNING",
            )

        return {
            "server_name": server_name,
            "node_id": node_id,
            "subscribed": True,
            "already_subscribed": False,
        }

    async def _unsubscribe_node(
        self,
        server_name: str,
        node_id: str,
    ) -> dict[str, Any]:
        node_id = self._canonical_node_id(node_id)
        async with self._server_lock(server_name):
            subscription = self._subscriptions.get(server_name)
            handle = self._handles.get(server_name, {}).pop(node_id, None)
            self._node_configs.get(server_name, {}).pop(node_id, None)
            if handle is None:
                return {
                    "server_name": server_name,
                    "node_id": node_id,
                    "subscribed": False,
                    "already_unsubscribed": True,
                }
            if subscription is not None:
                await subscription.unsubscribe(handle)
            self._set_point_status(
                server_name,
                node_id,
                "已取消訂閱",
                "",
                self._now(),
            )
            return {
                "server_name": server_name,
                "node_id": node_id,
                "subscribed": False,
                "already_unsubscribed": False,
            }

    async def _subscribe_all(self) -> dict[str, Any]:
        await self._connect_all()
        output: dict[str, Any] = {}
        for server_name, server_config in list(
            self._server_configs.items()
        ):
            nodes = server_config.get("nodes", [])
            results: list[dict[str, Any]] = []
            for node_config in nodes:
                if isinstance(node_config, Mapping):
                    enabled = self._to_bool(
                        node_config.get(
                            "enable",
                            node_config.get("enabled", True),
                        ),
                        True,
                    )
                    subscribe = self._to_bool(
                        node_config.get("subscribe", True),
                        True,
                    )
                    if not enabled or not subscribe:
                        continue
                try:
                    results.append(
                        await self._subscribe_node(
                            server_name,
                            node_config,
                        )
                    )
                except Exception as exc:
                    node_id = (
                        str(node_config.get("node_id", ""))
                        if isinstance(node_config, Mapping)
                        else str(node_config)
                    )
                    results.append(
                        {
                            "server_name": server_name,
                            "node_id": node_id,
                            "subscribed": False,
                            "error": str(exc),
                        }
                    )
            output[server_name] = {
                "server_name": server_name,
                "nodes": results,
            }
        return output

    async def _on_datachange(self, server_name: str, node, value, data):
        try:
            node_id = self._canonical_node_id(
                getattr(node, "nodeid", node)
            )
            config = self._node_configs.get(server_name, {}).get(node_id)
            monitored_item = getattr(data, "monitored_item", None)
            data_value = getattr(monitored_item, "Value", None)
            if data_value is not None:
                await self._publish_data_value(
                    server_name,
                    node_id,
                    data_value,
                    config,
                )
            else:
                await self._publish_value(
                    server_name,
                    node_id,
                    value,
                    config=config,
                    status_text="Good",
                    timestamp=self._now(),
                )
        except Exception as exc:
            self._log(
                f"Server「{server_name}」資料變更處理失敗：{exc}",
                "ERROR",
            )

    # ------------------------------------------------------------------
    # Browse與遞迴掃描
    # ------------------------------------------------------------------
    async def _browse_node(
        self,
        server_name: str,
        node_id: str,
    ) -> list[dict[str, Any]]:
        client = self._require_connected(server_name)
        node_id = self._canonical_node_id(node_id)
        parent = client.get_node(node_id)
        parent_path = await self._read_path(parent, node_id)
        descriptions = await parent.get_children_descriptions()
        records: list[dict[str, Any]] = []
        for description in descriptions:
            records.append(
                await self._record_from_description(
                    client,
                    description,
                    parent_path,
                    1,
                )
            )
        return records

    async def _scan_all_nodes(
        self,
        server_name: str,
        start_node_id: str,
        max_depth: int,
        max_nodes: int,
        only_variables: bool,
        include_ns0: bool,
    ) -> list[dict[str, Any]]:
        client = self._require_connected(server_name)
        start_node_id = self._canonical_node_id(start_node_id)
        max_depth = max(0, int(max_depth))
        max_nodes = max(1, int(max_nodes))
        start_node = client.get_node(start_node_id)
        start_path = await self._read_path(start_node, start_node_id)

        queue = deque([(start_node_id, start_path, 0)])
        visited: set[str] = set()
        output: list[dict[str, Any]] = []
        examined = 0

        while queue and examined < max_nodes:
            current_id, current_path, depth = queue.popleft()
            if current_id in visited or depth > max_depth:
                continue
            visited.add(current_id)
            examined += 1

            current = client.get_node(current_id)
            try:
                descriptions = await current.get_children_descriptions()
            except Exception as exc:
                self._log(
                    f"掃描Node「{current_id}」失敗：{exc}",
                    "DEBUG",
                )
                continue

            for description in descriptions:
                if examined + len(queue) >= max_nodes:
                    break
                record = await self._record_from_description(
                    client,
                    description,
                    current_path,
                    depth + 1,
                )
                child_id = record["node_id"]
                child_depth = record["depth"]
                namespace_index = self._namespace_index(child_id)
                is_variable = record["node_class"] == "Variable"

                if (include_ns0 or namespace_index != 0) and (
                    not only_variables or is_variable
                ):
                    output.append(record)

                if child_depth < max_depth and child_id not in visited:
                    queue.append(
                        (child_id, record["path"], child_depth)
                    )

        return output

    async def _record_from_description(
        self,
        client,
        description,
        parent_path: str,
        depth: int,
    ) -> dict[str, Any]:
        node_id = self._canonical_node_id(
            getattr(description, "NodeId", "")
        )
        display_name = self._localized_text(
            getattr(description, "DisplayName", "")
        )
        browse_name = self._qualified_name_text(
            getattr(description, "BrowseName", "")
        )
        node_class = self._node_class_name(
            getattr(description, "NodeClass", "")
        )
        child = client.get_node(node_id)
        data_type = await self._read_data_type(child, node_class)
        label = display_name or browse_name or node_id
        path = (
            f"{parent_path.rstrip('/')}/{label}"
            if parent_path
            else label
        )
        return {
            "display_name": display_name,
            "browse_name": browse_name,
            "node_id": node_id,
            "node_class": node_class,
            "data_type": data_type,
            "path": path,
            "depth": depth,
        }

    async def _read_data_type(self, node, node_class: str) -> str:
        if node_class != "Variable":
            return ""
        try:
            variant_type = await node.read_data_type_as_variant_type()
            return getattr(variant_type, "name", str(variant_type))
        except Exception:
            return "Unknown"

    async def _read_path(self, node, fallback: str) -> str:
        try:
            path = await node.get_path(as_string=True)
            if isinstance(path, (list, tuple)):
                return "/".join(
                    str(item).strip("/")
                    for item in path
                    if str(item)
                )
            return str(path)
        except Exception:
            return fallback

    # ------------------------------------------------------------------
    # 設定重新載入
    # ------------------------------------------------------------------
    async def _reload_config(self) -> dict[str, Any]:
        await self._disconnect_all()
        new_configs = self._load_server_configs()
        with self._state_lock:
            self._server_configs = new_configs
            self._clients.clear()
            self._subscriptions.clear()
            self._handlers.clear()
            self._handles.clear()
            self._node_configs.clear()
            self._connected = {name: False for name in new_configs}
            self._operation_locks.clear()
            self.latest_status = {}
            for server_name in new_configs:
                self._set_server_status(
                    server_name,
                    False,
                    "設定已重新載入，尚未連線",
                )
        self._log(
            f"OPC UA設定已重新載入，共{len(new_configs)}個Server",
            "INFO",
        )
        return {
            "reloaded": True,
            "server_count": len(new_configs),
            "server_names": list(new_configs),
        }

    # ------------------------------------------------------------------
    # PointValue建立、發布與狀態保存
    # ------------------------------------------------------------------
    async def _publish_data_value(
        self,
        server_name: str,
        node_id: str,
        data_value,
        config: Mapping[str, Any] | None = None,
    ) -> PointValue:
        value = getattr(
            getattr(data_value, "Value", None),
            "Value",
            None,
        )
        status_code = getattr(data_value, "StatusCode", None)
        status_text = (
            str(status_code) if status_code is not None else "Unknown"
        )
        source_time = getattr(data_value, "SourceTimestamp", None)
        server_time = getattr(data_value, "ServerTimestamp", None)
        timestamp = source_time or server_time or self._now()
        return await self._publish_value(
            server_name,
            node_id,
            value,
            config=config,
            status_text=status_text,
            timestamp=timestamp,
        )

    async def _publish_value(
        self,
        server_name: str,
        node_id: str,
        value: Any,
        config: Mapping[str, Any] | None,
        status_text: str,
        timestamp: datetime,
    ) -> PointValue:
        node_id = self._canonical_node_id(node_id)
        normalized = self._normalize_node_config(
            server_name,
            config or {"node_id": node_id},
        )
        data_type = str(normalized.get("data_type", "Auto") or "Auto")
        if data_type.upper() == "AUTO":
            data_type = (
                type(value).__name__
                if value is not None
                else "NoneType"
            )

        point_value = PointValue(
            point_key=make_opcua_point_key(server_name, node_id),
            protocol=PROTOCOL_OPCUA,
            source_name=str(
                normalized.get("source_name", server_name)
            ),
            device_name=str(
                normalized.get("device_name", server_name)
            ),
            point_name=str(normalized.get("point_name", node_id)),
            address_text=node_id,
            value=value,
            value_text=self._value_text(value),
            value_number=self._value_number(value),
            status_text=status_text,
            timestamp=timestamp,
            writable=self._to_bool(
                normalized.get("writable", False)
            ),
            data_type=data_type,
            raw_config=self._sanitize(normalized),
        )

        with self._state_lock:
            self.latest_values[point_value.point_key] = point_value
        self._set_point_status(
            server_name,
            node_id,
            status_text,
            "",
            timestamp,
        )
        self.value_bus.publish(point_value)
        return point_value

    def _point_to_dict(self, point_value: PointValue) -> dict[str, Any]:
        return point_value.to_dict()

    def _record_point_error(
        self,
        server_name: str,
        node_id: str,
        action: str,
        exc: Exception,
    ):
        error_text = str(exc)
        self._set_point_status(
            server_name,
            node_id,
            f"{action}失敗",
            error_text,
            self._now(),
        )
        self._log(
            f"Server「{server_name}」Node「{node_id}」"
            f"{action}失敗：{error_text}",
            "ERROR",
        )

    def _set_server_status(
        self,
        server_name: str,
        connected: bool,
        status_text: str,
        error_text: str = "",
    ):
        with self._state_lock:
            self._connected[server_name] = connected
            self.latest_status[server_name] = {
                "server_name": server_name,
                "connected": connected,
                "status_text": status_text,
                "error_text": error_text,
                "timestamp": self._now(),
            }

    def _set_point_status(
        self,
        server_name: str,
        node_id: str,
        status_text: str,
        error_text: str,
        timestamp: datetime,
    ):
        point_key = make_opcua_point_key(server_name, node_id)
        with self._state_lock:
            self.latest_point_status[point_key] = {
                "point_key": point_key,
                "server_name": server_name,
                "node_id": node_id,
                "status_text": status_text,
                "error_text": error_text,
                "timestamp": timestamp,
            }

    # ------------------------------------------------------------------
    # 設定解析與工具函式
    # ------------------------------------------------------------------
    def _load_server_configs(self) -> dict[str, dict[str, Any]]:
        root = self._config_snapshot()
        opcua = root.get(
            "opcua",
            root.get("OPCUA", root.get("opc_ua", {})),
        )
        if not opcua and "opcua_servers" in root:
            opcua = {"servers": root.get("opcua_servers")}

        if isinstance(opcua, Mapping):
            root_enabled = self._to_bool(
                opcua.get("enable", opcua.get("enabled", True)),
                True,
            )
            if not root_enabled:
                return {}

        if isinstance(opcua, list):
            raw_servers = opcua
        elif isinstance(opcua, Mapping):
            raw_servers = opcua.get(
                "servers",
                opcua.get("server_list", []),
            )
        else:
            raw_servers = []

        items: list[tuple[str | None, Any]] = []
        if isinstance(raw_servers, Mapping):
            items = [
                (str(name), value)
                for name, value in raw_servers.items()
            ]
        elif isinstance(raw_servers, list):
            items = [(None, value) for value in raw_servers]

        output: dict[str, dict[str, Any]] = {}
        for mapping_name, raw in items:
            if not isinstance(raw, Mapping):
                continue
            config = copy.deepcopy(dict(raw))
            enabled = self._to_bool(
                config.get("enable", config.get("enabled", True)),
                True,
            )
            if not enabled:
                continue

            server_name = str(
                config.get(
                    "server_name",
                    config.get("name", mapping_name or ""),
                )
                or ""
            ).strip()
            if not server_name:
                continue

            endpoint = config.get(
                "endpoint_url",
                config.get("endpoint", config.get("url", "")),
            )
            config["enable"] = True
            config["server_name"] = server_name
            config["endpoint_url"] = str(endpoint or "").strip()
            nodes = config.get(
                "nodes",
                config.get(
                    "points",
                    config.get("subscriptions", []),
                ),
            )
            if isinstance(nodes, Mapping):
                nodes = [
                    dict(
                        value,
                        node_id=value.get("node_id", key),
                    )
                    for key, value in nodes.items()
                    if isinstance(value, Mapping)
                ]
            config["nodes"] = (
                copy.deepcopy(nodes)
                if isinstance(nodes, list)
                else []
            )
            if server_name in output:
                raise ValueError(
                    f"OPC UA Server名稱重複：{server_name}"
                )
            output[server_name] = config
        return output

    def _config_snapshot(self) -> dict[str, Any]:
        candidates: list[Any] = []
        getter = getattr(self.config_manager, "get_config", None)
        if callable(getter):
            try:
                candidates.append(getter())
            except TypeError:
                pass
        for attr in ("config", "data", "settings"):
            candidates.append(
                getattr(self.config_manager, attr, None)
            )
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                return copy.deepcopy(dict(candidate))
        return {}

    def _normalize_node_config(
        self,
        server_name: str,
        node_config: Any,
    ) -> dict[str, Any]:
        if isinstance(node_config, Mapping):
            config = copy.deepcopy(dict(node_config))
        else:
            config = {"node_id": str(node_config)}
        node_id = config.get(
            "node_id",
            config.get(
                "address",
                config.get("address_text", ""),
            ),
        )
        node_id = self._canonical_node_id(node_id)
        if not node_id:
            raise ValueError(
                f"Server「{server_name}」的Node設定缺少node_id"
            )
        config["node_id"] = node_id
        config.setdefault(
            "enable",
            config.get("enabled", True),
        )
        config.setdefault("server_name", server_name)
        config.setdefault("source_name", server_name)
        config.setdefault("device_name", server_name)
        config.setdefault(
            "point_name",
            config.get("name", node_id),
        )
        config.setdefault("writable", False)
        config.setdefault("data_type", "Auto")
        return config

    def _configured_node_config(
        self,
        server_name: str,
        node_id: str,
    ) -> dict[str, Any]:
        node_id = self._canonical_node_id(node_id)
        with self._state_lock:
            subscribed = self._node_configs.get(
                server_name,
                {},
            ).get(node_id)
            server_config = self._server_configs.get(server_name)
        if isinstance(subscribed, Mapping):
            return self._normalize_node_config(
                server_name,
                subscribed,
            )

        if isinstance(server_config, Mapping):
            nodes = server_config.get("nodes", [])
            if isinstance(nodes, list):
                for node_config in nodes:
                    if not isinstance(node_config, Mapping):
                        continue
                    configured_id = self._canonical_node_id(
                        node_config.get(
                            "node_id",
                            node_config.get("address", ""),
                        )
                    )
                    if configured_id == node_id:
                        return self._normalize_node_config(
                            server_name,
                            node_config,
                        )

        return self._normalize_node_config(
            server_name,
            {"node_id": node_id},
        )

    def _require_server_config(
        self,
        server_name: str,
    ) -> dict[str, Any]:
        with self._state_lock:
            config = self._server_configs.get(server_name)
        if config is None:
            raise KeyError(
                f"找不到OPC UA Server設定：{server_name}"
            )
        return config

    def _require_connected(self, server_name: str) -> Client:
        with self._state_lock:
            client = self._clients.get(server_name)
            connected = self._connected.get(server_name, False)
        if client is None or not connected:
            raise RuntimeError(
                f"OPC UA Server「{server_name}」尚未連線"
            )
        return client

    def _canonical_node_id(self, node_id: Any) -> str:
        if hasattr(node_id, "to_string"):
            return str(node_id.to_string())
        text = str(node_id or "").strip()
        if text.startswith("NodeId(") and text.endswith(")"):
            text = text[7:-1]
        return text

    def _namespace_index(self, node_id: str) -> int:
        text = self._canonical_node_id(node_id)
        if text.startswith("ns="):
            try:
                return int(text.split(";", 1)[0][3:])
            except (TypeError, ValueError):
                return -1
        return 0

    def _node_class_name(self, value: Any) -> str:
        name = getattr(value, "name", None)
        if name:
            return str(name)
        text = str(value)
        return text.rsplit(".", 1)[-1]

    def _localized_text(self, value: Any) -> str:
        return str(getattr(value, "Text", value) or "")

    def _qualified_name_text(self, value: Any) -> str:
        name = getattr(value, "Name", None)
        namespace_index = getattr(value, "NamespaceIndex", None)
        if name is None:
            return str(value or "")
        return (
            f"{namespace_index}:{name}"
            if namespace_index not in (None, 0)
            else str(name)
        )

    def _value_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.hex(" ")
        if isinstance(value, (dict, list, tuple)):
            try:
                return json.dumps(
                    value,
                    ensure_ascii=False,
                    default=str,
                )
            except TypeError:
                pass
        return str(value)

    def _value_number(self, value: Any):
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, numbers.Real):
            number = float(value)
            return number if math.isfinite(number) else None
        return None

    def _safe_endpoint(self, endpoint: Any) -> str:
        text = str(endpoint or "")
        try:
            parsed = urlsplit(text)
            hostname = parsed.hostname or ""
            port = (
                f":{parsed.port}"
                if parsed.port is not None
                else ""
            )
            return urlunsplit(
                (
                    parsed.scheme,
                    f"{hostname}{port}",
                    parsed.path,
                    parsed.query,
                    parsed.fragment,
                )
            )
        except Exception:
            return text

    def _redact_error(
        self,
        config: Mapping[str, Any],
        exc: Exception,
    ) -> str:
        text = str(exc)
        auth = (
            config.get("auth")
            if isinstance(config.get("auth"), Mapping)
            else {}
        )
        password = str(
            config.get("password", auth.get("password", "")) or ""
        )
        if password:
            text = text.replace(password, "***")
        return text

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): (
                    ""
                    if str(key).lower() in SECRET_KEYS
                    else self._sanitize(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._sanitize(item) for item in value)
        return copy.deepcopy(value)

    def _to_bool(self, value: Any, default=False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        text = str(value).strip().lower()
        if text in {
            "1", "true", "yes", "on", "y", "是", "啟用"
        }:
            return True
        if text in {
            "0", "false", "no", "off", "n", "否", "停用"
        }:
            return False
        return default

    def _to_float(
        self,
        value: Any,
        default: float,
        minimum: float | None = None,
    ) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            result = default
        return max(minimum, result) if minimum is not None else result

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _log(self, message: str, level="INFO"):
        callback = self.log_callback
        if callback is None:
            return
        try:
            callback(message, level)
        except TypeError:
            try:
                callback(message)
            except TypeError:
                callback(level, message)
        except Exception:
            pass
