"""OPC UA NodeId瀏覽與掃描頁面。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import inspect
import threading
from collections.abc import Iterable, Mapping
from typing import Any, Callable

import tkinter as tk
from tkinter import messagebox, ttk


class OpcuaBrowsePage(ttk.Frame):
    """提供多台OPC UA Server的節點瀏覽、掃描與監控設定功能。

    修正重點：
    - 不使用ThreadPoolExecutor，避免已開始的Future阻止Python程序結束。
    - 頁面背景工作一律使用daemon thread。
    - 等待opcua_manager回傳的Future時加入timeout。
    - destroy時設定stop_event，後續背景結果不再回寫Tkinter。
    """

    TREE_COLUMNS = (
        "display_name",
        "browse_name",
        "node_id",
        "node_class",
        "data_type",
        "path",
    )

    def __init__(self, parent: tk.Misc, app_context: Any):
        super().__init__(parent)
        self.app_context = app_context
        self.config_manager = self._context_get("config_manager")
        self.opcua_manager = self._context_get("opcua_manager")
        self.log_func = self._context_get("log_func")
        self.refresh_all = self._context_get("refresh_all")

        self._destroyed = False
        self._stop_event = threading.Event()
        self._busy_count = 0
        self._workers: set[threading.Thread] = set()
        self._workers_lock = threading.Lock()
        self._all_rows: list[dict[str, Any]] = []
        self._history: list[str] = []
        self._current_node_id = "i=85"
        self._row_by_item: dict[str, dict[str, Any]] = {}
        self._filter_after_id: str | None = None
        self._config_lock = threading.RLock()

        self.server_var = tk.StringVar()
        self.node_id_var = tk.StringVar(value="i=85")
        self.scan_start_var = tk.StringVar(value="i=85")
        self.max_depth_var = tk.StringVar(value="8")
        self.max_nodes_var = tk.StringVar(value="3000")
        self.only_variables_var = tk.BooleanVar(value=True)
        self.include_ns0_var = tk.BooleanVar(value=False)
        self.filter_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就緒")
        self.current_path_var = tk.StringVar(value="目前NodeId：i=85")

        self._build_ui()
        self._bind_events()
        self.refresh_server_list()

    def _context_get(self, key: str, default: Any = None) -> Any:
        if isinstance(self.app_context, Mapping):
            return self.app_context.get(key, default)
        return getattr(self.app_context, key, default)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        server_frame = ttk.LabelFrame(self, text="OPC UA Server")
        server_frame.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        server_frame.columnconfigure(1, weight=1)

        ttk.Label(server_frame, text="Server：").grid(row=0, column=0, sticky="w", padx=(8, 4), pady=8)
        self.server_combo = ttk.Combobox(server_frame, textvariable=self.server_var, state="readonly")
        self.server_combo.grid(row=0, column=1, sticky="ew", padx=4, pady=8)
        ttk.Button(server_frame, text="重新載入Server", command=self.refresh_server_list).grid(
            row=0, column=2, padx=(4, 8), pady=8
        )

        browse_frame = ttk.LabelFrame(self, text="逐層瀏覽")
        browse_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        browse_frame.columnconfigure(1, weight=1)

        ttk.Label(browse_frame, text="NodeId：").grid(row=0, column=0, sticky="w", padx=(8, 4), pady=6)
        self.node_id_entry = ttk.Entry(browse_frame, textvariable=self.node_id_var)
        self.node_id_entry.grid(row=0, column=1, sticky="ew", padx=4, pady=6)
        self.back_button = ttk.Button(browse_frame, text="上一頁", command=self.go_back, state="disabled")
        self.back_button.grid(row=0, column=2, padx=4, pady=6)
        ttk.Button(browse_frame, text="瀏覽下一層", command=self.browse_current_node).grid(
            row=0, column=3, padx=(4, 8), pady=6
        )
        ttk.Label(browse_frame, textvariable=self.current_path_var).grid(
            row=1, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 6)
        )

        scan_frame = ttk.LabelFrame(self, text="遞迴掃描")
        scan_frame.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        scan_frame.columnconfigure(1, weight=1)

        ttk.Label(scan_frame, text="掃描起點：").grid(row=0, column=0, sticky="w", padx=(8, 4), pady=6)
        ttk.Entry(scan_frame, textvariable=self.scan_start_var).grid(row=0, column=1, sticky="ew", padx=4, pady=6)
        ttk.Label(scan_frame, text="最大深度：").grid(row=0, column=2, sticky="e", padx=(8, 4), pady=6)
        ttk.Spinbox(scan_frame, from_=1, to=100, textvariable=self.max_depth_var, width=7).grid(
            row=0, column=3, sticky="w", padx=4, pady=6
        )
        ttk.Label(scan_frame, text="最大節點數：").grid(row=0, column=4, sticky="e", padx=(8, 4), pady=6)
        ttk.Spinbox(scan_frame, from_=1, to=100000, textvariable=self.max_nodes_var, width=9).grid(
            row=0, column=5, sticky="w", padx=4, pady=6
        )

        ttk.Checkbutton(scan_frame, text="只顯示Variable", variable=self.only_variables_var).grid(
            row=1, column=0, sticky="w", padx=8, pady=6
        )
        ttk.Checkbutton(scan_frame, text="包含ns=0", variable=self.include_ns0_var).grid(
            row=1, column=1, sticky="w", padx=4, pady=6
        )
        ttk.Button(scan_frame, text="依設定掃描", command=self.scan_custom).grid(row=1, column=3, padx=4, pady=6)
        ttk.Button(scan_frame, text="掃描PLC變數(i=85)", command=lambda: self.scan_preset("i=85")).grid(
            row=1, column=4, padx=4, pady=6
        )
        ttk.Button(scan_frame, text="掃描整個Server(i=84)", command=lambda: self.scan_preset("i=84")).grid(
            row=1, column=5, padx=(4, 8), pady=6
        )

        note_frame = ttk.Frame(self)
        note_frame.grid(row=3, column=0, sticky="ew", padx=8, pady=(2, 4))
        note_frame.columnconfigure(1, weight=1)
        ttk.Label(note_frame, text="重要提醒：i=85是Objects；i=84是Root；i=0不是根節點。").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(2, 4)
        )
        ttk.Label(note_frame, text="篩選搜尋：").grid(row=1, column=0, sticky="w", padx=(0, 4))
        self.filter_entry = ttk.Entry(note_frame, textvariable=self.filter_var)
        self.filter_entry.grid(row=1, column=1, sticky="ew")

        result_frame = ttk.LabelFrame(self, text="NodeId結果")
        result_frame.grid(row=4, column=0, sticky="nsew", padx=8, pady=4)
        result_frame.columnconfigure(0, weight=1)
        result_frame.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            result_frame,
            columns=self.TREE_COLUMNS,
            show="headings",
            selectmode="extended",
        )
        headings = {
            "display_name": "Display Name",
            "browse_name": "Browse Name",
            "node_id": "NodeId",
            "node_class": "Node Class",
            "data_type": "Data Type",
            "path": "Path",
        }
        widths = {
            "display_name": 180,
            "browse_name": 180,
            "node_id": 230,
            "node_class": 100,
            "data_type": 130,
            "path": 320,
        }
        for column in self.TREE_COLUMNS:
            self.tree.heading(column, text=headings[column], command=lambda c=column: self._sort_tree(c, False))
            self.tree.column(column, width=widths[column], minwidth=80, stretch=True)

        y_scroll = ttk.Scrollbar(result_frame, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(result_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")

        action_frame = ttk.Frame(self)
        action_frame.grid(row=5, column=0, sticky="ew", padx=8, pady=(4, 8))
        action_frame.columnconfigure(2, weight=1)
        ttk.Button(action_frame, text="加入監控並訂閱", command=self.add_selected_to_monitor).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(action_frame, text="清除結果", command=self.clear_results).grid(row=0, column=1, padx=4)
        ttk.Label(action_frame, textvariable=self.status_var).grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.progress = ttk.Progressbar(action_frame, mode="indeterminate", length=130)
        self.progress.grid(row=0, column=3, padx=(8, 0))

        self.context_menu = tk.Menu(self, tearoff=False)
        self.context_menu.add_command(label="讀取此Node", command=self.read_selected_node)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="加入監控並訂閱", command=self.add_selected_to_monitor)
        self.context_menu.add_command(label="訂閱此Node", command=self.subscribe_selected_node)
        self.context_menu.add_command(label="取消訂閱此Node", command=self.unsubscribe_selected_node)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="修改監控設定", command=self.edit_selected_monitor)
        self.context_menu.add_command(label="從監控清單刪除", command=self.delete_selected_monitor)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="複製NodeId", command=self.copy_selected_node_id)

    def _bind_events(self) -> None:
        self.server_combo.bind("<<ComboboxSelected>>", self._on_server_changed)
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.node_id_entry.bind("<Return>", lambda _event: self.browse_current_node())
        self.filter_entry.bind("<Return>", lambda _event: self.apply_filter())
        self.filter_var.trace_add("write", self._schedule_filter)

    def refresh(self) -> None:
        self.refresh_server_list()

    def refresh_server_list(self) -> None:
        try:
            config = self._get_config_snapshot()
            names = self._extract_server_names(config)
        except Exception as exc:
            self._report_error("讀取OPC UA Server設定失敗", exc)
            names = []

        previous = self.server_var.get().strip()
        self.server_combo["values"] = names
        if previous in names:
            self.server_var.set(previous)
        elif names:
            self.server_var.set(names[0])
        else:
            self.server_var.set("")
            self.status_var.set("config.json內尚無OPC UA Server設定")

    def browse_current_node(self, push_history: bool = True) -> None:
        server_name = self._require_server()
        if not server_name:
            return
        node_id = self.node_id_var.get().strip() or "i=85"

        previous_node = self._current_node_id
        if push_history and previous_node and node_id != previous_node:
            self._history.append(previous_node)

        self._set_busy(True, f"正在瀏覽{server_name}：{node_id}")
        self._run_in_background(
            lambda: self._call_manager("browse_node", server_name, node_id),
            on_success=lambda result, node_id=node_id: self._on_browse_success(node_id, result),
            on_error=lambda exc: self._on_operation_error("瀏覽Node失敗", exc),
        )

    def _on_browse_success(self, node_id: str, result: Any) -> None:
        rows = self._normalise_rows(result, parent_path=node_id)
        self._current_node_id = node_id
        self.node_id_var.set(node_id)
        self.current_path_var.set(f"目前NodeId：{node_id}")
        self._set_rows(rows)
        self._update_back_button()
        self._set_busy(False, f"瀏覽完成，共{len(rows)}個下一層節點")

    def go_back(self) -> None:
        if not self._history:
            return
        node_id = self._history.pop()
        self.node_id_var.set(node_id)
        self._update_back_button()
        self.browse_current_node(push_history=False)

    def scan_preset(self, start_node_id: str) -> None:
        self.scan_start_var.set(start_node_id)
        self.scan_custom()

    def scan_custom(self) -> None:
        server_name = self._require_server()
        if not server_name:
            return

        start_node_id = self.scan_start_var.get().strip() or "i=85"
        try:
            max_depth = int(self.max_depth_var.get())
            max_nodes = int(self.max_nodes_var.get())
            if max_depth < 1 or max_nodes < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("參數錯誤", "最大深度與最大節點數必須是正整數。", parent=self)
            return

        self._set_busy(True, f"正在掃描{server_name}：{start_node_id}")
        self._run_in_background(
            lambda: self._call_manager(
                "scan_all_nodes",
                server_name,
                start_node_id,
                max_depth,
                max_nodes,
                bool(self.only_variables_var.get()),
                bool(self.include_ns0_var.get()),
            ),
            on_success=lambda result: self._on_scan_success(start_node_id, result),
            on_error=lambda exc: self._on_operation_error("掃描Node失敗", exc),
        )

    def _on_scan_success(self, start_node_id: str, result: Any) -> None:
        rows = self._normalise_rows(result, parent_path=start_node_id)
        self._set_rows(rows)
        self._set_busy(False, f"掃描完成，共{len(rows)}個節點")

    def read_selected_node(self) -> None:
        row = self._selected_row()
        if not row:
            messagebox.showwarning("尚未選取", "請先選取Node。", parent=self)
            return
        server_name = self._require_server()
        if not server_name:
            return
        node_id = row.get("node_id", "")
        self._set_busy(True, f"正在讀取{node_id}")
        self._run_in_background(
            lambda: self._call_manager("read_node", server_name, node_id),
            on_success=lambda result: self._show_read_result(node_id, result),
            on_error=lambda exc: self._on_operation_error("讀取Node失敗", exc),
        )

    def _show_read_result(self, node_id: str, result: Any) -> None:
        self._set_busy(False, "讀取完成")
        messagebox.showinfo("讀取結果", f"NodeId：{node_id}\n\n值：{result}", parent=self)

    def add_selected_to_monitor(self) -> None:
        rows = self._selected_rows()
        if not rows:
            messagebox.showwarning("尚未選取", "請先選取要加入監控的Node。", parent=self)
            return
        server_name = self._require_server()
        if not server_name:
            return
        try:
            added = self._add_rows_to_config(server_name, rows, subscribe=True)
            self.status_var.set(f"已加入{added}個Node到監控設定")
            if callable(self.refresh_all):
                self.refresh_all()
        except Exception as exc:
            self._report_error("加入監控設定失敗", exc)

    def subscribe_selected_node(self) -> None:
        row = self._selected_row()
        if not row:
            messagebox.showwarning("尚未選取", "請先選取Node。", parent=self)
            return
        server_name = self._require_server()
        if not server_name:
            return
        node_config = self._row_to_node_config(row, subscribe=True)
        self._set_busy(True, f"正在訂閱{node_config['node_id']}")
        self._run_in_background(
            lambda: self._call_manager("subscribe_node", server_name, node_config),
            on_success=lambda _result: self._set_busy(False, "訂閱命令已完成"),
            on_error=lambda exc: self._on_operation_error("訂閱Node失敗", exc),
        )

    def unsubscribe_selected_node(self) -> None:
        row = self._selected_row()
        if not row:
            messagebox.showwarning("尚未選取", "請先選取Node。", parent=self)
            return
        server_name = self._require_server()
        if not server_name:
            return
        node_id = str(row.get("node_id", ""))
        self._set_busy(True, f"正在取消訂閱{node_id}")
        self._run_in_background(
            lambda: self._call_manager("unsubscribe_node", server_name, node_id),
            on_success=lambda _result: self._set_busy(False, "取消訂閱命令已完成"),
            on_error=lambda exc: self._on_operation_error("取消訂閱Node失敗", exc),
        )

    def edit_selected_monitor(self) -> None:
        messagebox.showinfo("提示", "此版本先保留Node瀏覽、掃描、加入監控與訂閱功能；詳細編輯請到OPC UA Server設定頁。", parent=self)

    def delete_selected_monitor(self) -> None:
        messagebox.showinfo("提示", "此版本先保留Node瀏覽、掃描、加入監控與訂閱功能；刪除監控請到OPC UA Server設定頁。", parent=self)

    def copy_selected_node_id(self) -> None:
        row = self._selected_row()
        if not row:
            return
        node_id = str(row.get("node_id", ""))
        self.clipboard_clear()
        self.clipboard_append(node_id)
        self.status_var.set(f"已複製NodeId：{node_id}")

    def clear_results(self) -> None:
        self._set_rows([])
        self.status_var.set("已清除結果")

    def apply_filter(self) -> None:
        keyword = self.filter_var.get().strip().lower()
        if not keyword:
            rows = list(self._all_rows)
        else:
            rows = [
                row
                for row in self._all_rows
                if keyword in " ".join(str(row.get(column, "")) for column in self.TREE_COLUMNS).lower()
            ]
        self._populate_tree(rows)

    def _schedule_filter(self, *_args: Any) -> None:
        if self._filter_after_id is not None:
            try:
                self.after_cancel(self._filter_after_id)
            except tk.TclError:
                pass
        try:
            self._filter_after_id = self.after(250, self.apply_filter)
        except tk.TclError:
            self._filter_after_id = None

    def _on_server_changed(self, _event: Any = None) -> None:
        self.status_var.set(f"目前Server：{self.server_var.get() or '-'}")

    def _on_tree_double_click(self, event: tk.Event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        row = self._row_by_item.get(iid)
        if row and row.get("node_id"):
            self.node_id_var.set(str(row["node_id"]))
            self.browse_current_node()

    def _show_context_menu(self, event: tk.Event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def _sort_tree(self, column: str, reverse: bool) -> None:
        rows = [(self.tree.set(iid, column), iid) for iid in self.tree.get_children("")]
        rows.sort(reverse=reverse)
        for index, (_value, iid) in enumerate(rows):
            self.tree.move(iid, "", index)
        self.tree.heading(column, command=lambda: self._sort_tree(column, not reverse))

    def _selected_rows(self) -> list[dict[str, Any]]:
        rows = []
        for iid in self.tree.selection():
            row = self._row_by_item.get(iid)
            if row:
                rows.append(row)
        return rows

    def _selected_row(self) -> dict[str, Any] | None:
        rows = self._selected_rows()
        return rows[0] if rows else None

    def _set_rows(self, rows: list[dict[str, Any]]) -> None:
        self._all_rows = list(rows)
        self.apply_filter()

    def _populate_tree(self, rows: list[dict[str, Any]]) -> None:
        self.tree.delete(*self.tree.get_children(""))
        self._row_by_item.clear()
        for index, row in enumerate(rows, start=1):
            iid = f"node_{index}"
            values = tuple(str(row.get(column, "")) for column in self.TREE_COLUMNS)
            self.tree.insert("", "end", iid=iid, values=values)
            self._row_by_item[iid] = row

    def _normalise_rows(self, result: Any, parent_path: str = "") -> list[dict[str, Any]]:
        if result is None:
            return []
        if isinstance(result, Mapping):
            for key in ("nodes", "children", "items", "rows", "result"):
                value = result.get(key)
                if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
                    result = value
                    break
            else:
                result = [result]

        rows: list[dict[str, Any]] = []
        if not isinstance(result, Iterable) or isinstance(result, (str, bytes)):
            return rows

        for item in result:
            if isinstance(item, Mapping):
                node_id = item.get("node_id", item.get("nodeId", item.get("nodeid", item.get("id", ""))))
                display_name = item.get("display_name", item.get("displayName", item.get("name", node_id)))
                browse_name = item.get("browse_name", item.get("browseName", display_name))
                node_class = item.get("node_class", item.get("nodeClass", item.get("class", "")))
                data_type = item.get("data_type", item.get("dataType", item.get("datatype", "")))
                path = item.get("path", parent_path)
            else:
                node_id = str(item)
                display_name = node_id
                browse_name = ""
                node_class = ""
                data_type = ""
                path = parent_path

            if not node_id:
                continue
            rows.append(
                {
                    "display_name": str(display_name or node_id),
                    "browse_name": str(browse_name or ""),
                    "node_id": str(node_id),
                    "node_class": str(node_class or ""),
                    "data_type": str(data_type or ""),
                    "path": str(path or parent_path),
                }
            )
        return rows

    def _require_server(self) -> str:
        server_name = self.server_var.get().strip()
        if not server_name:
            messagebox.showwarning("尚未選擇Server", "請先選擇OPC UA Server。", parent=self)
            return ""
        return server_name

    def _get_config_snapshot(self) -> dict[str, Any]:
        getter = getattr(self.config_manager, "get_config", None)
        if callable(getter):
            value = getter()
            if isinstance(value, dict):
                return copy.deepcopy(value)
        return copy.deepcopy(getattr(self.config_manager, "config", {}) or {})

    @staticmethod
    def _extract_server_names(config: Mapping[str, Any]) -> list[str]:
        opcua_config = config.get("opcua", {}) if isinstance(config, Mapping) else {}
        servers = opcua_config.get("servers", []) if isinstance(opcua_config, Mapping) else []
        names: list[str] = []
        for index, server in enumerate(servers):
            if not isinstance(server, Mapping):
                continue
            enabled = server.get("enable", server.get("enabled", True))
            if enabled is False:
                continue
            name = str(server.get("name", f"server_{index + 1}")).strip()
            if name:
                names.append(name)
        return names

    def _add_rows_to_config(self, server_name: str, rows: list[dict[str, Any]], subscribe: bool) -> int:
        with self._config_lock:
            config = self._get_config_snapshot()
            opcua_config = config.setdefault("opcua", {})
            servers = opcua_config.setdefault("servers", [])
            target_server: dict[str, Any] | None = None
            for server in servers:
                if isinstance(server, dict) and str(server.get("name", "")).strip() == server_name:
                    target_server = server
                    break
            if target_server is None:
                raise KeyError(f"找不到OPC UA Server設定：{server_name}")

            nodes = target_server.setdefault("nodes", [])
            existing = {
                str(node.get("node_id", node.get("nodeId", "")))
                for node in nodes
                if isinstance(node, Mapping)
            }

            added = 0
            for row in rows:
                node_config = self._row_to_node_config(row, subscribe=subscribe)
                node_id = str(node_config["node_id"])
                if node_id in existing:
                    continue
                nodes.append(node_config)
                existing.add(node_id)
                added += 1

            update_section = getattr(self.config_manager, "update_section", None)
            if callable(update_section):
                update_section("opcua", opcua_config)
            else:
                setattr(self.config_manager, "config", config)
                save_config = getattr(self.config_manager, "save_config", None)
                if callable(save_config):
                    save_config()
            return added

    @staticmethod
    def _row_to_node_config(row: Mapping[str, Any], subscribe: bool) -> dict[str, Any]:
        node_id = str(row.get("node_id", "")).strip()
        name = str(row.get("display_name", "") or row.get("browse_name", "") or node_id)
        data_type = str(row.get("data_type", "") or "Auto")
        return {
            "enable": True,
            "name": name,
            "node_id": node_id,
            "subscribe": bool(subscribe),
            "writable": False,
            "data_type": data_type,
            "db_enable": True,
        }

    def _call_manager(self, method_name: str, *args: Any) -> Any:
        if self.opcua_manager is None:
            raise RuntimeError("opcua_manager尚未建立。")
        method = getattr(self.opcua_manager, method_name, None)
        if not callable(method):
            raise RuntimeError(f"opcua_manager未提供{method_name}()。")

        result = method(*args)
        if isinstance(result, concurrent.futures.Future):
            return result.result(timeout=30.0)
        if inspect.isawaitable(result):
            return asyncio.run(result)
        return result

    def _run_in_background(
        self,
        work: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        if self._destroyed:
            return

        def runner() -> None:
            thread = threading.current_thread()
            try:
                if self._stop_event.is_set():
                    return
                result = work()
            except BaseException as exc:
                error = exc
                self._call_in_ui(lambda error=error: on_error(error) if on_error else None)
            else:
                self._call_in_ui(lambda result=result: on_success(result) if on_success else None)
            finally:
                with self._workers_lock:
                    self._workers.discard(thread)

        thread = threading.Thread(target=runner, name="OpcuaBrowsePageWorker", daemon=True)
        with self._workers_lock:
            self._workers.add(thread)
        thread.start()

    def _set_busy(self, busy: bool, text: str | None = None) -> None:
        if busy:
            self._busy_count += 1
            try:
                self.progress.start(10)
            except tk.TclError:
                pass
        else:
            self._busy_count = max(0, self._busy_count - 1)
            if self._busy_count == 0:
                try:
                    self.progress.stop()
                except tk.TclError:
                    pass
        if text:
            self.status_var.set(text)

    def _on_operation_error(self, title: str, exc: BaseException) -> None:
        self._set_busy(False, f"{title}：{exc}")
        self._report_error(title, exc)

    def _report_error(self, title: str, exc: BaseException) -> None:
        text = str(exc) or exc.__class__.__name__
        self._log(f"{title}：{text}", "ERROR")
        if not self._destroyed:
            messagebox.showerror(title, text, parent=self)

    def _log(self, message: str, level: str = "INFO") -> None:
        if callable(self.log_func):
            try:
                self.log_func(message, level)
            except TypeError:
                self.log_func(message)

    def _call_in_ui(self, callback: Callable[[], None]) -> None:
        if self._destroyed:
            return
        try:
            self.after(0, callback)
        except (tk.TclError, RuntimeError):
            pass

    def _update_back_button(self) -> None:
        try:
            self.back_button.configure(state="normal" if self._history else "disabled")
        except tk.TclError:
            pass

    def destroy(self) -> None:
        self._destroyed = True
        self._stop_event.set()
        if self._filter_after_id is not None:
            try:
                self.after_cancel(self._filter_after_id)
            except tk.TclError:
                pass
            self._filter_after_id = None
        # 背景thread為daemon，不在UI關閉時阻塞等待。
        super().destroy()
