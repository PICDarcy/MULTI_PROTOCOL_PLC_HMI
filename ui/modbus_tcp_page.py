"""Modbus TCP設定頁面。"""

from __future__ import annotations

import json
import threading
import tkinter as tk
from collections.abc import Mapping
from tkinter import messagebox, ttk
from typing import Any


DEFAULT_SECTION = {
    "enable": False,
    "timeout": 1.0,
    "poll_interval": 1.0,
    "default_port": 502,
    "devices": [
        {
            "enable": True,
            "name": "範例Modbus_TCP_PLC",
            "host": "192.168.1.10",
            "port": 502,
            "unit_id": 1,
            "timeout": 1.0,
            "points": [
                {"enable": True, "name": "速度設定值", "type": "holding_register", "address": 0, "count": 1, "data_type": "UInt16", "writable": True, "db_enable": True},
                {"enable": True, "name": "實際溫度", "type": "input_register", "address": 10, "count": 2, "data_type": "Float32_ABCD", "writable": False, "db_enable": True},
                {"enable": True, "name": "啟動命令", "type": "coil", "address": 0, "count": 1, "data_type": "Bool", "writable": True, "db_enable": True},
                {"enable": True, "name": "安全門狀態", "type": "discrete_input", "address": 0, "count": 1, "data_type": "Bool", "writable": False, "db_enable": True},
            ],
        }
    ],
}


