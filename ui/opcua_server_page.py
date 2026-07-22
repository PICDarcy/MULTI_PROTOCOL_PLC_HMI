"""OPC UA Server與Node設定頁面。

此頁面只透過app_context提供的opcua_manager執行連線與訂閱操作，
不會直接建立asyncua Client。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import inspect
import json
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable, Optional


SERVER_DEFAULTS = {
    "enable": True,
    "name": "",
    "endpoint_url": "opc.tcp://127.0.0.1:4840",
    "use_username": False,
    "username": "",
    "password": "",
    "timeout": 5.0,
    "subscription_interval_ms": 1000,
    "nodes": [],
}

NODE_DEFAULTS = {
    "enable": True,
    "name": "",
    "node_id": "",
    "subscribe": True,
    "writable": False,
    "data_type": "Auto",
    "db_enable": True,
}

DATA_TYPES = (
    "Auto", "Boolean", "Byte", "SByte", "Int16", "UInt16", "Int32",
    "UInt32", "Int64", "UInt64", "Float", "Double", "String", "DateTime",
)


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "是", "啟用"}
    return default


class _FormDialog(tk.Toplevel):
    """Server與Node共用的模態編輯視窗。"""

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        fields: list[dict[str, Any]],
        initial: dict[str, Any],
        validator: Callable[[dict[str, Any]], Optional[str]],
    ) -> None:
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self.result: Optional[dict[str, Any]] = None
        self._fields = fields
        self._validator = validator
        self._vars: dict[str, tk.Variable] = {}

        body = ttk.Frame(self, padding=14)
        body.grid(sticky="nsew")
        body.columnconfigure(1, weight=1)

        for row, field in enumerate(fields):
            key = field["key"]
            field_type = field.get("type", "text")
            value = initial.get(key, field.get("default", ""))
            ttk.Label(body, text=field["label"]).grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=5
            )

            if field_type == "bool":
                var: tk.Variable = tk.BooleanVar(value=_bool(value))
                widget: tk.Widget = ttk.Checkbutton(body, variable=var)
            else:
                var = tk.StringVar(value="" if value is None else str(value))
                if field_type == "combo":
                    widget = ttk.Combobox(
                        body, textvariable=var, values=field.get("values", ()),
                        state="readonly", width=42,
                    )
                else:
                    widget = ttk.Entry(
                        body, textvariable=var, width=45,
                        show="*" if field_type == "password" else "",
                    )
            widget.grid(row=row, column=1, sticky="ew", pady=5)
            self._vars[key] = var

        buttons = ttk.Frame(body)
        buttons.grid(row=len(fields), column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="確定", command=self._ok).pack(side="left", padx=4)
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="left", padx=4)

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Return>", lambda _event: self._ok())
        self.update_idletasks()
        root = parent.winfo_toplevel()
        x = root.winfo_rootx() + max(0, (root.winfo_width() - self.winfo_reqwidth()) // 2)
        y = root.winfo_rooty() + max(0, (root.winfo_height() - self.winfo_reqheight()) // 2)
        self.geometry(f"+{x}+{y}")
        self.grab_set()
        self.focus_force()

    def _ok(self) -> None:
        values: dict[str, Any] = {}
        for field in self._fields:
            key = field["key"]
            field_type = field.get("type", "text")
            raw = self._vars[key].get()
            try:
                if field_type == "bool":
                    values[key] = bool(raw)
                elif field_type == "float":
                    values[key] = float(str(raw).strip())
                elif field_type == "int":
                    values[key] = int(str(raw).strip())
                else:
                    values[key] = str(raw).strip()
            except ValueError:
                messagebox.showerror("欄位錯誤", f"「{field['label']}」格式不正確。", parent=self)
                return

        error = self._validator(values)
        if error:
            messagebox.showerror("資料驗證失敗", error, parent=self)
            return
        self.result = values
        self.destroy()


class OpcuaServerPage(ttk.Frame):
    """管理多個OPC UA Server與其Node設定。"""

    def __init__(self, parent, app_context):
        super().__init__(parent)
        self.app_context = app_context
        self.config_manager = self._ctx("config_manager")
        self.opcua_manager = self._ctx("opcua_manager")
        self.log_func = self._ctx("log_func", lambda _message: None)
        self.refresh_all = self._ctx("refresh_all", lambda: None)

        self._config: dict[str, Any] = {}
        self._config_style = "opcua.servers"
        self._servers: list[dict[str, Any]] = []
        self._server_status: dict[str, str] = {}
        self._node_status: dict[tuple[str, str], str] = {}
        self.status_var = tk.StringVar(value="就緒")

        self._build_ui()
        self.reload_from_config()

    def _ctx(self, name: str, default: Any = None) -> Any:
        if isinstance(self.app_context, dict):
            return self.app_context.get(name, default)
        return getattr(self.app_context, name, default)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        paned = ttk.Panedwindow(self, orient=tk.VERTICAL)
        paned.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 4))

        server_box = ttk.LabelFrame(paned, text="OPC UA Server設定", padding=8)
        node_box = ttk.LabelFrame(paned, text="選取Server的Node設定", padding=8)
        paned.add(server_box, weight=1)
        paned.add(node_box, weight=1)
        self._build_server_ui(server_box)
        self._build_node_ui(node_box)

        footer = ttk.Frame(self, padding=(8, 4, 8, 8))
        footer.grid(row=1, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status_var, anchor="w").grid(row=0, column=0, sticky="ew")
        ttk.Button(footer, text="重新載入", command=self.reload_from_config).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(footer, text="儲存設定", command=self._save).grid(row=0, column=2, padx=(8, 0))

    def _build_server_ui(self, parent: ttk.LabelFrame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        buttons = ttk.Frame(parent)
        buttons.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for text, command in (
            ("新增Server", self._add_server), ("修改Server", self._edit_server),
            ("刪除Server", self._delete_server), ("連線選取Server", self._connect_selected),
            ("斷線選取Server", self._disconnect_selected), ("連線全部", self._connect_all),
            ("斷線全部", self._disconnect_all),
        ):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 6), pady=2)

        columns = ("enable", "name", "endpoint", "login", "username", "timeout", "interval", "status")
        headings = ("啟用", "名稱", "Endpoint URL", "帳密登入", "使用者名稱", "逾時(秒)", "訂閱間隔(ms)", "連線狀態")
        widths = (60, 130, 310, 80, 120, 80, 120, 100)
        self.server_tree = self._make_tree(parent, columns, headings, widths, row=1)
        self.server_tree.column("name", anchor="w")
        self.server_tree.column("endpoint", anchor="w")
        self.server_tree.column("username", anchor="w")
        self.server_tree.bind("<<TreeviewSelect>>", lambda _event: self._refresh_nodes())
        self.server_tree.bind("<Double-1>", lambda _event: self._edit_server())

    def _build_node_ui(self, parent: ttk.LabelFrame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        buttons = ttk.Frame(parent)
        buttons.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for text, command in (
            ("新增Node", self._add_node), ("修改Node", self._edit_node),
            ("刪除Node", self._delete_node), ("訂閱選取Node", self._subscribe_selected),
            ("取消訂閱選取Node", self._unsubscribe_selected), ("訂閱全部", self._subscribe_all),
        ):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 6), pady=2)

        columns = ("enable", "name", "node_id", "subscribe", "writable", "data_type", "db_enable", "status")
        headings = ("啟用", "名稱", "Node ID", "訂閱", "可寫入", "資料型別", "寫入DB", "訂閱狀態")
        widths = (60, 150, 320, 65, 70, 100, 70, 100)
        self.node_tree = self._make_tree(parent, columns, headings, widths, row=1)
        self.node_tree.column("name", anchor="w")
        self.node_tree.column("node_id", anchor="w")
        self.node_tree.bind("<Double-1>", lambda _event: self._edit_node())

    @staticmethod
    def _make_tree(
        parent: ttk.LabelFrame,
        columns: tuple[str, ...],
        headings: tuple[str, ...],
        widths: tuple[int, ...],
        row: int,
    ) -> ttk.Treeview:
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=0, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        tree = ttk.Treeview(holder, columns=columns, show="headings", selectmode="browse")
        for column, heading, width in zip(columns, headings, widths):
            tree.heading(column, text=heading)
            tree.column(column, width=width, minwidth=55, anchor="center")
        ybar = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        xbar = ttk.Scrollbar(holder, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        return tree

    def reload_from_config(self) -> None:
        try:
            self._config = self._load_config()
            raw = self._extract_servers(self._config)
            self._servers = [self._normalize_server(item) for item in raw]
            self._refresh_servers()
            self.status_var.set(f"已載入{len(self._servers)}組OPC UA Server設定")
        except Exception as exc:
            self._error("載入OPC UA設定失敗", exc)

    def _load_config(self) -> dict[str, Any]:
        manager = self.config_manager
        if manager is not None:
            for name in ("get_config", "load_config", "load", "read_config"):
                method = getattr(manager, name, None)
                if callable(method):
                    result = method()
                    if isinstance(result, dict):
                        return copy.deepcopy(result)
            for name in ("config", "data", "settings"):
                value = getattr(manager, name, None)
                if isinstance(value, dict):
                    return copy.deepcopy(value)
        path = self._config_path()
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as file:
            result = json.load(file)
        if not isinstance(result, dict):
            raise ValueError("config.json根節點必須是JSON物件。")
        return result

    def _extract_servers(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        opcua = config.get("opcua")
        if isinstance(opcua, dict) and isinstance(opcua.get("servers"), list):
            self._config_style = "opcua.servers"
            return [x for x in opcua["servers"] if isinstance(x, dict)]
        if isinstance(config.get("opcua_servers"), list):
            self._config_style = "opcua_servers"
            return [x for x in config["opcua_servers"] if isinstance(x, dict)]
        protocols = config.get("protocols")
        if isinstance(protocols, dict):
            opcua = protocols.get("opcua")
            if isinstance(opcua, dict) and isinstance(opcua.get("servers"), list):
                self._config_style = "protocols.opcua.servers"
                return [x for x in opcua["servers"] if isinstance(x, dict)]
        self._config_style = "opcua.servers"
        return []

    def _normalize_server(self, source: dict[str, Any]) -> dict[str, Any]:
        server = copy.deepcopy(SERVER_DEFAULTS)
        server.update(copy.deepcopy(source))
        server["enable"] = _bool(
            server.get("enable", server.get("enabled")),
            True,
        )
        server["use_username"] = _bool(server.get("use_username"), False)
        server["name"] = str(server.get("name", "")).strip()
        server["endpoint_url"] = str(server.get("endpoint_url", "")).strip()
        server["username"] = str(server.get("username", ""))
        server["password"] = str(server.get("password", ""))
        try:
            server["timeout"] = float(server.get("timeout", 5.0))
        except (TypeError, ValueError):
            server["timeout"] = 5.0
        try:
            server["subscription_interval_ms"] = int(server.get("subscription_interval_ms", 1000))
        except (TypeError, ValueError):
            server["subscription_interval_ms"] = 1000
        nodes = source.get("nodes", [])
        server["nodes"] = [self._normalize_node(x) for x in nodes if isinstance(x, dict)] if isinstance(nodes, list) else []
        return server

    @staticmethod
    def _normalize_node(source: dict[str, Any]) -> dict[str, Any]:
        node = copy.deepcopy(NODE_DEFAULTS)
        node.update(copy.deepcopy(source))
        node["enable"] = _bool(
            node.get("enable", node.get("enabled")),
            NODE_DEFAULTS["enable"],
        )
        for key in ("subscribe", "writable", "db_enable"):
            node[key] = _bool(node.get(key), NODE_DEFAULTS[key])
        node["name"] = str(node.get("name", "")).strip()
        node["node_id"] = str(node.get("node_id", "")).strip()
        node["data_type"] = str(node.get("data_type", "Auto")).strip() or "Auto"
        return node

    def _refresh_servers(self, selected: Optional[int] = None) -> None:
        if selected is None:
            selected = self._server_index()
        self.server_tree.delete(*self.server_tree.get_children())
        for index, server in enumerate(self._servers):
            self.server_tree.insert("", "end", iid=f"s{index}", values=(
                self._yn(server["enable"]), server["name"], server["endpoint_url"],
                self._yn(server["use_username"]), server["username"] if server["use_username"] else "",
                server["timeout"], server["subscription_interval_ms"],
                self._server_status.get(server["name"], "未連線"),
            ))
        if self._servers:
            selected = 0 if selected is None else max(0, min(selected, len(self._servers) - 1))
            iid = f"s{selected}"
            self.server_tree.selection_set(iid)
            self.server_tree.focus(iid)
            self.server_tree.see(iid)
        self._refresh_nodes()

    def _refresh_nodes(self, selected: Optional[int] = None) -> None:
        if selected is None:
            selected = self._node_index()
        self.node_tree.delete(*self.node_tree.get_children())
        server_index = self._server_index()
        if server_index is None:
            return
        server = self._servers[server_index]
        for index, node in enumerate(server["nodes"]):
            self.node_tree.insert("", "end", iid=f"n{index}", values=(
                self._yn(node["enable"]), node["name"], node["node_id"],
                self._yn(node["subscribe"]), self._yn(node["writable"]), node["data_type"],
                self._yn(node["db_enable"]), self._node_status.get((server["name"], node["node_id"]), "未訂閱"),
            ))
        if server["nodes"]:
            selected = 0 if selected is None else max(0, min(selected, len(server["nodes"]) - 1))
            iid = f"n{selected}"
            self.node_tree.selection_set(iid)
            self.node_tree.focus(iid)
            self.node_tree.see(iid)

    @staticmethod
    def _yn(value: Any) -> str:
        return "是" if _bool(value) else "否"

    def _server_index(self) -> Optional[int]:
        selected = self.server_tree.selection()
        try:
            return int(selected[0][1:]) if selected else None
        except ValueError:
            return None

    def _node_index(self) -> Optional[int]:
        selected = self.node_tree.selection()
        try:
            return int(selected[0][1:]) if selected else None
        except ValueError:
            return None

    def _server_fields(self) -> list[dict[str, Any]]:
        return [
            {"key": "enable", "label": "啟用", "type": "bool"},
            {"key": "name", "label": "Server名稱"},
            {"key": "endpoint_url", "label": "Endpoint URL"},
            {"key": "use_username", "label": "使用帳號密碼", "type": "bool"},
            {"key": "username", "label": "使用者名稱"},
            {"key": "password", "label": "密碼", "type": "password"},
            {"key": "timeout", "label": "連線逾時(秒)", "type": "float"},
            {"key": "subscription_interval_ms", "label": "訂閱間隔(ms)", "type": "int"},
        ]

    def _node_fields(self) -> list[dict[str, Any]]:
        return [
            {"key": "enable", "label": "啟用", "type": "bool"},
            {"key": "name", "label": "Node名稱"},
            {"key": "node_id", "label": "Node ID"},
            {"key": "subscribe", "label": "啟用訂閱", "type": "bool"},
            {"key": "writable", "label": "允許寫入", "type": "bool"},
            {"key": "data_type", "label": "資料型別", "type": "combo", "values": DATA_TYPES},
            {"key": "db_enable", "label": "寫入資料庫", "type": "bool"},
        ]

    def _dialog(self, title: str, fields: list[dict[str, Any]], initial: dict[str, Any], validator: Callable[[dict[str, Any]], Optional[str]]) -> Optional[dict[str, Any]]:
        dialog = _FormDialog(self, title, fields, initial, validator)
        self.wait_window(dialog)
        return dialog.result

    def _validate_server(self, values: dict[str, Any], editing: Optional[int] = None) -> Optional[str]:
        name = values.get("name", "")
        endpoint = values.get("endpoint_url", "")
        if not name:
            return "Server名稱不可空白。"
        if not endpoint:
            return "Endpoint URL不可空白。"
        if not endpoint.lower().startswith(("opc.tcp://", "http://", "https://")):
            return "Endpoint URL格式不正確，通常應以opc.tcp://開頭。"
        if values.get("timeout", 0) <= 0:
            return "連線逾時必須大於0秒。"
        if values.get("subscription_interval_ms", 0) <= 0:
            return "訂閱間隔必須大於0毫秒。"
        if values.get("use_username") and not values.get("username"):
            return "啟用帳號密碼登入時，使用者名稱不可空白。"
        for index, server in enumerate(self._servers):
            if index != editing and server["name"].casefold() == name.casefold():
                return f"Server名稱「{name}」已存在。"
        return None

    def _validate_node(self, values: dict[str, Any], server_index: int, editing: Optional[int] = None) -> Optional[str]:
        if not values.get("name"):
            return "Node名稱不可空白。"
        if not values.get("node_id"):
            return "Node ID不可空白。"
        for index, node in enumerate(self._servers[server_index]["nodes"]):
            if index == editing:
                continue
            if node["name"].casefold() == values["name"].casefold():
                return f"Node名稱「{values['name']}」已存在。"
            if node["node_id"] == values["node_id"]:
                return f"Node ID「{values['node_id']}」已存在。"
        return None

    def _add_server(self) -> None:
        initial = copy.deepcopy(SERVER_DEFAULTS)
        initial["name"] = self._next_name("server", [x["name"] for x in self._servers])
        result = self._dialog("新增OPC UA Server", self._server_fields(), initial, self._validate_server)
        if result is not None:
            result["nodes"] = []
            self._servers.append(self._normalize_server(result))
            self._refresh_servers(len(self._servers) - 1)
            self._save()

    def _edit_server(self) -> None:
        index = self._require_server()
        if index is None:
            return
        original = copy.deepcopy(self._servers[index])
        result = self._dialog("修改OPC UA Server", self._server_fields(), original, lambda x: self._validate_server(x, index))
        if result is None:
            return
        old_name = original["name"]
        result["nodes"] = original["nodes"]
        self._servers[index] = self._normalize_server(result)
        new_name = self._servers[index]["name"]
        if old_name != new_name:
            if old_name in self._server_status:
                self._server_status[new_name] = self._server_status.pop(old_name)
            for key in list(self._node_status):
                if key[0] == old_name:
                    self._node_status[(new_name, key[1])] = self._node_status.pop(key)
        self._refresh_servers(index)
        self._save()

    def _delete_server(self) -> None:
        index = self._require_server()
        if index is None:
            return
        server = self._servers[index]
        if not messagebox.askyesno("刪除Server", f"確定刪除Server「{server['name']}」及全部Node嗎？", parent=self):
            return
        self._servers.pop(index)
        self._server_status.pop(server["name"], None)
        for key in list(self._node_status):
            if key[0] == server["name"]:
                self._node_status.pop(key, None)
        self._refresh_servers(min(index, len(self._servers) - 1))
        self._save()

    def _add_node(self) -> None:
        server_index = self._require_server()
        if server_index is None:
            return
        server = self._servers[server_index]
        initial = copy.deepcopy(NODE_DEFAULTS)
        initial["name"] = self._next_name("node", [x["name"] for x in server["nodes"]])
        result = self._dialog(f"新增Node－{server['name']}", self._node_fields(), initial, lambda x: self._validate_node(x, server_index))
        if result is not None:
            server["nodes"].append(self._normalize_node(result))
            self._refresh_nodes(len(server["nodes"]) - 1)
            self._save()

    def _edit_node(self) -> None:
        server_index = self._require_server()
        node_index = self._require_node()
        if server_index is None or node_index is None:
            return
        server = self._servers[server_index]
        original = copy.deepcopy(server["nodes"][node_index])
        result = self._dialog(f"修改Node－{server['name']}", self._node_fields(), original, lambda x: self._validate_node(x, server_index, node_index))
        if result is None:
            return
        old_id = original["node_id"]
        server["nodes"][node_index] = self._normalize_node(result)
        new_id = server["nodes"][node_index]["node_id"]
        if old_id != new_id and (server["name"], old_id) in self._node_status:
            self._node_status[(server["name"], new_id)] = self._node_status.pop((server["name"], old_id))
        self._refresh_nodes(node_index)
        self._save()

    def _delete_node(self) -> None:
        server_index = self._require_server()
        node_index = self._require_node()
        if server_index is None or node_index is None:
            return
        server = self._servers[server_index]
        node = server["nodes"][node_index]
        if not messagebox.askyesno("刪除Node", f"確定刪除Node「{node['name']}」嗎？", parent=self):
            return
        server["nodes"].pop(node_index)
        self._node_status.pop((server["name"], node["node_id"]), None)
        self._refresh_nodes(min(node_index, len(server["nodes"]) - 1))
        self._save()

    def _save(self) -> None:
        try:
            config = copy.deepcopy(self._config)
            self._put_servers(config, copy.deepcopy(self._servers))
            self._write_config(config)
            self._config = config
            reload_method = getattr(self.opcua_manager, "reload_config", None)
            if callable(reload_method):
                self.status_var.set("OPC UA設定已儲存，正在重新載入…")
                self._run_awaitable(
                    reload_method(),
                    "重新載入OPC UA設定",
                    success=self._save_done,
                )
                return
            self._save_done()
        except Exception as exc:
            self._error("儲存OPC UA設定失敗", exc)

    def _save_done(self) -> None:
        self._refresh_app()
        self.status_var.set("OPC UA設定已儲存")
        self._log("OPC UA Server與Node設定已儲存。")

    def _put_servers(self, config: dict[str, Any], servers: list[dict[str, Any]]) -> None:
        if self._config_style == "opcua_servers":
            config["opcua_servers"] = servers
        elif self._config_style == "protocols.opcua.servers":
            protocols = config.setdefault("protocols", {})
            if not isinstance(protocols, dict):
                protocols = config["protocols"] = {}
            opcua = protocols.setdefault("opcua", {})
            if not isinstance(opcua, dict):
                opcua = protocols["opcua"] = {}
            opcua["servers"] = servers
        else:
            opcua = config.setdefault("opcua", {})
            if not isinstance(opcua, dict):
                opcua = config["opcua"] = {}
            opcua["servers"] = servers

    def _write_config(self, config: dict[str, Any]) -> None:
        manager = self.config_manager
        if manager is not None:
            for name in ("save_config", "write_config", "update_config"):
                method = getattr(manager, name, None)
                if callable(method):
                    method(copy.deepcopy(config))
                    return
            save = getattr(manager, "save", None)
            if callable(save):
                try:
                    save(copy.deepcopy(config))
                except TypeError:
                    for attr in ("config", "data", "settings"):
                        if hasattr(manager, attr):
                            setattr(manager, attr, copy.deepcopy(config))
                            save()
                            return
                else:
                    return
        path = self._config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(config, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temp, path)

    def _config_path(self) -> Path:
        configured = getattr(self.config_manager, "config_path", None)
        return Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parents[1] / "config.json"

    def _connect_selected(self) -> None:
        index = self._require_server()
        if index is not None:
            name = self._servers[index]["name"]
            self._manager("連線選取Server", "connect_server", name, success=lambda: self._set_server_status(name, "已連線"))

    def _disconnect_selected(self) -> None:
        index = self._require_server()
        if index is not None:
            name = self._servers[index]["name"]
            self._manager("斷線選取Server", "disconnect_server", name, success=lambda: self._set_server_status(name, "未連線"))

    def _connect_all(self) -> None:
        self._manager("連線全部Server", "connect_all", success=lambda: self._set_all_server_status("已連線", True))

    def _disconnect_all(self) -> None:
        self._manager("斷線全部Server", "disconnect_all", success=lambda: self._set_all_server_status("未連線", False))

    def _subscribe_selected(self) -> None:
        server_index = self._require_server()
        node_index = self._require_node()
        if server_index is None or node_index is None:
            return
        server = self._servers[server_index]
        node = copy.deepcopy(server["nodes"][node_index])
        self._manager("訂閱選取Node", "subscribe_node", server["name"], node, success=lambda: self._set_node_status(server["name"], node["node_id"], "已訂閱"))

    def _unsubscribe_selected(self) -> None:
        server_index = self._require_server()
        node_index = self._require_node()
        if server_index is None or node_index is None:
            return
        server = self._servers[server_index]
        node = server["nodes"][node_index]
        self._manager("取消訂閱選取Node", "unsubscribe_node", server["name"], node["node_id"], success=lambda: self._set_node_status(server["name"], node["node_id"], "未訂閱"))

    def _subscribe_all(self) -> None:
        self._manager("訂閱全部Node", "subscribe_all", success=self._mark_subscribed)

    def _manager(self, action: str, method_name: str, *args: Any, success: Optional[Callable[[], None]] = None) -> None:
        method = getattr(self.opcua_manager, method_name, None)
        if not callable(method):
            messagebox.showerror("功能無法使用", f"opcua_manager未提供{method_name}()。", parent=self)
            return
        self.status_var.set(f"{action}執行中…")

        def worker() -> None:
            try:
                self._resolve_async_result(method(*args))
            except Exception as exc:
                self._safe_after(lambda error=exc: self._error(f"{action}失敗", error))
                return
            self._safe_after(lambda: self._manager_done(action, success))

        threading.Thread(target=worker, name=f"OpcuaServerPage-{method_name}", daemon=True).start()

    def _run_awaitable(
        self,
        result: Any,
        action: str,
        success: Optional[Callable[[], None]] = None,
    ) -> None:
        """相容舊名稱，實際可處理concurrent Future與awaitable。"""
        def worker() -> None:
            try:
                self._resolve_async_result(result)
            except Exception as exc:
                self._safe_after(lambda error=exc: self._error(f"{action}失敗", error))
                return
            if success:
                self._safe_after(success)

        threading.Thread(target=worker, name="OpcuaServerPage-reload", daemon=True).start()

    @staticmethod
    def _resolve_async_result(result: Any) -> Any:
        if isinstance(result, concurrent.futures.Future):
            return result.result(timeout=30.0)

        if inspect.iscoroutine(result):
            return asyncio.run(result)

        if isinstance(result, asyncio.Future):
            if result.done():
                return result.result()
            loop = result.get_loop()
            if loop.is_running():
                async def wait_future() -> Any:
                    return await result

                return asyncio.run_coroutine_threadsafe(
                    wait_future(),
                    loop,
                ).result(timeout=30.0)
            return loop.run_until_complete(result)

        if inspect.isawaitable(result):
            async def wait_awaitable() -> Any:
                return await result

            return asyncio.run(wait_awaitable())

        return result

    def _safe_after(self, callback: Callable[[], None]) -> None:
        try:
            self.after(0, callback)
        except (tk.TclError, RuntimeError):
            pass

    def _manager_done(self, action: str, success: Optional[Callable[[], None]]) -> None:
        if success:
            success()
        self.status_var.set(f"{action}完成")
        self._log(f"{action}完成。")
        self._refresh_app()

    def _set_server_status(self, name: str, status: str) -> None:
        self._server_status[name] = status
        self._refresh_servers(self._server_index())

    def _set_all_server_status(self, status: str, enabled_only: bool) -> None:
        for server in self._servers:
            if not enabled_only or server["enable"]:
                self._server_status[server["name"]] = status
        self._refresh_servers(self._server_index())

    def _set_node_status(self, server_name: str, node_id: str, status: str) -> None:
        self._node_status[(server_name, node_id)] = status
        self._refresh_nodes(self._node_index())

    def _mark_subscribed(self) -> None:
        for server in self._servers:
            if server["enable"]:
                for node in server["nodes"]:
                    if node["enable"] and node["subscribe"]:
                        self._node_status[(server["name"], node["node_id"])] = "已訂閱"
        self._refresh_nodes(self._node_index())

    def _require_server(self) -> Optional[int]:
        index = self._server_index()
        if index is None or not 0 <= index < len(self._servers):
            messagebox.showwarning("尚未選取", "請先選取一組OPC UA Server。", parent=self)
            return None
        return index

    def _require_node(self) -> Optional[int]:
        server_index = self._server_index()
        node_index = self._node_index()
        if server_index is None or node_index is None or not 0 <= node_index < len(self._servers[server_index]["nodes"]):
            messagebox.showwarning("尚未選取", "請先選取一個Node。", parent=self)
            return None
        return node_index

    @staticmethod
    def _next_name(prefix: str, names: list[str]) -> str:
        used = {name.casefold() for name in names}
        number = 1
        while f"{prefix}_{number}".casefold() in used:
            number += 1
        return f"{prefix}_{number}"

    def _refresh_app(self) -> None:
        try:
            if callable(self.refresh_all):
                self.refresh_all()
        except Exception as exc:
            self._log(f"refresh_all執行失敗：{exc}")

    def _log(self, message: str) -> None:
        try:
            if callable(self.log_func):
                self.log_func(message)
        except Exception:
            pass

    def _error(self, title: str, exc: Exception) -> None:
        self.status_var.set(f"{title}：{exc}")
        self._log(f"{title}：{exc}")
        messagebox.showerror(title, str(exc), parent=self)
