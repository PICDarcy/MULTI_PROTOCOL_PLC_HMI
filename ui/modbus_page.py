"""Modbus RTU設定頁。

本頁面只透過config_manager與modbus_manager公開介面工作，不直接操作
SerialClient或任何序列埠客戶端。
"""

from __future__ import annotations

import copy
import inspect
import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import tkinter as tk
from tkinter import messagebox, ttk


POINT_TYPES = (
    "holding_register",
    "input_register",
    "coil",
    "discrete_input",
)
READ_ONLY_TYPES = {"input_register", "discrete_input"}
DATA_TYPES = (
    "Auto",
    "bool",
    "uint16",
    "int16",
    "uint32",
    "int32",
    "uint64",
    "int64",
    "float32",
    "float64",
    "string",
    "raw",
)

DEFAULT_CONFIG = {
    "enable": False,
    "port": "COM1",
    "baudrate": 9600,
    "bytesize": 8,
    "parity": "N",
    "stopbits": 1,
    "timeout": 1.0,
    "poll_interval": 1.0,
    "devices": [],
}
DEFAULT_DEVICE = {"enable": True, "name": "PLC_1", "station_id": 1, "points": []}
DEFAULT_POINT = {
    "enable": True,
    "name": "Point_1",
    "type": "holding_register",
    "address": 0,
    "count": 1,
    "data_type": "uint16",
    "writable": False,
    "db_enable": False,
}