class ModbusTcpPage(ttk.Frame):
    """以JSON方式管理Modbus TCP設定，並提供啟停與讀取測試。"""

    def __init__(self, parent, app_context):
        super().__init__(parent)

        self.app_context = app_context
        self.config_manager = self._context_get("config_manager")
        self.modbus_tcp_manager = self._context_get("modbus_tcp_manager")
        self.log_func = self._context_get("log_func", lambda message: None)
        self.refresh_all = self._context_get("refresh_all")

        self._worker_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._destroyed = False

        self.status_var = tk.StringVar(value="就緒")
        self.running_var = tk.StringVar(value="未知")
        self.device_count_var = tk.StringVar(value="0")
        self.point_count_var = tk.StringVar(value="0")

        self._build_ui()
        self.refresh()

    def _context_get(self, name: str, default: Any = None) -> Any:
        if isinstance(self.app_context, Mapping):
            return self.app_context.get(name, default)
        return getattr(self.app_context, name, default)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        title_frame = ttk.Frame(self, padding=(8, 8, 8, 4))
        title_frame.grid(row=0, column=0, sticky="ew")
        title_frame.columnconfigure(0, weight=1)
        ttk.Label(title_frame, text="Modbus TCP設定", font=("", 14, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(title_frame, text="支援多IP、多Port、多Unit ID，點位會發布為MODBUS_TCP。").grid(row=1, column=0, sticky="w", pady=(4, 0))

        summary = ttk.LabelFrame(self, text="狀態", padding=8)
        summary.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        ttk.Label(summary, text="輪詢：").grid(row=0, column=0, sticky="w")
        ttk.Label(summary, textvariable=self.running_var).grid(row=0, column=1, sticky="w")
        ttk.Label(summary, text="設備數：").grid(row=0, column=2, sticky="w", padx=(12, 0))
        ttk.Label(summary, textvariable=self.device_count_var).grid(row=0, column=3, sticky="w")
        ttk.Label(summary, text="點位數：").grid(row=0, column=4, sticky="w", padx=(12, 0))
        ttk.Label(summary, textvariable=self.point_count_var).grid(row=0, column=5, sticky="w")

        editor_frame = ttk.LabelFrame(self, text="modbus_tcp設定JSON", padding=8)
        editor_frame.grid(row=2, column=0, sticky="nsew", padx=8, pady=4)
        editor_frame.columnconfigure(0, weight=1)
        editor_frame.rowconfigure(0, weight=1)
        self.text = tk.Text(editor_frame, wrap="none", undo=True, font=("Consolas", 10))
        y_scroll = ttk.Scrollbar(editor_frame, orient="vertical", command=self.text.yview)
        x_scroll = ttk.Scrollbar(editor_frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")

        button_frame = ttk.Frame(self, padding=(8, 4, 8, 8))
        button_frame.grid(row=3, column=0, sticky="ew")
        button_frame.columnconfigure(99, weight=1)
        buttons = (
            ("重新載入設定", self.refresh),
            ("套用範例設定", self.load_example),
            ("儲存設定", self.save_settings),
            ("重新載入Manager", self.reload_manager),
            ("啟動輪詢", self.start_polling),
            ("停止輪詢", self.stop_polling),
            ("讀取一次", self.read_once),
        )
        for index, (text, command) in enumerate(buttons):
            ttk.Button(button_frame, text=text, command=command).grid(row=0, column=index, padx=(0 if index == 0 else 6, 0), sticky="w")
        ttk.Label(button_frame, textvariable=self.status_var).grid(row=0, column=99, sticky="e", padx=(12, 0))

        note = (
            "點位type支援holding_register、input_register、coil、discrete_input。\n"
            "可寫入點位請設定writable=true；資料庫上傳可用db_enable控制。\n"
            "常用data_type：UInt16、Int16、UInt32_ABCD、Int32_ABCD、Float32_ABCD、Bool、String。"
        )
        ttk.Label(self, text=note, padding=(8, 0, 8, 8), foreground="#555555").grid(row=4, column=0, sticky="w")

    def refresh(self) -> None:
        section = self._get_section()
        self._set_editor(section)
        self._update_summary(section)
        self.status_var.set("已重新載入設定")

    def refresh_data(self) -> None:
        self.refresh()

    def reload_data(self) -> None:
        self.refresh()

    def update_view(self) -> None:
        self.refresh()

    def _get_section(self) -> dict[str, Any]:
        if self.config_manager is None:
            return dict(DEFAULT_SECTION)
        getter = getattr(self.config_manager, "get_section", None)
        if callable(getter):
            value = getter("modbus_tcp", {})
            if isinstance(value, dict) and value:
                return value
        getter = getattr(self.config_manager, "get_config", None)
        if callable(getter):
            root = getter()
            if isinstance(root, dict) and isinstance(root.get("modbus_tcp"), dict):
                return dict(root["modbus_tcp"])
        return dict(DEFAULT_SECTION)

    def _set_editor(self, section: Mapping[str, Any]) -> None:
        self.text.delete("1.0", "end")
        self.text.insert("1.0", json.dumps(section, ensure_ascii=False, indent=2))

    def _editor_json(self) -> dict[str, Any]:
        text = self.text.get("1.0", "end").strip()
        if not text:
            raise ValueError("modbus_tcp設定不可空白")
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("modbus_tcp根節點必須是JSON物件")
        return data

    def _update_summary(self, section: Mapping[str, Any] | None = None) -> None:
        section = section if section is not None else self._get_section()
        devices = [item for item in section.get("devices", []) if isinstance(item, Mapping)]
        point_count = sum(len([point for point in device.get("points", []) if isinstance(point, Mapping)]) for device in devices)
        running = False
        if self.modbus_tcp_manager is not None:
            checker = getattr(self.modbus_tcp_manager, "is_running", None)
            if callable(checker):
                try:
                    running = bool(checker())
                except Exception:
                    running = False
        self.running_var.set("執行中" if running else "停止")
        self.device_count_var.set(str(len(devices)))
        self.point_count_var.set(str(point_count))

    def load_example(self) -> None:
        self._set_editor(DEFAULT_SECTION)
        self._update_summary(DEFAULT_SECTION)
        self.status_var.set("已載入範例，尚未儲存")

    def save_settings(self) -> None:
        try:
            data = self._editor_json()
            self._validate_section(data)
            updater = getattr(self.config_manager, "update_section", None)
            if not callable(updater):
                raise RuntimeError("ConfigManager未提供update_section()")
            updater("modbus_tcp", data)
            self.status_var.set("modbus_tcp設定已儲存")
            self._log("Modbus TCP設定已儲存")
            self._update_summary(data)
            if callable(self.refresh_all):
                self.refresh_all()
        except Exception as exc:
            messagebox.showerror("儲存失敗", str(exc), parent=self)

    def _validate_section(self, data: Mapping[str, Any]) -> None:
        devices = data.get("devices", [])
        if not isinstance(devices, list):
            raise ValueError("devices必須是陣列")
        for index, device in enumerate(devices, start=1):
            if not isinstance(device, Mapping):
                raise ValueError(f"第{index}個device必須是JSON物件")
            if not str(device.get("host", device.get("ip", ""))).strip():
                raise ValueError(f"第{index}個device缺少host")
            int(device.get("port", data.get("default_port", 502)))
            int(device.get("unit_id", device.get("station_id", 1)))
            points = device.get("points", [])
            if not isinstance(points, list):
                raise ValueError(f"第{index}個device的points必須是陣列")
            for point_index, point in enumerate(points, start=1):
                if not isinstance(point, Mapping):
                    raise ValueError(f"第{index}個device第{point_index}個point必須是JSON物件")
                if str(point.get("type", "")).lower() not in {"holding_register", "input_register", "coil", "discrete_input"}:
                    raise ValueError(f"第{index}個device第{point_index}個point type不支援：{point.get('type')}")
                int(point.get("address", 0))
                int(point.get("count", 1))

    def reload_manager(self) -> None:
        self._run_worker("重新載入Modbus TCP Manager", self._reload_manager_job)

    def _reload_manager_job(self) -> str:
        if self.modbus_tcp_manager is None:
            raise RuntimeError("modbus_tcp_manager尚未建立")
        result = self.modbus_tcp_manager.reload_config()
        return f"重新載入完成：{result}"

    def start_polling(self) -> None:
        self._run_worker("啟動Modbus TCP輪詢", self._start_job)

    def _start_job(self) -> str:
        if self.modbus_tcp_manager is None:
            raise RuntimeError("modbus_tcp_manager尚未建立")
        return str(self.modbus_tcp_manager.start_polling())

    def stop_polling(self) -> None:
        self._run_worker("停止Modbus TCP輪詢", self._stop_job)

    def _stop_job(self) -> str:
        if self.modbus_tcp_manager is None:
            raise RuntimeError("modbus_tcp_manager尚未建立")
        return str(self.modbus_tcp_manager.stop_polling())

    def read_once(self) -> None:
        self._run_worker("讀取一次Modbus TCP", self._read_once_job)

    def _read_once_job(self) -> str:
        if self.modbus_tcp_manager is None:
            raise RuntimeError("modbus_tcp_manager尚未建立")
        result = self.modbus_tcp_manager.read_all_once()
        return f"讀取完成：{result}"

    def _run_worker(self, action_name: str, job) -> None:
        with self._worker_lock:
            if self._worker and self._worker.is_alive():
                messagebox.showwarning("背景工作中", "已有一個Modbus TCP操作正在執行。", parent=self)
                return
            self.status_var.set(f"{action_name}中...")
            self._worker = threading.Thread(target=self._worker_entry, args=(action_name, job), name="ModbusTcpPageWorker", daemon=True)
            self._worker.start()

    def _worker_entry(self, action_name: str, job) -> None:
        error_text = ""
        result_text = ""
        try:
            result_text = str(job())
        except Exception as exc:
            error_text = str(exc) or exc.__class__.__name__

        def finish() -> None:
            if self._destroyed:
                return
            self._update_summary()
            if error_text:
                self.status_var.set(f"{action_name}失敗")
                self._log(f"{action_name}失敗：{error_text}", "ERROR")
                messagebox.showerror(f"{action_name}失敗", error_text, parent=self)
            else:
                self.status_var.set(result_text or f"{action_name}完成")
                self._log(result_text or f"{action_name}完成")

        try:
            self.after(0, finish)
        except (tk.TclError, RuntimeError):
            pass

    def _log(self, message: str, level: str = "INFO") -> None:
        try:
            self.log_func(message, level)
        except TypeError:
            self.log_func(message)
        except Exception:
            pass

    def destroy(self) -> None:
        self._destroyed = True
        super().destroy()
