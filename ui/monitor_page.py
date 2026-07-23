"""統一監控與點位讀寫頁面。"""

from __future__ import annotations

import concurrent.futures
import threading
import tkinter as tk
from collections.abc import Mapping
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Any


class MonitorPage(ttk.Frame):
    """顯示ValueBus中的所有最新PointValue並提供統一寫入功能。"""

    COLUMNS = ("protocol", "source_name", "device_name", "point_name", "address_text", "data_type", "writable", "value_text", "status_text", "timestamp")
    COLUMN_TITLES = {
        "protocol": "協定",
        "source_name": "來源名稱",
        "device_name": "設備名稱",
        "point_name": "點位名稱",
        "address_text": "位址／NodeId",
        "data_type": "資料型別",
        "writable": "可寫入",
        "value_text": "目前值",
        "status_text": "狀態",
        "timestamp": "更新時間",
    }

    def __init__(self, parent, app_context):
        super().__init__(parent)
        self.app_context = app_context
        self.value_bus = self._get_context_value("value_bus")
        self.modbus_manager = self._get_context_value("modbus_manager")
        self.modbus_tcp_manager = self._get_context_value("modbus_tcp_manager")
        self.opcua_manager = self._get_context_value("opcua_manager")
        self.log_func = self._get_context_value("log_func", lambda message: None)
        self._destroyed = False
        self._subscribed = False
        self._point_by_key: dict[str, Any] = {}
        self._iid_by_key: dict[str, str] = {}
        self._key_by_iid: dict[str, str] = {}
        self._iid_counter = 0
        self._pending_lock = threading.Lock()
        self._pending_points: dict[str, Any] = {}
        self._after_id: str | None = None
        self._write_lock = threading.Lock()
        self._write_worker: threading.Thread | None = None
        self.selected_point_var = tk.StringVar(value="尚未選取點位")
        self.selected_address_var = tk.StringVar(value="-")
        self.selected_value_var = tk.StringVar(value="-")
        self.write_value_var = tk.StringVar()
        self._build_ui()
        self._subscribe_value_bus()
        self.refresh_table()
        self._schedule_pending_update()

    def _get_context_value(self, name: str, default: Any = None) -> Any:
        if isinstance(self.app_context, Mapping):
            return self.app_context.get(name, default)
        return getattr(self.app_context, name, default)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        title_frame = ttk.Frame(self, padding=(8, 8, 8, 4))
        title_frame.grid(row=0, column=0, sticky="ew")
        title_frame.columnconfigure(0, weight=1)
        ttk.Label(title_frame, text="統一監控／讀寫", font=("", 14, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Button(title_frame, text="重新整理", command=self.refresh_table).grid(row=0, column=1, sticky="e")

        tree_frame = ttk.Frame(self, padding=(8, 4))
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(tree_frame, columns=self.COLUMNS, show="headings", selectmode="browse")
        widths = {"protocol": 110, "source_name": 130, "device_name": 130, "point_name": 150, "address_text": 240, "data_type": 100, "writable": 75, "value_text": 140, "status_text": 120, "timestamp": 165}
        for column in self.COLUMNS:
            self.tree.heading(column, text=self.COLUMN_TITLES[column])
            self.tree.column(column, width=widths[column], minwidth=60, anchor="center" if column in {"protocol", "data_type", "writable"} else "w")
        y_scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        x_scrollbar = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scrollbar.set, xscrollcommand=x_scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scrollbar.grid(row=0, column=1, sticky="ns")
        x_scrollbar.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Button-3>", self._show_context_menu)

        detail_frame = ttk.LabelFrame(self, text="點位讀寫", padding=10)
        detail_frame.grid(row=2, column=0, sticky="ew", padx=8, pady=(4, 8))
        detail_frame.columnconfigure(1, weight=1)
        detail_frame.columnconfigure(3, weight=1)
        ttk.Label(detail_frame, text="點位名稱：").grid(row=0, column=0, sticky="w")
        ttk.Label(detail_frame, textvariable=self.selected_point_var).grid(row=0, column=1, sticky="w")
        ttk.Label(detail_frame, text="位址／NodeId：").grid(row=0, column=2, sticky="w", padx=(16, 0))
        ttk.Label(detail_frame, textvariable=self.selected_address_var).grid(row=0, column=3, sticky="w")
        ttk.Label(detail_frame, text="目前值：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(detail_frame, textvariable=self.selected_value_var).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Label(detail_frame, text="寫入值：").grid(row=1, column=2, sticky="w", padx=(16, 0), pady=(6, 0))
        self.write_entry = ttk.Entry(detail_frame, textvariable=self.write_value_var)
        self.write_entry.grid(row=1, column=3, sticky="ew", pady=(6, 0))
        self.write_entry.bind("<Return>", lambda _event: self.write_selected_point())
        button_frame = ttk.Frame(detail_frame)
        button_frame.grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.write_button = ttk.Button(button_frame, text="寫入新值", command=self.write_selected_point)
        self.write_button.pack(side="left")
        ttk.Button(button_frame, text="寫入TRUE", command=lambda: self._quick_write("TRUE")).pack(side="left", padx=(6, 0))
        ttk.Button(button_frame, text="寫入FALSE", command=lambda: self._quick_write("FALSE")).pack(side="left", padx=(6, 0))
        ttk.Button(button_frame, text="填入目前值", command=self.fill_current_value).pack(side="left", padx=(6, 0))
        ttk.Button(button_frame, text="重新整理", command=self.refresh_table).pack(side="left", padx=(6, 0))
        self.context_menu = tk.Menu(self, tearoff=False)
        self.context_menu.add_command(label="寫入此點位", command=self.write_selected_point)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="複製point_key", command=self._copy_point_key)
        self.context_menu.add_command(label="複製位址／NodeId", command=self._copy_address)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="重新整理", command=self.refresh_table)

    def _subscribe_value_bus(self) -> None:
        if self.value_bus is None:
            self._log("MonitorPage找不到ValueBus，無法訂閱資料。")
            return
        subscribe = getattr(self.value_bus, "subscribe", None)
        if not callable(subscribe):
            self._log("ValueBus未提供subscribe(callback)介面。")
            return
        try:
            subscribe(self._on_point_value)
            self._subscribed = True
        except Exception as exc:
            self._log(f"訂閱ValueBus失敗：{exc}")

    def _unsubscribe_value_bus(self) -> None:
        if not self._subscribed or self.value_bus is None:
            return
        unsubscribe = getattr(self.value_bus, "unsubscribe", None)
        if callable(unsubscribe):
            try:
                unsubscribe(self._on_point_value)
            except Exception as exc:
                self._log(f"取消訂閱ValueBus失敗：{exc}")
        self._subscribed = False

    def _on_point_value(self, point_value: Any) -> None:
        if self._destroyed:
            return
        point_key = self._point_key(point_value)
        if not point_key:
            return
        with self._pending_lock:
            self._pending_points[point_key] = point_value

    def _schedule_pending_update(self) -> None:
        if self._destroyed:
            return
        try:
            self._after_id = self.after(100, self._flush_pending_points)
        except (tk.TclError, RuntimeError):
            self._after_id = None

    def _flush_pending_points(self) -> None:
        if self._destroyed:
            return
        with self._pending_lock:
            pending_points = list(self._pending_points.values())
            self._pending_points.clear()
        selected_key = self._selected_point_key()
        for point_value in pending_points:
            self._upsert_point(point_value)
        if selected_key:
            self._restore_selection(selected_key)
            point_value = self._point_by_key.get(selected_key)
            if point_value is not None:
                self._show_point_details(point_value)
        self._schedule_pending_update()

    def refresh(self) -> None:
        self.refresh_table()

    def refresh_data(self) -> None:
        self.refresh_table()

    def reload_data(self) -> None:
        self.refresh_table()

    def update_view(self) -> None:
        self.refresh_table()

    def refresh_table(self) -> None:
        selected_key = self._selected_point_key()
        latest_points = self._get_latest_points()
        latest_keys: set[str] = set()
        for point_value in latest_points:
            point_key = self._point_key(point_value)
            if not point_key:
                continue
            latest_keys.add(point_key)
            self._upsert_point(point_value)
        for point_key in list(self._point_by_key):
            if point_key not in latest_keys:
                self._remove_point(point_key)
        if selected_key and selected_key in self._point_by_key:
            self._restore_selection(selected_key)
            self._show_point_details(self._point_by_key[selected_key])
        elif not self.tree.selection():
            self._clear_point_details()

    def _get_latest_points(self) -> list[Any]:
        if self.value_bus is None:
            return []
        get_latest_list = getattr(self.value_bus, "get_latest_list", None)
        if callable(get_latest_list):
            try:
                result = get_latest_list()
                if result is not None:
                    return list(result)
            except Exception as exc:
                self._log(f"讀取ValueBus清單失敗：{exc}")
        get_latest_dict = getattr(self.value_bus, "get_latest_dict", None)
        if callable(get_latest_dict):
            try:
                result = get_latest_dict()
                if isinstance(result, Mapping):
                    return list(result.values())
            except Exception as exc:
                self._log(f"讀取ValueBus字典失敗：{exc}")
        return []

    def _upsert_point(self, point_value: Any) -> None:
        point_key = self._point_key(point_value)
        if not point_key:
            return
        self._point_by_key[point_key] = point_value
        values = self._tree_values(point_value)
        iid = self._iid_by_key.get(point_key)
        if iid and self.tree.exists(iid):
            self.tree.item(iid, values=values)
            return
        self._iid_counter += 1
        iid = f"point_{self._iid_counter}"
        self.tree.insert("", "end", iid=iid, values=values)
        self._iid_by_key[point_key] = iid
        self._key_by_iid[iid] = point_key

    def _remove_point(self, point_key: str) -> None:
        iid = self._iid_by_key.pop(point_key, None)
        self._point_by_key.pop(point_key, None)
        if iid:
            self._key_by_iid.pop(iid, None)
            if self.tree.exists(iid):
                self.tree.delete(iid)

    def _tree_values(self, point_value: Any) -> tuple[str, ...]:
        return (
            self._text(self._field(point_value, "protocol")),
            self._text(self._field(point_value, "source_name")),
            self._text(self._field(point_value, "device_name")),
            self._text(self._field(point_value, "point_name")),
            self._text(self._field(point_value, "address_text")),
            self._text(self._field(point_value, "data_type")),
            "是" if self._as_bool(self._field(point_value, "writable", False)) else "否",
            self._text(self._field(point_value, "value_text", self._field(point_value, "value"))),
            self._text(self._field(point_value, "status_text")),
            self._format_timestamp(self._field(point_value, "timestamp")),
        )

    def _on_tree_select(self, _event: Any = None) -> None:
        point_value = self._selected_point()
        if point_value is None:
            self._clear_point_details()
            return
        self._show_point_details(point_value)

    def _on_tree_double_click(self, event: tk.Event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self._on_tree_select()
        self.fill_current_value()

    def _show_context_menu(self, event: tk.Event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self._on_tree_select()
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def _selected_point_key(self) -> str:
        selection = self.tree.selection()
        if not selection:
            return ""
        return self._key_by_iid.get(selection[0], "")

    def _selected_point(self) -> Any | None:
        key = self._selected_point_key()
        if not key:
            return None
        return self._point_by_key.get(key)

    def _restore_selection(self, point_key: str) -> None:
        iid = self._iid_by_key.get(point_key)
        if iid and self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.focus(iid)

    def _show_point_details(self, point_value: Any) -> None:
        self.selected_point_var.set(self._text(self._field(point_value, "point_name")) or "未命名點位")
        self.selected_address_var.set(self._text(self._field(point_value, "address_text")) or "-")
        self.selected_value_var.set(self._text(self._field(point_value, "value_text", self._field(point_value, "value"))) or "-")

    def _clear_point_details(self) -> None:
        self.selected_point_var.set("尚未選取點位")
        self.selected_address_var.set("-")
        self.selected_value_var.set("-")

    def fill_current_value(self) -> None:
        point_value = self._selected_point()
        if point_value is None:
            return
        value = self._field(point_value, "value_text", self._field(point_value, "value"))
        self.write_value_var.set(self._text(value))

    def _quick_write(self, value_text: str) -> None:
        self.write_value_var.set(value_text)
        self.write_selected_point()

    def write_selected_point(self) -> None:
        point_value = self._selected_point()
        if point_value is None:
            messagebox.showwarning("尚未選取", "請先選取要寫入的點位。", parent=self)
            return
        if not self._as_bool(self._field(point_value, "writable", False)):
            messagebox.showwarning("不可寫入", "此點位設定為不可寫入。", parent=self)
            return
        value_text = self.write_value_var.get().strip()
        if value_text == "":
            messagebox.showwarning("缺少寫入值", "請輸入要寫入的值。", parent=self)
            return
        protocol = self._text(self._field(point_value, "protocol")).upper()
        point_name = self._text(self._field(point_value, "point_name")) or "未命名點位"
        address_text = self._text(self._field(point_value, "address_text")) or "-"
        confirm_text = "確定要寫入此點位嗎？\n\n" f"協定：{protocol}\n" f"點位：{point_name}\n" f"位址／NodeId：{address_text}\n" f"新值：{value_text}"
        if not messagebox.askyesno("確認寫入", confirm_text, parent=self):
            return
        if protocol == "OPCUA":
            self._start_background_write(point_value, value_text, point_name)
            return
        try:
            if protocol == "MODBUS_RTU":
                result = self._write_modbus(point_value, value_text)
            elif protocol == "MODBUS_TCP":
                result = self._write_modbus_tcp(point_value, value_text)
            else:
                raise ValueError(f"不支援的協定：{protocol or '未指定'}")
        except Exception as exc:
            self._log(f"點位寫入失敗：{exc}")
            messagebox.showerror("寫入失敗", str(exc), parent=self)
            return
        if result is False:
            messagebox.showerror("寫入失敗", "通訊管理器回報寫入失敗，請查看系統紀錄。", parent=self)
            return
        self._log(f"{protocol}寫入成功：{point_name} = {value_text}")
        messagebox.showinfo("寫入完成", "寫入成功。", parent=self)

    def _start_background_write(self, point_value: Any, value_text: str, point_name: str) -> None:
        with self._write_lock:
            if self._write_worker and self._write_worker.is_alive():
                messagebox.showwarning("寫入中", "已有一筆OPC UA寫入尚未完成，請稍後再試。", parent=self)
                return
            self._set_write_busy(True)
            self._write_worker = threading.Thread(target=self._opcua_write_worker, args=(point_value, value_text, point_name), name="MonitorOpcuaWrite", daemon=True)
            self._write_worker.start()

    def _opcua_write_worker(self, point_value: Any, value_text: str, point_name: str) -> None:
        error_text = ""
        success = False
        try:
            result = self._write_opcua(point_value, value_text)
            result = self._resolve_write_result(result, timeout=20.0)
            if result is False:
                raise RuntimeError("通訊管理器回報寫入失敗。")
            success = True
        except concurrent.futures.TimeoutError:
            error_text = "OPC UA寫入等待逾時，請確認Server連線與Node權限。"
        except Exception as exc:
            error_text = str(exc) or exc.__class__.__name__

        def finish() -> None:
            if self._destroyed:
                return
            self._set_write_busy(False)
            if success:
                self._log(f"OPC UA寫入成功：{point_name} = {value_text}")
                messagebox.showinfo("寫入完成", "OPC UA寫入已完成。", parent=self)
            else:
                self._log(f"OPC UA寫入失敗：{point_name} = {value_text}；{error_text}")
                messagebox.showerror("寫入失敗", error_text, parent=self)
        self._call_in_ui(finish)

    def _resolve_write_result(self, result: Any, timeout: float) -> Any:
        if isinstance(result, concurrent.futures.Future):
            return result.result(timeout=timeout)
        return result

    def _set_write_busy(self, busy: bool) -> None:
        try:
            self.write_button.configure(state="disabled" if busy else "normal")
        except Exception:
            pass

    def _write_modbus(self, point_value: Any, value_text: str) -> Any:
        if self.modbus_manager is None:
            raise RuntimeError("modbus_manager尚未建立。")
        write_point = getattr(self.modbus_manager, "write_point", None)
        if not callable(write_point):
            raise RuntimeError("modbus_manager未提供write_point()。")
        point_key = self._point_key(point_value)
        if not point_key:
            raise ValueError("此MODBUS_RTU點位缺少point_key。")
        return write_point(point_key, value_text)

    def _write_modbus_tcp(self, point_value: Any, value_text: str) -> Any:
        if self.modbus_tcp_manager is None:
            raise RuntimeError("modbus_tcp_manager尚未建立。")
        write_point = getattr(self.modbus_tcp_manager, "write_point", None)
        if not callable(write_point):
            raise RuntimeError("modbus_tcp_manager未提供write_point()。")
        point_key = self._point_key(point_value)
        if not point_key:
            raise ValueError("此MODBUS_TCP點位缺少point_key。")
        return write_point(point_key, value_text)

    def _write_opcua(self, point_value: Any, value_text: str) -> Any:
        if self.opcua_manager is None:
            raise RuntimeError("opcua_manager尚未建立。")
        write_node = getattr(self.opcua_manager, "write_node", None)
        if not callable(write_node):
            raise RuntimeError("opcua_manager未提供write_node()。")
        raw_config = self._field(point_value, "raw_config", {})
        if not isinstance(raw_config, Mapping):
            raw_config = {}
        server_name = self._first_nonempty(raw_config.get("server_name"), self._field(point_value, "source_name"), self._field(point_value, "device_name"))
        node_id = self._first_nonempty(raw_config.get("node_id"), raw_config.get("nodeId"), self._field(point_value, "address_text"))
        data_type = self._first_nonempty(self._field(point_value, "data_type"), raw_config.get("data_type"), "Auto")
        if not server_name:
            raise ValueError("此OPCUA點位缺少server_name。")
        if not node_id:
            raise ValueError("此OPCUA點位缺少NodeId。")
        return write_node(str(server_name), str(node_id), value_text, str(data_type))

    def _copy_point_key(self) -> None:
        point_value = self._selected_point()
        if point_value is not None:
            self._copy_to_clipboard(self._point_key(point_value))

    def _copy_address(self) -> None:
        point_value = self._selected_point()
        if point_value is not None:
            self._copy_to_clipboard(self._text(self._field(point_value, "address_text")))

    def _copy_to_clipboard(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self._log(f"已複製：{text}")

    @staticmethod
    def _field(point_value: Any, name: str, default: Any = None) -> Any:
        if isinstance(point_value, Mapping):
            return point_value.get(name, default)
        return getattr(point_value, name, default)

    @staticmethod
    def _point_key(point_value: Any) -> str:
        if isinstance(point_value, Mapping):
            return str(point_value.get("point_key", "") or "")
        return str(getattr(point_value, "point_key", "") or "")

    @staticmethod
    def _text(value: Any) -> str:
        return "" if value is None else str(value)

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on", "是"}
        return bool(value)

    @staticmethod
    def _first_nonempty(*values: Any) -> Any:
        for value in values:
            if value is None:
                continue
            if isinstance(value, str) and value.strip() == "":
                continue
            return value
        return None

    @staticmethod
    def _format_timestamp(value: Any) -> str:
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return "" if value is None else str(value)

    def _log(self, message: str) -> None:
        try:
            self.log_func(message)
        except TypeError:
            self.log_func(message, "INFO")
        except Exception:
            pass

    def _call_in_ui(self, callback) -> None:
        if self._destroyed:
            return
        try:
            self.after(0, callback)
        except (tk.TclError, RuntimeError):
            pass

    def destroy(self) -> None:
        self._destroyed = True
        self._unsubscribe_value_bus()
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        super().destroy()