class ModbusPage(ttk.Frame):
    """Modbus RTU通訊、PLC裝置與點位設定頁。"""

    def __init__(self, parent, app_context):
        super().__init__(parent)
        self.app_context = app_context
        self.config_manager = self._ctx("config_manager")
        self.modbus_manager = self._ctx("modbus_manager")
        self.log_func = self._ctx("log_func", print)
        self.refresh_all = self._ctx("refresh_all")

        self.config: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self.selected_device: Optional[int] = None
        self.action_running = False
        self.status_after_id: Optional[str] = None

        self.enable_var = tk.BooleanVar(value=False)
        self.port_var = tk.StringVar(value="COM1")
        self.baudrate_var = tk.StringVar(value="9600")
        self.bytesize_var = tk.StringVar(value="8")
        self.parity_var = tk.StringVar(value="N")
        self.stopbits_var = tk.StringVar(value="1")
        self.timeout_var = tk.StringVar(value="1.0")
        self.poll_interval_var = tk.StringVar(value="1.0")
        self.status_var = tk.StringVar(value="尚未載入設定")
        self.running_var = tk.StringVar(value="輪詢狀態：未知")

        self._build_ui()
        self.reload_settings(show_message=False, reload_manager=False)
        self._poll_running_status()

    # UI -----------------------------------------------------------------
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        title = ttk.Frame(self, padding=(10, 10, 10, 4))
        title.grid(row=0, column=0, sticky="ew")
        title.columnconfigure(0, weight=1)
        ttk.Label(title, text="Modbus RTU設定", font=("TkDefaultFont", 14, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(title, textvariable=self.running_var).grid(row=0, column=1, sticky="e")

        self._build_serial_frame()
        self._build_tree_area()
        self._build_action_bar()

    def _build_serial_frame(self) -> None:
        frame = ttk.LabelFrame(self, text="通訊參數", padding=10)
        frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(4, 8))
        for column in (1, 3, 5, 7):
            frame.columnconfigure(column, weight=1)

        ttk.Checkbutton(frame, text="啟用Modbus RTU", variable=self.enable_var).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=4
        )
        self._entry(frame, 0, 2, "序列埠", self.port_var)
        self._combo(
            frame,
            0,
            4,
            "鮑率",
            self.baudrate_var,
            ("1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"),
            "normal",
        )
        self._combo(frame, 0, 6, "資料位元", self.bytesize_var, ("5", "6", "7", "8"))
        self._combo(frame, 1, 0, "同位元", self.parity_var, ("N", "E", "O", "M", "S"))
        self._combo(frame, 1, 2, "停止位元", self.stopbits_var, ("1", "1.5", "2"))
        self._entry(frame, 1, 4, "逾時秒數", self.timeout_var)
        self._entry(frame, 1, 6, "輪詢間隔秒數", self.poll_interval_var)

    @staticmethod
    def _entry(parent, row, column, label, variable) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="e", padx=(8, 4), pady=4)
        ttk.Entry(parent, textvariable=variable, width=14).grid(
            row=row, column=column + 1, sticky="ew", padx=(0, 8), pady=4
        )

    @staticmethod
    def _combo(parent, row, column, label, variable, values, state="readonly") -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="e", padx=(8, 4), pady=4)
        ttk.Combobox(
            parent,
            textvariable=variable,
            values=values,
            state=state,
            width=12,
        ).grid(row=row, column=column + 1, sticky="ew", padx=(0, 8), pady=4)

    def _build_tree_area(self) -> None:
        paned = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        paned.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 8))

        devices = ttk.LabelFrame(paned, text="PLC裝置", padding=8)
        points = ttk.LabelFrame(paned, text="選取PLC的點位", padding=8)
        paned.add(devices, weight=1)
        paned.add(points, weight=2)
        for frame in (devices, points):
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)

        self.device_tree = ttk.Treeview(
            devices,
            columns=("enable", "name", "station_id"),
            show="headings",
            selectmode="browse",
        )
        for column, text, width in (
            ("enable", "啟用", 55),
            ("name", "PLC名稱", 180),
            ("station_id", "站號", 65),
        ):
            self.device_tree.heading(column, text=text)
            self.device_tree.column(column, width=width, anchor=tk.CENTER if column != "name" else tk.W)
        self.device_tree.grid(row=0, column=0, sticky="nsew")
        dscroll = ttk.Scrollbar(devices, command=self.device_tree.yview)
        dscroll.grid(row=0, column=1, sticky="ns")
        self.device_tree.configure(yscrollcommand=dscroll.set)
        self.device_tree.bind("<<TreeviewSelect>>", self._device_selected)
        self.device_tree.bind("<Double-1>", lambda _event: self.edit_device())

        dbuttons = ttk.Frame(devices)
        dbuttons.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(dbuttons, text="新增PLC", command=self.add_device).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(dbuttons, text="修改PLC", command=self.edit_device).pack(side=tk.LEFT, padx=5)
        ttk.Button(dbuttons, text="刪除PLC", command=self.delete_device).pack(side=tk.LEFT, padx=5)

        point_columns = (
            "enable",
            "name",
            "type",
            "address",
            "count",
            "data_type",
            "writable",
            "db_enable",
        )
        self.point_tree = ttk.Treeview(
            points,
            columns=point_columns,
            show="headings",
            selectmode="browse",
        )
        headings = {
            "enable": "啟用",
            "name": "點位名稱",
            "type": "類型",
            "address": "位址",
            "count": "數量",
            "data_type": "資料型別",
            "writable": "可寫入",
            "db_enable": "寫入資料庫",
        }
        widths = {
            "enable": 55,
            "name": 145,
            "type": 135,
            "address": 65,
            "count": 60,
            "data_type": 90,
            "writable": 70,
            "db_enable": 90,
        }
        for column in point_columns:
            self.point_tree.heading(column, text=headings[column])
            self.point_tree.column(
                column,
                width=widths[column],
                anchor=tk.W if column in ("name", "type") else tk.CENTER,
            )
        self.point_tree.grid(row=0, column=0, sticky="nsew")
        pyscroll = ttk.Scrollbar(points, command=self.point_tree.yview)
        pyscroll.grid(row=0, column=1, sticky="ns")
        pxscroll = ttk.Scrollbar(points, orient=tk.HORIZONTAL, command=self.point_tree.xview)
        pxscroll.grid(row=1, column=0, sticky="ew")
        self.point_tree.configure(yscrollcommand=pyscroll.set, xscrollcommand=pxscroll.set)
        self.point_tree.bind("<Double-1>", lambda _event: self.edit_point())

        pbuttons = ttk.Frame(points)
        pbuttons.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(pbuttons, text="新增點位", command=self.add_point).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(pbuttons, text="修改點位", command=self.edit_point).pack(side=tk.LEFT, padx=5)
        ttk.Button(pbuttons, text="刪除點位", command=self.delete_point).pack(side=tk.LEFT, padx=5)

    def _build_action_bar(self) -> None:
        frame = ttk.Frame(self, padding=(10, 0, 10, 10))
        frame.grid(row=3, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, textvariable=self.status_var).grid(row=0, column=0, sticky="w")

        buttons = ttk.Frame(frame)
        buttons.grid(row=0, column=1, sticky="e")
        for text, command in (
            ("儲存設定", self.save_settings),
            ("重新載入設定", self.reload_settings),
            ("啟動Modbus輪詢", self.start_polling),
            ("停止Modbus輪詢", self.stop_polling),
            ("讀取一次", self.read_all_once),
        ):
            ttk.Button(buttons, text=text, command=command).pack(side=tk.LEFT, padx=4)

    # 裝置與點位管理 -----------------------------------------------------
    def add_device(self) -> None:
        used_ids = {self._to_int(item.get("station_id"), -1) for item in self.config["devices"]}
        station_id = next((number for number in range(1, 248) if number not in used_ids), 1)
        initial = copy.deepcopy(DEFAULT_DEVICE)
        initial["name"] = self._unique_name("PLC", self.config["devices"])
        initial["station_id"] = station_id
        result = _DeviceDialog(self, "新增PLC", initial).show()
        if result is None or not self._device_is_unique(result):
            return
        self.config["devices"].append(result)
        self.selected_device = len(self.config["devices"]) - 1
        self._refresh_devices()
        self._status(f"已新增PLC：{result['name']}，尚未儲存")

    def edit_device(self) -> None:
        index = self._selected_device_index()
        if index is None:
            messagebox.showinfo("修改PLC", "請先選取要修改的PLC。", parent=self)
            return
        result = _DeviceDialog(self, "修改PLC", self.config["devices"][index]).show()
        if result is None or not self._device_is_unique(result, index):
            return
        self.config["devices"][index] = result
        self.selected_device = index
        self._refresh_devices()
        self._status(f"已修改PLC：{result['name']}，尚未儲存")

    def delete_device(self) -> None:
        index = self._selected_device_index()
        if index is None:
            messagebox.showinfo("刪除PLC", "請先選取要刪除的PLC。", parent=self)
            return
        name = self.config["devices"][index]["name"]
        if not messagebox.askyesno(
            "刪除PLC",
            f"確定刪除PLC「{name}」及其所有點位嗎？",
            parent=self,
        ):
            return
        self.config["devices"].pop(index)
        self.selected_device = min(index, len(self.config["devices"]) - 1) if self.config["devices"] else None
        self._refresh_devices()
        self._status(f"已刪除PLC：{name}，尚未儲存")

    def add_point(self) -> None:
        device_index = self._selected_device_index()
        if device_index is None:
            messagebox.showinfo("新增點位", "請先選取一個PLC。", parent=self)
            return
        points = self.config["devices"][device_index]["points"]
        initial = copy.deepcopy(DEFAULT_POINT)
        initial["name"] = self._unique_name("Point", points)
        result = _PointDialog(self, "新增點位", initial).show()
        if result is None or not self._point_is_unique(device_index, result):
            return
        points.append(result)
        self._refresh_points(len(points) - 1)
        self._status(f"已新增點位：{result['name']}，尚未儲存")

    def edit_point(self) -> None:
        device_index = self._selected_device_index()
        point_index = self._selected_point_index()
        if device_index is None or point_index is None:
            messagebox.showinfo("修改點位", "請先選取要修改的點位。", parent=self)
            return
        points = self.config["devices"][device_index]["points"]
        result = _PointDialog(self, "修改點位", points[point_index]).show()
        if result is None or not self._point_is_unique(device_index, result, point_index):
            return
        points[point_index] = result
        self._refresh_points(point_index)
        self._status(f"已修改點位：{result['name']}，尚未儲存")

    def delete_point(self) -> None:
        device_index = self._selected_device_index()
        point_index = self._selected_point_index()
        if device_index is None or point_index is None:
            messagebox.showinfo("刪除點位", "請先選取要刪除的點位。", parent=self)
            return
        points = self.config["devices"][device_index]["points"]
        name = points[point_index]["name"]
        if not messagebox.askyesno("刪除點位", f"確定刪除點位「{name}」嗎？", parent=self):
            return
        points.pop(point_index)
        self._refresh_points(min(point_index, len(points) - 1) if points else None)
        self._status(f"已刪除點位：{name}，尚未儲存")

    # 設定讀寫 -----------------------------------------------------------
    def save_settings(self) -> None:
        try:
            self._form_to_config()
            self._validate_config()
            self._save_config_json()
            if self.modbus_manager is None:
                raise RuntimeError("app_context未提供modbus_manager。")
            self.modbus_manager.reload_config()
            self._call_refresh_all()
            self._status("Modbus RTU設定已儲存")
            self._log("INFO", "已儲存Modbus RTU設定並呼叫modbus_manager.reload_config()")
            messagebox.showinfo("儲存完成", "設定已寫入config.json並重新載入。", parent=self)
        except Exception as exc:  # noqa: BLE001
            self._status(f"儲存失敗：{exc}")
            self._log("ERROR", f"儲存Modbus RTU設定失敗：{exc}")
            messagebox.showerror("儲存失敗", str(exc), parent=self)

    def reload_settings(self, show_message: bool = True, reload_manager: bool = True) -> None:
        try:
            self._reload_config_manager()
            self.config = self._normalize(self._read_modbus_section())
            self.selected_device = 0 if self.config["devices"] else None
            self._config_to_form()
            self._refresh_devices()
            if reload_manager and self.modbus_manager is not None:
                self.modbus_manager.reload_config()
            self._call_refresh_all()
            self._status("已重新載入Modbus RTU設定")
            if show_message:
                messagebox.showinfo("重新載入", "Modbus RTU設定已重新載入。", parent=self)
        except Exception as exc:  # noqa: BLE001
            self._status(f"重新載入失敗：{exc}")
            self._log("ERROR", f"重新載入Modbus RTU設定失敗：{exc}")
            if show_message:
                messagebox.showerror("重新載入失敗", str(exc), parent=self)

    def _form_to_config(self) -> None:
        port = self.port_var.get().strip()
        if not port:
            raise ValueError("序列埠不可為空白。")
        baudrate = self._positive_int(self.baudrate_var.get(), "鮑率")
        bytesize = self._positive_int(self.bytesize_var.get(), "資料位元")
        if bytesize not in (5, 6, 7, 8):
            raise ValueError("資料位元只能是5、6、7或8。")
        parity = self.parity_var.get().strip().upper()
        if parity not in ("N", "E", "O", "M", "S"):
            raise ValueError("同位元只能是N、E、O、M或S。")
        stopbits = float(self.stopbits_var.get())
        if stopbits not in (1.0, 1.5, 2.0):
            raise ValueError("停止位元只能是1、1.5或2。")
        timeout = self._positive_float(self.timeout_var.get(), "逾時秒數")
        poll_interval = self._positive_float(self.poll_interval_var.get(), "輪詢間隔秒數")
        self.config.update(
            {
                "enable": bool(self.enable_var.get()),
                "port": port,
                "baudrate": baudrate,
                "bytesize": bytesize,
                "parity": parity,
                "stopbits": int(stopbits) if stopbits.is_integer() else stopbits,
                "timeout": timeout,
                "poll_interval": poll_interval,
            }
        )

    def _config_to_form(self) -> None:
        self.enable_var.set(bool(self.config["enable"]))
        self.port_var.set(str(self.config["port"]))
        self.baudrate_var.set(str(self.config["baudrate"]))
        self.bytesize_var.set(str(self.config["bytesize"]))
        self.parity_var.set(str(self.config["parity"]).upper())
        self.stopbits_var.set(str(self.config["stopbits"]))
        self.timeout_var.set(str(self.config["timeout"]))
        self.poll_interval_var.set(str(self.config["poll_interval"]))

    def _read_modbus_section(self) -> Dict[str, Any]:
        manager = self.config_manager
        if manager is not None:
            for method_name in ("get_section", "get"):
                method = getattr(manager, method_name, None)
                if callable(method):
                    try:
                        value = method("modbus_rtu", None)
                    except TypeError:
                        value = method("modbus_rtu")
                    if isinstance(value, dict):
                        return copy.deepcopy(value)
            full = self._manager_full_config(manager)
            if isinstance(full, dict) and isinstance(full.get("modbus_rtu"), dict):
                return copy.deepcopy(full["modbus_rtu"])

        path = self._config_path()
        if path.exists():
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict) and isinstance(data.get("modbus_rtu"), dict):
                return data["modbus_rtu"]
        return copy.deepcopy(DEFAULT_CONFIG)

    def _save_config_json(self) -> None:
        manager = self.config_manager
        full = self._manager_full_config(manager) if manager is not None else None
        if not isinstance(full, dict):
            path = self._config_path()
            if path.exists():
                try:
                    with path.open("r", encoding="utf-8") as file:
                        full = json.load(file)
                except (OSError, json.JSONDecodeError):
                    full = {}
            else:
                full = {}
        full = copy.deepcopy(full)
        full["modbus_rtu"] = copy.deepcopy(self.config)

        manager_updated = False
        manager_saved = False
        if manager is not None:
            update_section = getattr(manager, "update_section", None)
            set_method = getattr(manager, "set", None)
            set_config = getattr(manager, "set_config", None)
            if callable(update_section):
                update_section("modbus_rtu", copy.deepcopy(self.config))
                manager_updated = True
            elif callable(set_method):
                set_method("modbus_rtu", copy.deepcopy(self.config))
                manager_updated = True
            elif callable(set_config):
                self._call_save_method(set_config, full)
                manager_updated = True
            else:
                for attr_name in ("config", "data", "settings"):
                    attr = getattr(manager, attr_name, None)
                    if isinstance(attr, dict):
                        attr["modbus_rtu"] = copy.deepcopy(self.config)
                        manager_updated = True
                        break

            for method_name in ("save_config", "save", "write_config", "persist"):
                method = getattr(manager, method_name, None)
                if callable(method):
                    self._call_save_method(method, full)
                    manager_saved = True
                    break

        if not manager_saved:
            path = self._config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as file:
                json.dump(full, file, ensure_ascii=False, indent=2)
                file.write("\n")
        if manager is not None and not manager_updated:
            self._log("WARNING", "config_manager沒有可辨識的更新方法，已直接寫入config.json")

    @staticmethod
    def _manager_full_config(manager) -> Optional[Dict[str, Any]]:
        if manager is None:
            return None
        for method_name in ("get_config", "get_all", "as_dict", "to_dict"):
            method = getattr(manager, method_name, None)
            if callable(method):
                try:
                    value = method()
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(value, dict):
                    return value
        for attr_name in ("config", "data", "settings"):
            value = getattr(manager, attr_name, None)
            if isinstance(value, dict):
                return value
        return None

    @staticmethod
    def _call_save_method(method, full_config: Dict[str, Any]) -> None:
        try:
            required = [
                item
                for item in inspect.signature(method).parameters.values()
                if item.kind in (item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD)
                and item.default is item.empty
            ]
            method(copy.deepcopy(full_config)) if required else method()
        except (TypeError, ValueError):
            try:
                method()
            except TypeError:
                method(copy.deepcopy(full_config))

    def _reload_config_manager(self) -> None:
        if self.config_manager is None:
            return
        for method_name in ("reload_config", "reload", "load_config", "load"):
            method = getattr(self.config_manager, method_name, None)
            if callable(method):
                try:
                    method()
                    return
                except TypeError:
                    continue

    def _config_path(self) -> Path:
        if self.config_manager is not None:
            for attr_name in ("config_path", "path", "file_path", "config_file"):
                value = getattr(self.config_manager, attr_name, None)
                if isinstance(value, (str, Path)) and str(value).strip():
                    return Path(value)
        return Path("config.json")

    # Manager操作 --------------------------------------------------------
    def start_polling(self) -> None:
        self._manager_action("啟動Modbus輪詢", "start_polling")

    def stop_polling(self) -> None:
        self._manager_action("停止Modbus輪詢", "stop_polling")

    def read_all_once(self) -> None:
        self._manager_action("讀取一次", "read_all_once")

    def _manager_action(self, text: str, method_name: str) -> None:
        if self.action_running:
            messagebox.showinfo("操作進行中", "目前已有Modbus操作正在執行。", parent=self)
            return
        if self.modbus_manager is None:
            messagebox.showerror("無法執行", "app_context未提供modbus_manager。", parent=self)
            return
        method = getattr(self.modbus_manager, method_name, None)
        if not callable(method):
            messagebox.showerror("無法執行", f"modbus_manager未提供{method_name}()。", parent=self)
            return

        self.action_running = True
        self._status(f"正在{text}……")

        def worker() -> None:
            try:
                result = method()
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self._manager_done(text, None, exc))
            else:
                self.after(0, lambda: self._manager_done(text, result, None))

        threading.Thread(target=worker, daemon=True, name=f"ModbusPage-{method_name}").start()

    def _manager_done(self, text: str, result: Any, error: Optional[Exception]) -> None:
        self.action_running = False
        if error is not None:
            self._status(f"{text}失敗：{error}")
            self._log("ERROR", f"{text}失敗：{error}")
            messagebox.showerror(f"{text}失敗", str(error), parent=self)
            return
        suffix = "" if result in (None, "") else f"，結果：{result}"
        self._status(f"{text}完成{suffix}")
        self._log("INFO", f"{text}完成{suffix}")
        self._call_refresh_all()
        self._update_running_status()

    def _poll_running_status(self) -> None:
        self._update_running_status()
        self.status_after_id = self.after(1000, self._poll_running_status)

    def _update_running_status(self) -> None:
        method = getattr(self.modbus_manager, "is_running", None)
        if not callable(method):
            self.running_var.set("輪詢狀態：無法取得")
            return
        try:
            running = bool(method())
        except Exception:  # noqa: BLE001
            self.running_var.set("輪詢狀態：取得失敗")
        else:
            self.running_var.set("輪詢狀態：執行中" if running else "輪詢狀態：已停止")

    # Treeview -----------------------------------------------------------
    def _refresh_devices(self) -> None:
        self.device_tree.delete(*self.device_tree.get_children())
        for index, device in enumerate(self.config["devices"]):
            self.device_tree.insert(
                "",
                tk.END,
                iid=f"device:{index}",
                values=(self._yes_no(device["enable"]), device["name"], device["station_id"]),
            )
        if self.config["devices"]:
            if self.selected_device is None or self.selected_device >= len(self.config["devices"]):
                self.selected_device = 0
            iid = f"device:{self.selected_device}"
            self.device_tree.selection_set(iid)
            self.device_tree.focus(iid)
            self.device_tree.see(iid)
        else:
            self.selected_device = None
        self._refresh_points()

    def _refresh_points(self, selected: Optional[int] = None) -> None:
        self.point_tree.delete(*self.point_tree.get_children())
        if self.selected_device is None:
            return
        points = self.config["devices"][self.selected_device]["points"]
        for index, point in enumerate(points):
            self.point_tree.insert(
                "",
                tk.END,
                iid=f"point:{index}",
                values=(
                    self._yes_no(point["enable"]),
                    point["name"],
                    point["type"],
                    point["address"],
                    point["count"],
                    point["data_type"],
                    self._yes_no(point["writable"]),
                    self._yes_no(point["db_enable"]),
                ),
            )
        if selected is not None and 0 <= selected < len(points):
            iid = f"point:{selected}"
            self.point_tree.selection_set(iid)
            self.point_tree.focus(iid)
            self.point_tree.see(iid)

    def _device_selected(self, _event=None) -> None:
        index = self._tree_index(self.device_tree, "device:")
        if index is not None:
            self.selected_device = index
            self._refresh_points()

    def _selected_device_index(self) -> Optional[int]:
        index = self._tree_index(self.device_tree, "device:")
        if index is not None:
            self.selected_device = index
        return self.selected_device

    def _selected_point_index(self) -> Optional[int]:
        return self._tree_index(self.point_tree, "point:")

    @staticmethod
    def _tree_index(tree: ttk.Treeview, prefix: str) -> Optional[int]:
        selection = tree.selection()
        if not selection or not selection[0].startswith(prefix):
            return None
        try:
            return int(selection[0][len(prefix) :])
        except ValueError:
            return None

    # 驗證與工具 ---------------------------------------------------------
    def _normalize(self, raw: Any) -> Dict[str, Any]:
        config = copy.deepcopy(DEFAULT_CONFIG)
        if isinstance(raw, dict):
            config.update(copy.deepcopy(raw))
        devices = []
        for device_index, source in enumerate(config.get("devices", [])):
            if not isinstance(source, dict):
                continue
            device = copy.deepcopy(DEFAULT_DEVICE)
            device.update(copy.deepcopy(source))
            device["enable"] = bool(device.get("enable", True))
            device["name"] = str(device.get("name") or f"PLC_{device_index + 1}")
            device["station_id"] = self._to_int(device.get("station_id"), device_index + 1)
            points = []
            for point_index, raw_point in enumerate(device.get("points", [])):
                if not isinstance(raw_point, dict):
                    continue
                point = copy.deepcopy(DEFAULT_POINT)
                point.update(copy.deepcopy(raw_point))
                point["enable"] = bool(point.get("enable", True))
                point["name"] = str(point.get("name") or f"Point_{point_index + 1}")
                point["type"] = str(point.get("type") or "holding_register")
                point["address"] = self._to_int(point.get("address"), 0)
                point["count"] = max(1, self._to_int(point.get("count"), 1))
                point["data_type"] = str(point.get("data_type") or "uint16")
                point["writable"] = bool(point.get("writable", False))
                point["db_enable"] = bool(point.get("db_enable", False))
                if point["type"] in READ_ONLY_TYPES:
                    point["writable"] = False
                points.append(point)
            device["points"] = points
            devices.append(device)
        config["devices"] = devices
        return config

    def _validate_config(self) -> None:
        names: set[str] = set()
        station_ids: set[int] = set()
        for device in self.config["devices"]:
            name_key = device["name"].casefold()
            if name_key in names:
                raise ValueError(f"PLC名稱重複：{device['name']}")
            names.add(name_key)
            if not 1 <= device["station_id"] <= 247:
                raise ValueError(f"PLC「{device['name']}」站號必須介於1到247。")
            if device["station_id"] in station_ids:
                raise ValueError(f"Modbus站號重複：{device['station_id']}")
            station_ids.add(device["station_id"])
            point_names: set[str] = set()
            for point in device["points"]:
                key = point["name"].casefold()
                if key in point_names:
                    raise ValueError(f"PLC「{device['name']}」點位名稱重複：{point['name']}")
                point_names.add(key)
                if point["type"] not in POINT_TYPES:
                    raise ValueError(f"點位「{point['name']}」使用不支援的type。")
                if point["address"] < 0 or point["count"] < 1:
                    raise ValueError(f"點位「{point['name']}」位址或數量無效。")

    def _device_is_unique(self, candidate: Dict[str, Any], ignore: Optional[int] = None) -> bool:
        for index, device in enumerate(self.config["devices"]):
            if index == ignore:
                continue
            if device["name"].casefold() == candidate["name"].casefold():
                messagebox.showerror("資料重複", f"PLC名稱「{candidate['name']}」已存在。", parent=self)
                return False
            if device["station_id"] == candidate["station_id"]:
                messagebox.showerror("資料重複", f"Modbus站號{candidate['station_id']}已被使用。", parent=self)
                return False
        return True

    def _point_is_unique(
        self,
        device_index: int,
        candidate: Dict[str, Any],
        ignore: Optional[int] = None,
    ) -> bool:
        for index, point in enumerate(self.config["devices"][device_index]["points"]):
            if index != ignore and point["name"].casefold() == candidate["name"].casefold():
                messagebox.showerror("資料重複", f"點位名稱「{candidate['name']}」已存在。", parent=self)
                return False
        return True

    @staticmethod
    def _unique_name(prefix: str, items) -> str:
        names = {str(item.get("name", "")).casefold() for item in items}
        number = len(items) + 1
        while f"{prefix}_{number}".casefold() in names:
            number += 1
        return f"{prefix}_{number}"

    def _ctx(self, name: str, default: Any = None) -> Any:
        return self.app_context.get(name, default) if isinstance(self.app_context, dict) else getattr(
            self.app_context, name, default
        )

    def _call_refresh_all(self) -> None:
        if callable(self.refresh_all):
            try:
                self.refresh_all()
            except Exception as exc:  # noqa: BLE001
                self._log("ERROR", f"refresh_all執行失敗：{exc}")

    def _log(self, level: str, text: str) -> None:
        if not callable(self.log_func):
            return
        try:
            self.log_func(level, text)
        except TypeError:
            try:
                self.log_func(f"[{level}] {text}")
            except Exception:  # noqa: BLE001
                pass

    def _status(self, text: str) -> None:
        self.status_var.set(text)

    @staticmethod
    def _yes_no(value: Any) -> str:
        return "是" if bool(value) else "否"

    @staticmethod
    def _to_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _positive_int(value: Any, field: str) -> int:
        try:
            number = int(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"{field}必須是整數。") from exc
        if number <= 0:
            raise ValueError(f"{field}必須大於0。")
        return number

    @staticmethod
    def _positive_float(value: Any, field: str) -> float:
        try:
            number = float(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"{field}必須是數字。") from exc
        if number <= 0:
            raise ValueError(f"{field}必須大於0。")
        return number

    def destroy(self) -> None:
        if self.status_after_id is not None:
            try:
                self.after_cancel(self.status_after_id)
            except tk.TclError:
                pass
        super().destroy()


