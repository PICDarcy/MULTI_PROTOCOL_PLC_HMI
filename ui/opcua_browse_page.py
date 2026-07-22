"""OPC UA NodeId瀏覽與掃描頁面。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import inspect
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import tkinter as tk
from tkinter import messagebox, ttk


class OpcuaBrowsePage(ttk.Frame):
    """提供多台OPC UA Server的節點瀏覽、掃描與監控設定功能。"""

    TREE_COLUMNS: Tuple[str, ...] = (
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

        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="opcua-browse-ui",
        )
        self._destroyed = False
        self._busy_count = 0
        self._all_rows: List[Dict[str, Any]] = []
        self._history: List[str] = []
        self._current_node_id = "i=85"
        self._row_by_item: Dict[str, Dict[str, Any]] = {}
        self._filter_after_id: Optional[str] = None
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

    # ------------------------------------------------------------------
    # UI建立
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        server_frame = ttk.LabelFrame(self, text="OPC UA Server")
        server_frame.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        server_frame.columnconfigure(1, weight=1)

        ttk.Label(server_frame, text="Server：").grid(
            row=0, column=0, sticky="w", padx=(8, 4), pady=8
        )
        self.server_combo = ttk.Combobox(
            server_frame,
            textvariable=self.server_var,
            state="readonly",
        )
        self.server_combo.grid(row=0, column=1, sticky="ew", padx=4, pady=8)
        ttk.Button(
            server_frame,
            text="重新載入Server",
            command=self.refresh_server_list,
        ).grid(row=0, column=2, padx=(4, 8), pady=8)

        browse_frame = ttk.LabelFrame(self, text="逐層瀏覽")
        browse_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        browse_frame.columnconfigure(1, weight=1)

        ttk.Label(browse_frame, text="NodeId：").grid(
            row=0, column=0, sticky="w", padx=(8, 4), pady=6
        )
        self.node_id_entry = ttk.Entry(browse_frame, textvariable=self.node_id_var)
        self.node_id_entry.grid(row=0, column=1, sticky="ew", padx=4, pady=6)
        self.back_button = ttk.Button(
            browse_frame,
            text="上一頁",
            command=self.go_back,
            state="disabled",
        )
        self.back_button.grid(row=0, column=2, padx=4, pady=6)
        ttk.Button(
            browse_frame,
            text="瀏覽下一層",
            command=self.browse_current_node,
        ).grid(row=0, column=3, padx=(4, 8), pady=6)

        ttk.Label(
            browse_frame,
            textvariable=self.current_path_var,
        ).grid(row=1, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 6))

        scan_frame = ttk.LabelFrame(self, text="遞迴掃描")
        scan_frame.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        scan_frame.columnconfigure(1, weight=1)

        ttk.Label(scan_frame, text="掃描起點：").grid(
            row=0, column=0, sticky="w", padx=(8, 4), pady=6
        )
        ttk.Entry(scan_frame, textvariable=self.scan_start_var).grid(
            row=0, column=1, sticky="ew", padx=4, pady=6
        )
        ttk.Label(scan_frame, text="最大深度：").grid(
            row=0, column=2, sticky="e", padx=(8, 4), pady=6
        )
        ttk.Spinbox(
            scan_frame,
            from_=1,
            to=100,
            textvariable=self.max_depth_var,
            width=7,
        ).grid(row=0, column=3, sticky="w", padx=4, pady=6)
        ttk.Label(scan_frame, text="最大節點數：").grid(
            row=0, column=4, sticky="e", padx=(8, 4), pady=6
        )
        ttk.Spinbox(
            scan_frame,
            from_=1,
            to=100000,
            textvariable=self.max_nodes_var,
            width=9,
        ).grid(row=0, column=5, sticky="w", padx=4, pady=6)

        ttk.Checkbutton(
            scan_frame,
            text="只顯示Variable",
            variable=self.only_variables_var,
        ).grid(row=1, column=0, sticky="w", padx=8, pady=6)
        ttk.Checkbutton(
            scan_frame,
            text="包含ns=0",
            variable=self.include_ns0_var,
        ).grid(row=1, column=1, sticky="w", padx=4, pady=6)
        ttk.Button(
            scan_frame,
            text="依設定掃描",
            command=self.scan_custom,
        ).grid(row=1, column=3, padx=4, pady=6)
        ttk.Button(
            scan_frame,
            text="掃描PLC變數(i=85)",
            command=lambda: self.scan_preset("i=85"),
        ).grid(row=1, column=4, padx=4, pady=6)
        ttk.Button(
            scan_frame,
            text="掃描整個Server(i=84)",
            command=lambda: self.scan_preset("i=84"),
        ).grid(row=1, column=5, padx=(4, 8), pady=6)

        note_frame = ttk.Frame(self)
        note_frame.grid(row=3, column=0, sticky="ew", padx=8, pady=(2, 4))
        note_frame.columnconfigure(1, weight=1)
        ttk.Label(
            note_frame,
            text="重要提醒：i=85是Objects；i=84是Root；i=0不是根節點。",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(2, 4))
        ttk.Label(note_frame, text="篩選搜尋：").grid(
            row=1, column=0, sticky="w", padx=(0, 4)
        )
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
            "node_id": 220,
            "node_class": 100,
            "data_type": 130,
            "path": 320,
        }
        for column in self.TREE_COLUMNS:
            self.tree.heading(
                column,
                text=headings[column],
                command=lambda c=column: self._sort_tree(c, False),
            )
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
        ttk.Button(
            action_frame,
            text="加入監控並訂閱",
            command=self.add_selected_to_monitor,
        ).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(
            action_frame,
            text="清除結果",
            command=self.clear_results,
        ).grid(row=0, column=1, padx=4)
        ttk.Label(action_frame, textvariable=self.status_var).grid(
            row=0, column=2, sticky="e", padx=(8, 0)
        )
        self.progress = ttk.Progressbar(action_frame, mode="indeterminate", length=130)
        self.progress.grid(row=0, column=3, padx=(8, 0))

        self.context_menu = tk.Menu(self, tearoff=False)
        self.context_menu.add_command(label="讀取此Node", command=self.read_selected_node)
        self.context_menu.add_separator()
        self.context_menu.add_command(
            label="加入監控並訂閱",
            command=self.add_selected_to_monitor,
        )
        self.context_menu.add_command(label="訂閱此Node", command=self.subscribe_selected_node)
        self.context_menu.add_command(
            label="取消訂閱此Node",
            command=self.unsubscribe_selected_node,
        )
        self.context_menu.add_separator()
        self.context_menu.add_command(
            label="修改監控設定",
            command=self.edit_selected_monitor,
        )
        self.context_menu.add_command(
            label="從監控清單刪除",
            command=self.delete_selected_monitor,
        )
        self.context_menu.add_separator()
        self.context_menu.add_command(label="複製NodeId", command=self.copy_selected_node_id)

    def _bind_events(self) -> None:
        self.server_combo.bind("<<ComboboxSelected>>", self._on_server_changed)
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.node_id_entry.bind("<Return>", lambda _event: self.browse_current_node())
        self.filter_entry.bind("<Return>", lambda _event: self.apply_filter())
        self.filter_var.trace_add("write", self._schedule_filter)
        self.bind("<Destroy>", self._on_destroy, add="+")

    # ------------------------------------------------------------------
    # 對外更新介面
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """供app_context.refresh_all或頁面切換時呼叫。"""
        self.refresh_server_list()

    def refresh_server_list(self) -> None:
        """重新讀取config.json內的OPC UA Server名稱。"""
        try:
            config = self._get_config_snapshot()
            names = self._extract_server_names(config)
        except Exception as exc:  # noqa: BLE001
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

    # ------------------------------------------------------------------
    # 瀏覽與掃描
    # ------------------------------------------------------------------
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
            on_success=lambda result: self._on_browse_success(node_id, result),
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
            messagebox.showwarning("掃描參數錯誤", "最大深度與最大節點數必須是大於0的整數。")
            return

        only_variables = bool(self.only_variables_var.get())
        include_ns0 = bool(self.include_ns0_var.get())
        self._set_busy(True, f"正在掃描{server_name}：{start_node_id}")
        self._run_in_background(
            lambda: self._call_manager(
                "scan_all_nodes",
                server_name,
                start_node_id,
                max_depth,
                max_nodes,
                only_variables,
                include_ns0,
            ),
            on_success=lambda result: self._on_scan_success(start_node_id, result),
            on_error=lambda exc: self._on_operation_error("掃描Node失敗", exc),
        )

    def _on_scan_success(self, start_node_id: str, result: Any) -> None:
        rows = self._normalise_rows(result, parent_path=start_node_id)
        self.current_path_var.set(f"最近掃描起點：{start_node_id}")
        self._set_rows(rows)
        self._set_busy(False, f"掃描完成，共取得{len(rows)}個節點")

    # ------------------------------------------------------------------
    # Treeview與篩選
    # ------------------------------------------------------------------
    def _set_rows(self, rows: Sequence[Dict[str, Any]]) -> None:
        self._all_rows = [dict(row) for row in rows]
        self.apply_filter()

    def clear_results(self) -> None:
        self._all_rows.clear()
        self._render_rows([])
        self.status_var.set("已清除結果")

    def _schedule_filter(self, *_args: Any) -> None:
        if self._filter_after_id:
            try:
                self.after_cancel(self._filter_after_id)
            except tk.TclError:
                pass
        self._filter_after_id = self.after(180, self.apply_filter)

    def apply_filter(self) -> None:
        self._filter_after_id = None
        keyword = self.filter_var.get().strip().lower()
        if not keyword:
            rows = self._all_rows
        else:
            rows = [
                row
                for row in self._all_rows
                if any(keyword in str(row.get(column, "")).lower() for column in self.TREE_COLUMNS)
            ]
        self._render_rows(rows)
        if keyword:
            self.status_var.set(f"篩選結果：{len(rows)}/{len(self._all_rows)}")

    def _render_rows(self, rows: Iterable[Dict[str, Any]]) -> None:
        for item in self.tree.get_children(""):
            self.tree.delete(item)
        self._row_by_item.clear()

        for row in rows:
            values = tuple(row.get(column, "") for column in self.TREE_COLUMNS)
            item = self.tree.insert("", "end", values=values)
            self._row_by_item[item] = row

    def _sort_tree(self, column: str, descending: bool) -> None:
        rows = list(self._all_rows)
        rows.sort(
            key=lambda row: str(row.get(column, "")).casefold(),
            reverse=descending,
        )
        self._all_rows = rows
        self.apply_filter()
        self.tree.heading(
            column,
            command=lambda: self._sort_tree(column, not descending),
        )

    def _on_tree_double_click(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.tree.selection_set(item)
        row = self._row_by_item.get(item)
        if not row:
            return
        node_id = str(row.get("node_id", "")).strip()
        if not node_id:
            return
        if self._current_node_id and node_id != self._current_node_id:
            self._history.append(self._current_node_id)
        self.node_id_var.set(node_id)
        self._update_back_button()
        self.browse_current_node(push_history=False)

    def _show_context_menu(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        if item:
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            try:
                self.context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.context_menu.grab_release()

    def _selected_rows(self) -> List[Dict[str, Any]]:
        return [
            self._row_by_item[item]
            for item in self.tree.selection()
            if item in self._row_by_item
        ]

    def _single_selected_row(self, show_warning: bool = True) -> Optional[Dict[str, Any]]:
        rows = self._selected_rows()
        if not rows:
            if show_warning:
                messagebox.showinfo("尚未選取", "請先選取一個Node。")
            return None
        return rows[0]

    # ------------------------------------------------------------------
    # Node操作
    # ------------------------------------------------------------------
    def read_selected_node(self) -> None:
        row = self._single_selected_row()
        server_name = self._require_server()
        if not row or not server_name:
            return
        node_id = str(row.get("node_id", ""))
        self._set_busy(True, f"正在讀取{node_id}")
        self._run_in_background(
            lambda: self._call_manager("read_node", server_name, node_id),
            on_success=lambda value: self._show_read_result(node_id, value),
            on_error=lambda exc: self._on_operation_error("讀取Node失敗", exc),
        )

    def _show_read_result(self, node_id: str, value: Any) -> None:
        self._set_busy(False, f"讀取完成：{node_id}")
        if isinstance(value, (dict, list, tuple)):
            text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        else:
            text = str(value)
        messagebox.showinfo("Node讀取結果", f"NodeId：{node_id}\n\n值：\n{text}")

    def add_selected_to_monitor(self) -> None:
        rows = self._selected_rows()
        server_name = self._require_server()
        if not rows or not server_name:
            if not rows:
                messagebox.showinfo("尚未選取", "請先選取至少一個Node。")
            return

        self._set_busy(True, f"正在加入{len(rows)}個Node至監控清單")
        self._run_in_background(
            lambda: self._add_rows_to_monitor_worker(server_name, rows),
            on_success=self._on_add_monitor_success,
            on_error=lambda exc: self._on_operation_error("加入監控並訂閱失敗", exc),
        )

    def _add_rows_to_monitor_worker(
        self,
        server_name: str,
        rows: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        with self._config_lock:
            config = self._get_config_snapshot()
            server_config = self._find_server_config(config, server_name)
            if server_config is None:
                raise KeyError(f"config.json找不到Server：{server_name}")

            nodes = server_config.setdefault("nodes", [])
            if not isinstance(nodes, list):
                raise TypeError(f"Server {server_name}的nodes設定必須是清單。")

            existing_by_id = {
                str(node.get("node_id", "")): node
                for node in nodes
                if isinstance(node, dict)
            }
            added = 0
            updated = 0
            node_configs: List[Dict[str, Any]] = []

            for row in rows:
                node_config = self._make_node_config(row)
                node_id = node_config["node_id"]
                existing = existing_by_id.get(node_id)
                if existing is None:
                    nodes.append(node_config)
                    existing_by_id[node_id] = node_config
                    added += 1
                else:
                    existing.update(node_config)
                    node_config = existing
                    updated += 1
                node_configs.append(copy.deepcopy(node_config))

            self._save_config_snapshot(config)

        subscribe_errors: List[str] = []
        for node_config in node_configs:
            try:
                self._call_manager("subscribe_node", server_name, node_config)
            except Exception as exc:  # noqa: BLE001
                subscribe_errors.append(f"{node_config.get('node_id')}: {exc}")

        return {
            "added": added,
            "updated": updated,
            "subscribed": len(node_configs) - len(subscribe_errors),
            "errors": subscribe_errors,
        }

    def _on_add_monitor_success(self, result: Dict[str, Any]) -> None:
        errors = result.get("errors", [])
        summary = (
            f"新增{result.get('added', 0)}個、更新{result.get('updated', 0)}個，"
            f"成功訂閱{result.get('subscribed', 0)}個。"
        )
        self._set_busy(False, summary)
        self._log(summary)
        self._invoke_refresh_all()
        if errors:
            messagebox.showwarning(
                "部分訂閱失敗",
                summary + "\n\n" + "\n".join(str(error) for error in errors),
            )
        else:
            messagebox.showinfo("完成", summary)

    def subscribe_selected_node(self) -> None:
        row = self._single_selected_row()
        server_name = self._require_server()
        if not row or not server_name:
            return
        node_config = self._existing_or_new_node_config(server_name, row)
        self._set_busy(True, f"正在訂閱{node_config['node_id']}")
        self._run_in_background(
            lambda: self._call_manager("subscribe_node", server_name, node_config),
            on_success=lambda _result: self._set_busy(
                False,
                f"已訂閱{node_config['node_id']}",
            ),
            on_error=lambda exc: self._on_operation_error("訂閱Node失敗", exc),
        )

    def unsubscribe_selected_node(self) -> None:
        row = self._single_selected_row()
        server_name = self._require_server()
        if not row or not server_name:
            return
        node_id = str(row.get("node_id", ""))
        self._set_busy(True, f"正在取消訂閱{node_id}")
        self._run_in_background(
            lambda: self._call_manager("unsubscribe_node", server_name, node_id),
            on_success=lambda _result: self._set_busy(False, f"已取消訂閱{node_id}"),
            on_error=lambda exc: self._on_operation_error("取消訂閱Node失敗", exc),
        )

    def edit_selected_monitor(self) -> None:
        row = self._single_selected_row()
        server_name = self._require_server()
        if not row or not server_name:
            return

        try:
            config = self._get_config_snapshot()
            server_config = self._find_server_config(config, server_name)
            node_config = self._find_node_config(server_config, str(row.get("node_id", "")))
        except Exception as exc:  # noqa: BLE001
            self._report_error("讀取監控設定失敗", exc)
            return

        if node_config is None:
            messagebox.showinfo("找不到監控設定", "此Node尚未加入監控清單。")
            return
        self._open_monitor_editor(server_name, node_config)

    def _open_monitor_editor(self, server_name: str, node_config: Dict[str, Any]) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("修改OPC UA監控設定")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        dialog.resizable(False, False)

        point_name_var = tk.StringVar(value=str(node_config.get("point_name", "")))
        data_type_var = tk.StringVar(value=str(node_config.get("data_type", "Auto")))
        writable_var = tk.BooleanVar(value=bool(node_config.get("writable", False)))
        subscribe_var = tk.BooleanVar(value=bool(node_config.get("subscribe", True)))

        ttk.Label(dialog, text="NodeId：").grid(row=0, column=0, sticky="e", padx=8, pady=6)
        ttk.Label(dialog, text=str(node_config.get("node_id", ""))).grid(
            row=0, column=1, sticky="w", padx=8, pady=6
        )
        ttk.Label(dialog, text="Point名稱：").grid(row=1, column=0, sticky="e", padx=8, pady=6)
        ttk.Entry(dialog, textvariable=point_name_var, width=42).grid(
            row=1, column=1, sticky="ew", padx=8, pady=6
        )
        ttk.Label(dialog, text="資料型別：").grid(row=2, column=0, sticky="e", padx=8, pady=6)
        ttk.Combobox(
            dialog,
            textvariable=data_type_var,
            values=(
                "Auto",
                "Boolean",
                "SByte",
                "Byte",
                "Int16",
                "UInt16",
                "Int32",
                "UInt32",
                "Int64",
                "UInt64",
                "Float",
                "Double",
                "String",
                "DateTime",
            ),
            width=39,
        ).grid(row=2, column=1, sticky="ew", padx=8, pady=6)
        ttk.Checkbutton(dialog, text="允許寫入", variable=writable_var).grid(
            row=3, column=1, sticky="w", padx=8, pady=4
        )
        ttk.Checkbutton(dialog, text="啟用訂閱", variable=subscribe_var).grid(
            row=4, column=1, sticky="w", padx=8, pady=4
        )

        button_frame = ttk.Frame(dialog)
        button_frame.grid(row=5, column=0, columnspan=2, sticky="e", padx=8, pady=10)
        ttk.Button(button_frame, text="取消", command=dialog.destroy).pack(side="right", padx=(4, 0))

        def save_changes() -> None:
            updates = {
                "point_name": point_name_var.get().strip() or str(node_config.get("display_name", "")),
                "data_type": data_type_var.get().strip() or "Auto",
                "writable": bool(writable_var.get()),
                "subscribe": bool(subscribe_var.get()),
            }
            dialog.destroy()
            self._save_monitor_edits(server_name, str(node_config.get("node_id", "")), updates)

        ttk.Button(button_frame, text="儲存", command=save_changes).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.wait_visibility()
        dialog.focus_set()

    def _save_monitor_edits(
        self,
        server_name: str,
        node_id: str,
        updates: Dict[str, Any],
    ) -> None:
        self._set_busy(True, f"正在更新監控設定：{node_id}")

        def worker() -> Dict[str, Any]:
            with self._config_lock:
                config = self._get_config_snapshot()
                server_config = self._find_server_config(config, server_name)
                node_config = self._find_node_config(server_config, node_id)
                if node_config is None:
                    raise KeyError(f"監控清單找不到NodeId：{node_id}")
                node_config.update(updates)
                saved_node = copy.deepcopy(node_config)
                self._save_config_snapshot(config)

            if saved_node.get("subscribe", True):
                self._call_manager("subscribe_node", server_name, saved_node)
            else:
                self._call_manager("unsubscribe_node", server_name, node_id)
            return saved_node

        self._run_in_background(
            worker,
            on_success=lambda _result: self._on_monitor_edit_success(node_id),
            on_error=lambda exc: self._on_operation_error("更新監控設定失敗", exc),
        )

    def _on_monitor_edit_success(self, node_id: str) -> None:
        self._set_busy(False, f"已更新監控設定：{node_id}")
        self._invoke_refresh_all()

    def delete_selected_monitor(self) -> None:
        row = self._single_selected_row()
        server_name = self._require_server()
        if not row or not server_name:
            return
        node_id = str(row.get("node_id", ""))
        if not messagebox.askyesno(
            "刪除監控設定",
            f"確定要從{server_name}的監控清單刪除以下Node嗎？\n\n{node_id}",
        ):
            return

        self._set_busy(True, f"正在刪除監控設定：{node_id}")

        def worker() -> bool:
            with self._config_lock:
                config = self._get_config_snapshot()
                server_config = self._find_server_config(config, server_name)
                if server_config is None:
                    raise KeyError(f"config.json找不到Server：{server_name}")
                nodes = server_config.get("nodes", [])
                if not isinstance(nodes, list):
                    raise TypeError("nodes設定必須是清單。")
                original_count = len(nodes)
                server_config["nodes"] = [
                    node
                    for node in nodes
                    if not (
                        isinstance(node, dict)
                        and str(node.get("node_id", "")) == node_id
                    )
                ]
                removed = len(server_config["nodes"]) != original_count
                if removed:
                    self._save_config_snapshot(config)
            try:
                self._call_manager("unsubscribe_node", server_name, node_id)
            except Exception as exc:  # noqa: BLE001
                self._log(f"刪除設定後取消訂閱失敗：{exc}", "WARNING")
            return removed

        self._run_in_background(
            worker,
            on_success=lambda removed: self._on_delete_monitor_success(node_id, removed),
            on_error=lambda exc: self._on_operation_error("刪除監控設定失敗", exc),
        )

    def _on_delete_monitor_success(self, node_id: str, removed: bool) -> None:
        if removed:
            text = f"已從監控清單刪除：{node_id}"
            self._invoke_refresh_all()
        else:
            text = f"監控清單內原本沒有此Node：{node_id}"
        self._set_busy(False, text)

    def copy_selected_node_id(self) -> None:
        row = self._single_selected_row()
        if not row:
            return
        node_id = str(row.get("node_id", ""))
        self.clipboard_clear()
        self.clipboard_append(node_id)
        self.status_var.set(f"已複製NodeId：{node_id}")

    # ------------------------------------------------------------------
    # 非同步執行：所有Future/Coroutine都在背景執行緒等待
    # ------------------------------------------------------------------
    def _run_in_background(
        self,
        operation: Callable[[], Any],
        on_success: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        if self._destroyed:
            return

        future = self._executor.submit(self._execute_operation, operation)

        def completed(done_future: concurrent.futures.Future) -> None:
            try:
                result = done_future.result()
            except BaseException as exc:  # noqa: BLE001
                self._safe_after(lambda: on_error(exc) if on_error else self._report_error("背景工作失敗", exc))
            else:
                if on_success:
                    self._safe_after(lambda: on_success(result))

        future.add_done_callback(completed)

    def _execute_operation(self, operation: Callable[[], Any]) -> Any:
        result = operation()
        return self._resolve_async_result(result)

    def _resolve_async_result(self, result: Any) -> Any:
        """在背景執行緒解析Future或awaitable，絕不阻塞Tkinter主執行緒。"""
        if isinstance(result, concurrent.futures.Future):
            return result.result()

        if inspect.iscoroutine(result):
            return asyncio.run(result)

        if isinstance(result, asyncio.Future):
            if result.done():
                return result.result()
            loop = result.get_loop()
            if loop.is_running():
                async def wait_future() -> Any:
                    return await result

                return asyncio.run_coroutine_threadsafe(wait_future(), loop).result()
            return loop.run_until_complete(result)

        if inspect.isawaitable(result):
            async def wait_awaitable() -> Any:
                return await result

            return asyncio.run(wait_awaitable())

        if hasattr(result, "result") and callable(result.result):
            try:
                return result.result()
            except TypeError:
                return result
        return result

    def _safe_after(self, callback: Callable[[], None]) -> None:
        if self._destroyed:
            return
        try:
            self.after(0, callback)
        except (tk.TclError, RuntimeError):
            pass

    # ------------------------------------------------------------------
    # Manager與config.json相容層
    # ------------------------------------------------------------------
    def _call_manager(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        if self.opcua_manager is None:
            raise RuntimeError("app_context未提供opcua_manager。")
        method = getattr(self.opcua_manager, method_name, None)
        if not callable(method):
            raise AttributeError(f"opcua_manager未提供{method_name}()。")
        result = method(*args, **kwargs)
        return self._resolve_async_result(result)

    def _get_config_snapshot(self) -> Dict[str, Any]:
        manager = self.config_manager
        if manager is None:
            return self._read_config_file_fallback()

        for method_name in ("get_config", "get_all", "load_config", "load"):
            method = getattr(manager, method_name, None)
            if callable(method):
                try:
                    result = method()
                except TypeError:
                    continue
                result = self._resolve_async_result(result)
                if isinstance(result, dict):
                    return copy.deepcopy(result)

        for attr_name in ("config", "data", "config_data"):
            value = getattr(manager, attr_name, None)
            if isinstance(value, dict):
                return copy.deepcopy(value)

        getter = getattr(manager, "get", None)
        if callable(getter):
            try:
                root = getter()
                if isinstance(root, dict):
                    return copy.deepcopy(root)
            except TypeError:
                pass

        return self._read_config_file_fallback()

    def _save_config_snapshot(self, config: Dict[str, Any]) -> None:
        manager = self.config_manager
        last_error: Optional[BaseException] = None

        if manager is not None:
            for method_name in ("save_config", "save", "write_config", "update_config"):
                method = getattr(manager, method_name, None)
                if not callable(method):
                    continue
                try:
                    result = method(copy.deepcopy(config))
                    self._resolve_async_result(result)
                    return
                except TypeError:
                    try:
                        self._assign_manager_config(manager, config)
                        result = method()
                        self._resolve_async_result(result)
                        return
                    except Exception as exc:  # noqa: BLE001
                        last_error = exc
                except Exception as exc:  # noqa: BLE001
                    last_error = exc

            setter = getattr(manager, "set_config", None)
            saver = getattr(manager, "save_config", None) or getattr(manager, "save", None)
            if callable(setter) and callable(saver):
                try:
                    self._resolve_async_result(setter(copy.deepcopy(config)))
                    self._resolve_async_result(saver())
                    return
                except Exception as exc:  # noqa: BLE001
                    last_error = exc

        try:
            self._write_config_file_fallback(config)
        except Exception:
            if last_error is not None:
                raise RuntimeError(f"ConfigManager儲存失敗：{last_error}") from last_error
            raise

    @staticmethod
    def _assign_manager_config(manager: Any, config: Dict[str, Any]) -> None:
        for attr_name in ("config", "data", "config_data"):
            if hasattr(manager, attr_name):
                setattr(manager, attr_name, copy.deepcopy(config))
                return
        raise AttributeError("ConfigManager沒有可寫入的config/data屬性。")

    def _config_path(self) -> Path:
        manager = self.config_manager
        if manager is not None:
            for attr_name in ("config_path", "path", "file_path", "filename"):
                value = getattr(manager, attr_name, None)
                if value:
                    return Path(value)
        return Path(__file__).resolve().parent.parent / "config.json"

    def _read_config_file_fallback(self) -> Dict[str, Any]:
        path = self._config_path()
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise TypeError("config.json最外層必須是JSON物件。")
        return data

    def _write_config_file_fallback(self, config: Dict[str, Any]) -> None:
        path = self._config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(config, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temp_path, path)

    def _extract_server_names(self, config: Dict[str, Any]) -> List[str]:
        servers = self._server_container(config)
        names: List[str] = []
        if isinstance(servers, list):
            for item in servers:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("server_name")
                    if name:
                        names.append(str(name))
        elif isinstance(servers, dict):
            names.extend(str(name) for name in servers.keys())
        return sorted(dict.fromkeys(names), key=str.casefold)

    def _server_container(self, config: Dict[str, Any]) -> Any:
        candidates = [
            config.get("opcua_servers"),
            config.get("OPCUA_SERVERS"),
        ]
        for key in ("opcua", "OPCUA"):
            section = config.get(key)
            if isinstance(section, dict):
                candidates.extend((section.get("servers"), section.get("server_list")))
        protocols = config.get("protocols")
        if isinstance(protocols, dict):
            section = protocols.get("opcua") or protocols.get("OPCUA")
            if isinstance(section, dict):
                candidates.append(section.get("servers"))
        candidates.append(config.get("servers"))

        for candidate in candidates:
            if isinstance(candidate, (list, dict)):
                return candidate
        return []

    def _find_server_config(
        self,
        config: Dict[str, Any],
        server_name: str,
    ) -> Optional[Dict[str, Any]]:
        servers = self._server_container(config)
        if isinstance(servers, list):
            for item in servers:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("server_name")
                if str(name) == server_name:
                    return item
        elif isinstance(servers, dict):
            item = servers.get(server_name)
            if isinstance(item, dict):
                item.setdefault("name", server_name)
                return item
        return None

    @staticmethod
    def _find_node_config(
        server_config: Optional[Dict[str, Any]],
        node_id: str,
    ) -> Optional[Dict[str, Any]]:
        if not server_config:
            return None
        nodes = server_config.get("nodes", [])
        if not isinstance(nodes, list):
            return None
        for node in nodes:
            if isinstance(node, dict) and str(node.get("node_id", "")) == node_id:
                return node
        return None

    def _existing_or_new_node_config(
        self,
        server_name: str,
        row: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            config = self._get_config_snapshot()
            server_config = self._find_server_config(config, server_name)
            existing = self._find_node_config(server_config, str(row.get("node_id", "")))
            if existing:
                return copy.deepcopy(existing)
        except Exception as exc:  # noqa: BLE001
            self._log(f"讀取既有Node設定失敗，改用掃描結果：{exc}", "WARNING")
        return self._make_node_config(row)

    def _make_node_config(self, row: Dict[str, Any]) -> Dict[str, Any]:
        display_name = str(row.get("display_name", "")).strip()
        browse_name = str(row.get("browse_name", "")).strip()
        node_id = str(row.get("node_id", "")).strip()
        data_type = self._infer_data_type(row)
        return {
            "point_name": display_name or browse_name or node_id,
            "node_id": node_id,
            "display_name": display_name,
            "browse_name": browse_name,
            "path": str(row.get("path", "")),
            "node_class": str(row.get("node_class", "")),
            "data_type": data_type,
            "subscribe": True,
            "writable": False,
        }

    # ------------------------------------------------------------------
    # 結果正規化與型別推測
    # ------------------------------------------------------------------
    def _normalise_rows(self, result: Any, parent_path: str = "") -> List[Dict[str, Any]]:
        if result is None:
            return []
        if isinstance(result, dict):
            for key in ("nodes", "results", "items", "children"):
                value = result.get(key)
                if isinstance(value, (list, tuple)):
                    result = value
                    break
            else:
                result = [result]
        elif not isinstance(result, (list, tuple, set)):
            try:
                result = list(result)
            except TypeError:
                result = [result]

        rows: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in result:
            row = self._normalise_row(item, parent_path)
            node_id = row["node_id"]
            unique_key = node_id or json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            if unique_key in seen:
                continue
            seen.add(unique_key)
            rows.append(row)
        return rows

    def _normalise_row(self, item: Any, parent_path: str) -> Dict[str, Any]:
        def value(*names: str, default: Any = "") -> Any:
            if isinstance(item, dict):
                for name in names:
                    if name in item and item[name] is not None:
                        return item[name]
            else:
                for name in names:
                    if hasattr(item, name):
                        attr = getattr(item, name)
                        if attr is not None:
                            return attr
            return default

        display_name = self._text_value(value("display_name", "DisplayName", "name"))
        browse_name = self._text_value(value("browse_name", "BrowseName"))
        node_id = self._node_id_text(value("node_id", "nodeid", "NodeId", "id"))
        node_class = self._text_value(value("node_class", "NodeClass", "class_name"))
        data_type_value = value(
            "data_type",
            "DataType",
            "data_type_name",
            "variant_type",
            "built_in_type",
            default="",
        )
        path = self._text_value(value("path", "browse_path", "full_path"))
        if not path:
            child_name = display_name or browse_name or node_id
            path = f"{parent_path}/{child_name}" if parent_path else child_name

        row = {
            "display_name": display_name,
            "browse_name": browse_name,
            "node_id": node_id,
            "node_class": node_class,
            "data_type": self._normalise_data_type(data_type_value),
            "path": path,
        }
        raw_value = value("value", "Value", default=None)
        if raw_value is not None:
            row["value"] = raw_value
        row["data_type"] = self._infer_data_type(row)
        return row

    @staticmethod
    def _text_value(value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "Text"):
            value = value.Text
        if hasattr(value, "Name") and not isinstance(value, str):
            namespace = getattr(value, "NamespaceIndex", None)
            name = getattr(value, "Name", "")
            if namespace not in (None, 0, "0"):
                return f"{namespace}:{name}"
            return str(name)
        return str(value)

    @staticmethod
    def _node_id_text(value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "to_string") and callable(value.to_string):
            try:
                return str(value.to_string())
            except Exception:  # noqa: BLE001
                pass
        return str(value)

    def _infer_data_type(self, row: Dict[str, Any]) -> str:
        current = self._normalise_data_type(row.get("data_type", ""))
        if current and current not in {"Auto", "Unknown", "None"}:
            return current

        value = row.get("value")
        if isinstance(value, bool):
            return "Boolean"
        if isinstance(value, int):
            return "Int64"
        if isinstance(value, float):
            return "Double"
        if isinstance(value, str):
            return "String"
        if isinstance(value, (bytes, bytearray)):
            return "ByteString"
        return "Auto"

    @staticmethod
    def _normalise_data_type(value: Any) -> str:
        if value is None:
            return "Auto"
        text = str(value).strip()
        if not text:
            return "Auto"
        lowered = text.lower().replace("varianttype.", "").replace("builtintype.", "")
        aliases = {
            "bool": "Boolean",
            "boolean": "Boolean",
            "sbyte": "SByte",
            "byte": "Byte",
            "int16": "Int16",
            "uint16": "UInt16",
            "int32": "Int32",
            "uint32": "UInt32",
            "int64": "Int64",
            "uint64": "UInt64",
            "float": "Float",
            "double": "Double",
            "string": "String",
            "datetime": "DateTime",
            "bytestring": "ByteString",
            "localizedtext": "LocalizedText",
            "guid": "Guid",
        }
        compact = lowered.replace("_", "").replace(" ", "")
        return aliases.get(compact, text)

    # ------------------------------------------------------------------
    # 共用輔助
    # ------------------------------------------------------------------
    def _context_get(self, key: str, default: Any = None) -> Any:
        if isinstance(self.app_context, dict):
            return self.app_context.get(key, default)
        return getattr(self.app_context, key, default)

    def _require_server(self) -> Optional[str]:
        server_name = self.server_var.get().strip()
        if not server_name:
            messagebox.showwarning("未選擇Server", "請先選擇一台OPC UA Server。")
            return None
        return server_name

    def _on_server_changed(self, _event: Optional[tk.Event] = None) -> None:
        self._history.clear()
        self._current_node_id = "i=85"
        self.node_id_var.set("i=85")
        self.scan_start_var.set("i=85")
        self.current_path_var.set("目前NodeId：i=85")
        self._update_back_button()
        self.clear_results()

    def _update_back_button(self) -> None:
        self.back_button.configure(state="normal" if self._history else "disabled")

    def _set_busy(self, busy: bool, text: str) -> None:
        if busy:
            self._busy_count += 1
            if self._busy_count == 1:
                self.progress.start(12)
        else:
            self._busy_count = max(0, self._busy_count - 1)
            if self._busy_count == 0:
                self.progress.stop()
        self.status_var.set(text)
        self._log(text)

    def _on_operation_error(self, title: str, exc: BaseException) -> None:
        self._set_busy(False, f"{title}：{exc}")
        self._report_error(title, exc)

    def _report_error(self, title: str, exc: BaseException) -> None:
        text = f"{title}：{exc}"
        self._log(text, "ERROR")
        messagebox.showerror(title, str(exc))

    def _log(self, message: str, level: str = "INFO") -> None:
        if not callable(self.log_func):
            return
        try:
            self.log_func(message, level)
        except TypeError:
            try:
                self.log_func(f"[{level}] {message}")
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    def _invoke_refresh_all(self) -> None:
        if not callable(self.refresh_all):
            return
        try:
            self.refresh_all()
        except Exception as exc:  # noqa: BLE001
            self._log(f"refresh_all執行失敗：{exc}", "WARNING")

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is not self or self._destroyed:
            return
        self._destroyed = True
        if self._filter_after_id:
            try:
                self.after_cancel(self._filter_after_id)
            except tk.TclError:
                pass
        self._executor.shutdown(wait=False, cancel_futures=True)