class _ModalDialog(tk.Toplevel):
    def __init__(self, parent, title: str) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self.result: Optional[Dict[str, Any]] = None
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def show(self) -> Optional[Dict[str, Any]]:
        self.update_idletasks()
        top = self.master.winfo_toplevel()
        x = top.winfo_rootx() + max(0, (top.winfo_width() - self.winfo_width()) // 2)
        y = top.winfo_rooty() + max(0, (top.winfo_height() - self.winfo_height()) // 2)
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        self.grab_set()
        self.wait_window()
        return self.result

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class _DeviceDialog(_ModalDialog):
    def __init__(self, parent, title: str, source: Dict[str, Any]) -> None:
        super().__init__(parent, title)
        self.source = copy.deepcopy(source)
        self.enable_var = tk.BooleanVar(value=bool(source.get("enable", True)))
        self.name_var = tk.StringVar(value=str(source.get("name", "")))
        self.station_var = tk.StringVar(value=str(source.get("station_id", 1)))
        frame = ttk.Frame(self, padding=14)
        frame.grid()
        ttk.Checkbutton(frame, text="啟用此PLC", variable=self.enable_var).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )
        ttk.Label(frame, text="PLC名稱").grid(row=1, column=0, sticky="e", padx=(0, 8), pady=5)
        name_entry = ttk.Entry(frame, textvariable=self.name_var, width=28)
        name_entry.grid(row=1, column=1, pady=5)
        ttk.Label(frame, text="Modbus站號").grid(row=2, column=0, sticky="e", padx=(0, 8), pady=5)
        ttk.Spinbox(frame, textvariable=self.station_var, from_=1, to=247, width=10).grid(
            row=2, column=1, sticky="w", pady=5
        )
        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="確定", command=self._ok).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="取消", command=self._cancel).pack(side=tk.LEFT)
        name_entry.focus_set()
        self.bind("<Return>", lambda _event: self._ok())
        self.bind("<Escape>", lambda _event: self._cancel())

    def _ok(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("輸入錯誤", "PLC名稱不可為空白。", parent=self)
            return
        try:
            station_id = int(self.station_var.get())
        except ValueError:
            messagebox.showerror("輸入錯誤", "Modbus站號必須是整數。", parent=self)
            return
        if not 1 <= station_id <= 247:
            messagebox.showerror("輸入錯誤", "Modbus站號必須介於1到247。", parent=self)
            return
        result = copy.deepcopy(self.source)
        result.update(
            {
                "enable": bool(self.enable_var.get()),
                "name": name,
                "station_id": station_id,
                "points": copy.deepcopy(self.source.get("points", [])),
            }
        )
        self.result = result
        self.destroy()


class _PointDialog(_ModalDialog):
    def __init__(self, parent, title: str, source: Dict[str, Any]) -> None:
        super().__init__(parent, title)
        self.source = copy.deepcopy(source)
        self.enable_var = tk.BooleanVar(value=bool(source.get("enable", True)))
        self.name_var = tk.StringVar(value=str(source.get("name", "")))
        self.type_var = tk.StringVar(value=str(source.get("type", "holding_register")))
        self.address_var = tk.StringVar(value=str(source.get("address", 0)))
        self.count_var = tk.StringVar(value=str(source.get("count", 1)))
        self.data_type_var = tk.StringVar(value=str(source.get("data_type", "uint16")))
        self.writable_var = tk.BooleanVar(value=bool(source.get("writable", False)))
        self.db_enable_var = tk.BooleanVar(value=bool(source.get("db_enable", False)))

        frame = ttk.Frame(self, padding=14)
        frame.grid()
        ttk.Checkbutton(frame, text="啟用此點位", variable=self.enable_var).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )
        fields = (
            ("點位名稱", ttk.Entry(frame, textvariable=self.name_var, width=30)),
            (
                "類型",
                ttk.Combobox(frame, textvariable=self.type_var, values=POINT_TYPES, state="readonly", width=27),
            ),
            ("起始位址", ttk.Entry(frame, textvariable=self.address_var, width=14)),
            ("讀取數量", ttk.Entry(frame, textvariable=self.count_var, width=14)),
            (
                "資料型別",
                ttk.Combobox(frame, textvariable=self.data_type_var, values=DATA_TYPES, state="normal", width=27),
            ),
        )
        for row, (label, widget) in enumerate(fields, start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="e", padx=(0, 8), pady=4)
            widget.grid(row=row, column=1, sticky="w", pady=4)
        fields[1][1].bind("<<ComboboxSelected>>", lambda _event: self._update_writable())

        options = ttk.Frame(frame)
        options.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 2))
        self.writable_check = ttk.Checkbutton(options, text="允許寫入", variable=self.writable_var)
        self.writable_check.pack(side=tk.LEFT, padx=(0, 14))
        ttk.Checkbutton(options, text="寫入資料庫", variable=self.db_enable_var).pack(side=tk.LEFT)
        ttk.Label(frame, text="input_register與discrete_input為唯讀類型。 ").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        buttons = ttk.Frame(frame)
        buttons.grid(row=8, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="確定", command=self._ok).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="取消", command=self._cancel).pack(side=tk.LEFT)
        fields[0][1].focus_set()
        self._update_writable()
        self.bind("<Return>", lambda _event: self._ok())
        self.bind("<Escape>", lambda _event: self._cancel())

    def _update_writable(self) -> None:
        if self.type_var.get() in READ_ONLY_TYPES:
            self.writable_var.set(False)
            self.writable_check.state(["disabled"])
        else:
            self.writable_check.state(["!disabled"])

    def _ok(self) -> None:
        name = self.name_var.get().strip()
        point_type = self.type_var.get().strip()
        data_type = self.data_type_var.get().strip()
        if not name:
            messagebox.showerror("輸入錯誤", "點位名稱不可為空白。", parent=self)
            return
        if point_type not in POINT_TYPES:
            messagebox.showerror("輸入錯誤", "請選擇支援的點位類型。", parent=self)
            return
        try:
            address = int(self.address_var.get())
            count = int(self.count_var.get())
        except ValueError:
            messagebox.showerror("輸入錯誤", "位址與數量必須是整數。", parent=self)
            return
        if address < 0 or count < 1:
            messagebox.showerror("輸入錯誤", "位址不可小於0，數量必須大於等於1。", parent=self)
            return
        if not data_type:
            messagebox.showerror("輸入錯誤", "資料型別不可為空白。", parent=self)
            return
        result = copy.deepcopy(self.source)
        result.update(
            {
                "enable": bool(self.enable_var.get()),
                "name": name,
                "type": point_type,
                "address": address,
                "count": count,
                "data_type": data_type,
                "writable": False if point_type in READ_ONLY_TYPES else bool(self.writable_var.get()),
                "db_enable": bool(self.db_enable_var.get()),
            }
        )
        self.result = result
        self.destroy()
