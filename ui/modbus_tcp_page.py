"""Modbus TCP設定頁，每台PLC各自設定連線端點。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Optional

import tkinter as tk
from tkinter import messagebox, ttk

from .modbus_page import (
    DEFAULT_POINT,
    ModbusPage,
    POINT_TYPES,
    READ_ONLY_TYPES,
    _ModalDialog,
)

DEFAULT_TCP_CONFIG = {
    "enable": False,
    "timeout": 3.0,
    "poll_interval": 1.0,
    "devices": [],
}
DEFAULT_TCP_DEVICE = {
    "enable": True,
    "name": "PLC_1",
    "host": "127.0.0.1",
    "port": 502,
    "station_id": 1,
    "points": [],
}


class ModbusTcpPage(ModbusPage):
    """Modbus TCP連線、PLC裝置與點位設定頁。"""

    def __init__(self, parent, app_context):
        ttk.Frame.__init__(self, parent)
        self.app_context = app_context
        self.config_manager = self._ctx("config_manager")
        self.modbus_manager = self._ctx("modbus_tcp_manager")
        self.log_func = self._ctx("log_func", print)
        self.refresh_all = self._ctx("refresh_all")

        self.config: Dict[str, Any] = copy.deepcopy(DEFAULT_TCP_CONFIG)
        self.selected_device: Optional[int] = None
        self.action_running = False
        self.status_after_id: Optional[str] = None

        self.enable_var = tk.BooleanVar(value=False)
        self.timeout_var = tk.StringVar(value="3.0")
        self.poll_interval_var = tk.StringVar(value="1.0")
        self.status_var = tk.StringVar(value="尚未載入設定")
        self.running_var = tk.StringVar(value="輪詢狀態：未知")

        self._build_ui()
        self.reload_settings(show_message=False, reload_manager=False)
        self._poll_running_status()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        title = ttk.Frame(self, padding=(10, 10, 10, 4))
        title.grid(row=0, column=0, sticky="ew")
        title.columnconfigure(0, weight=1)
        ttk.Label(
            title,
            text="Modbus TCP設定",
            font=("TkDefaultFont", 14, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(title, textvariable=self.running_var).grid(
            row=0, column=1, sticky="e"
        )
        self._build_tcp_frame()
        self._build_tree_area()
        self._build_action_bar()

    def _build_tcp_frame(self) -> None:
        frame = ttk.LabelFrame(self, text="共用輪詢參數", padding=10)
        frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(4, 8))
        for column in (1, 3, 5):
            frame.columnconfigure(column, weight=1)
        ttk.Checkbutton(
            frame,
            text="啟用Modbus TCP",
            variable=self.enable_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
        self._entry(frame, 0, 2, "逾時秒數", self.timeout_var)
        self._entry(frame, 0, 4, "輪詢間隔秒數", self.poll_interval_var)
        ttk.Label(
            frame,
            text="每台PLC的IP、TCP Port與Unit ID請在新增／修改PLC中設定。",
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(5, 0))

    def _device_tree_specs(self):
        return (
            ("enable", "啟用", 50, tk.CENTER),
            ("name", "PLC名稱", 110, tk.W),
            ("host", "主機 / IP", 145, tk.W),
            ("port", "TCP Port", 65, tk.CENTER),
            ("station_id", "Unit ID", 60, tk.CENTER),
        )

    def _device_tree_values(self, device: Dict[str, Any]):
        return (
            self._yes_no(device["enable"]),
            device["name"],
            device["host"],
            device["port"],
            device["station_id"],
        )

    def _next_device_station_id(self) -> int:
        # 不同IP的PLC通常都使用Unit ID 1。
        return 1

    def _show_device_dialog(
        self,
        title: str,
        source: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        initial = copy.deepcopy(DEFAULT_TCP_DEVICE)
        initial.update(copy.deepcopy(source))
        if "host" not in source:
            initial["host"] = getattr(
                self,
                "_legacy_host",
                DEFAULT_TCP_DEVICE["host"],
            )
        if "port" not in source:
            initial["port"] = getattr(
                self,
                "_legacy_port",
                DEFAULT_TCP_DEVICE["port"],
            )
        return _TcpDeviceDialog(self, title, initial).show()

    def _build_action_bar(self) -> None:
        frame = ttk.Frame(self, padding=(10, 0, 10, 10))
        frame.grid(row=3, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, textvariable=self.status_var).grid(
            row=0, column=0, sticky="w"
        )
        buttons = ttk.Frame(frame)
        buttons.grid(row=0, column=1, sticky="e")
        for text, command in (
            ("儲存設定", self.save_settings),
            ("重新載入設定", self.reload_settings),
            ("啟動TCP輪詢", self.start_polling),
            ("停止TCP輪詢", self.stop_polling),
            ("讀取一次", self.read_all_once),
        ):
            ttk.Button(buttons, text=text, command=command).pack(
                side=tk.LEFT, padx=4
            )

    def save_settings(self) -> None:
        try:
            self._form_to_config()
            self._validate_config()
            self._save_config_json()
            if self.modbus_manager is None:
                raise RuntimeError("app_context未提供modbus_tcp_manager。")
            self.modbus_manager.reload_config()
            self._call_refresh_all()
            self._status("Modbus TCP設定已儲存")
            self._log("INFO", "已儲存Modbus TCP設定並重新載入管理器")
            messagebox.showinfo(
                "儲存完成",
                "每台PLC的Modbus TCP連線設定已寫入config.json。",
                parent=self,
            )
        except Exception as exc:
            self._status(f"儲存失敗：{exc}")
            self._log("ERROR", f"儲存Modbus TCP設定失敗：{exc}")
            messagebox.showerror("儲存失敗", str(exc), parent=self)

    def reload_settings(
        self,
        show_message: bool = True,
        reload_manager: bool = True,
    ) -> None:
        try:
            self._reload_config_manager()
            self.config = self._normalize(self._read_modbus_section())
            self.selected_device = 0 if self.config["devices"] else None
            self._config_to_form()
            self._refresh_devices()
            if reload_manager and self.modbus_manager is not None:
                self.modbus_manager.reload_config()
            self._call_refresh_all()
            self._status("已重新載入Modbus TCP設定")
            if show_message:
                messagebox.showinfo(
                    "重新載入",
                    "Modbus TCP設定已重新載入。",
                    parent=self,
                )
        except Exception as exc:
            self._status(f"重新載入失敗：{exc}")
            self._log("ERROR", f"重新載入Modbus TCP設定失敗：{exc}")
            if show_message:
                messagebox.showerror("重新載入失敗", str(exc), parent=self)

    def _form_to_config(self) -> None:
        self.config.update(
            {
                "enable": bool(self.enable_var.get()),
                "timeout": self._positive_float(
                    self.timeout_var.get(), "逾時秒數"
                ),
                "poll_interval": self._positive_float(
                    self.poll_interval_var.get(), "輪詢間隔秒數"
                ),
            }
        )
        # 尚無device時保留舊端點，供使用者第一次新增PLC時自動帶入。
        if not self.config.get("devices") and getattr(
            self, "_had_legacy_endpoint", False
        ):
            self.config["host"] = self._legacy_host
            self.config["port"] = self._legacy_port
        else:
            self.config.pop("host", None)
            self.config.pop("port", None)

    def _config_to_form(self) -> None:
        self.enable_var.set(bool(self.config["enable"]))
        self.timeout_var.set(str(self.config["timeout"]))
        self.poll_interval_var.set(str(self.config["poll_interval"]))

    def _read_modbus_section(self) -> Dict[str, Any]:
        if self.config_manager is not None:
            value = self.config_manager.get_section("modbus_tcp", None)
            if isinstance(value, dict):
                return copy.deepcopy(value)
        path = self._config_path()
        if path.exists():
            with path.open("r", encoding="utf-8") as file:
                value = json.load(file).get("modbus_tcp")
            if isinstance(value, dict):
                return value
        return copy.deepcopy(DEFAULT_TCP_CONFIG)

    def _save_config_json(self) -> None:
        if self.config_manager is not None:
            setter = getattr(self.config_manager, "set_section", None)
            if not callable(setter):
                setter = getattr(self.config_manager, "set", None)
            saver = getattr(self.config_manager, "save_config", None)
            if not callable(saver):
                saver = getattr(self.config_manager, "save", None)
            if callable(setter) and callable(saver):
                setter("modbus_tcp", copy.deepcopy(self.config))
                saver()
                return

            update = getattr(self.config_manager, "update_section", None)
            if callable(update):
                update("modbus_tcp", copy.deepcopy(self.config))
                return

        path = Path(self._config_path())
        full = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as file:
                full = json.load(file)
        full["modbus_tcp"] = copy.deepcopy(self.config)
        with path.open("w", encoding="utf-8") as file:
            json.dump(full, file, ensure_ascii=False, indent=2)
            file.write("\n")

    def _normalize(self, raw: Any) -> Dict[str, Any]:
        raw_config = copy.deepcopy(raw) if isinstance(raw, dict) else {}
        legacy_host = str(raw_config.get("host", "127.0.0.1") or "").strip()
        legacy_port = self._to_int(raw_config.get("port"), 502)
        self._legacy_host = legacy_host
        self._legacy_port = legacy_port
        self._had_legacy_endpoint = "host" in raw_config or "port" in raw_config

        config = copy.deepcopy(DEFAULT_TCP_CONFIG)
        config.update(raw_config)
        config.pop("host", None)
        config.pop("port", None)

        devices = []
        for device_index, source in enumerate(config.get("devices", [])):
            if not isinstance(source, dict):
                continue
            device = copy.deepcopy(DEFAULT_TCP_DEVICE)
            device.update(copy.deepcopy(source))
            device["enable"] = bool(device.get("enable", True))
            device["name"] = str(
                device.get("name") or f"PLC_{device_index + 1}"
            )
            if "host" in source:
                raw_host = source.get("host")
            elif "ip" in source:
                raw_host = source.get("ip")
            else:
                raw_host = legacy_host
            device["host"] = str(raw_host or "").strip()
            device["port"] = self._to_int(
                source.get("port", legacy_port),
                legacy_port,
            )
            device.pop("ip", None)
            device["station_id"] = self._to_int(
                device.get("station_id"),
                1,
            )

            points = []
            for point_index, source_point in enumerate(device.get("points", [])):
                if not isinstance(source_point, dict):
                    continue
                point = copy.deepcopy(DEFAULT_POINT)
                point.update(copy.deepcopy(source_point))
                point["enable"] = bool(point.get("enable", True))
                point["name"] = str(
                    point.get("name") or f"Point_{point_index + 1}"
                )
                point["type"] = str(
                    point.get("type") or "holding_register"
                )
                point["address"] = self._to_int(point.get("address"), 0)
                point["count"] = max(
                    1,
                    self._to_int(point.get("count"), 1),
                )
                point["data_type"] = str(
                    point.get("data_type") or "uint16"
                )
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
        endpoints: set[tuple[str, int, int]] = set()
        for device in self.config["devices"]:
            name_key = device["name"].casefold()
            if name_key in names:
                raise ValueError(f"PLC名稱重複：{device['name']}")
            names.add(name_key)

            host = str(device.get("host", "")).strip()
            port = self._to_int(device.get("port"), 0)
            station_id = self._to_int(device.get("station_id"), -1)
            if not host:
                raise ValueError(f"PLC「{device['name']}」主機/IP不可空白。")
            if not 1 <= port <= 65535:
                raise ValueError(
                    f"PLC「{device['name']}」TCP Port必須介於1到65535。"
                )
            if not 1 <= station_id <= 247:
                raise ValueError(
                    f"PLC「{device['name']}」Unit ID必須介於1到247。"
                )
            endpoint_key = (host.casefold(), port, station_id)
            if endpoint_key in endpoints:
                raise ValueError(
                    f"Modbus TCP端點與Unit ID重複："
                    f"{host}:{port} / {station_id}"
                )
            endpoints.add(endpoint_key)

            point_names: set[str] = set()
            for point in device["points"]:
                key = point["name"].casefold()
                if key in point_names:
                    raise ValueError(
                        f"PLC「{device['name']}」點位名稱重複："
                        f"{point['name']}"
                    )
                point_names.add(key)
                if point["type"] not in POINT_TYPES:
                    raise ValueError(
                        f"點位「{point['name']}」使用不支援的type。"
                    )
                if point["address"] < 0 or point["count"] < 1:
                    raise ValueError(
                        f"點位「{point['name']}」位址或數量無效。"
                    )

    def _device_is_unique(
        self,
        candidate: Dict[str, Any],
        ignore: Optional[int] = None,
    ) -> bool:
        candidate_endpoint = (
            str(candidate.get("host", "")).strip().casefold(),
            self._to_int(candidate.get("port"), 0),
            self._to_int(candidate.get("station_id"), -1),
        )
        for index, device in enumerate(self.config["devices"]):
            if index == ignore:
                continue
            if device["name"].casefold() == candidate["name"].casefold():
                messagebox.showerror(
                    "資料重複",
                    f"PLC名稱「{candidate['name']}」已存在。",
                    parent=self,
                )
                return False
            endpoint = (
                str(device.get("host", "")).strip().casefold(),
                self._to_int(device.get("port"), 0),
                self._to_int(device.get("station_id"), -1),
            )
            if endpoint == candidate_endpoint:
                messagebox.showerror(
                    "資料重複",
                    "相同IP、TCP Port與Unit ID的PLC已存在。",
                    parent=self,
                )
                return False
        return True


class _TcpDeviceDialog(_ModalDialog):
    """Modbus TCP PLC連線參數編輯視窗。"""

    def __init__(
        self,
        parent,
        title: str,
        source: Dict[str, Any],
    ) -> None:
        super().__init__(parent, title)
        self.source = copy.deepcopy(source)
        self.enable_var = tk.BooleanVar(value=bool(source.get("enable", True)))
        self.name_var = tk.StringVar(value=str(source.get("name", "")))
        self.host_var = tk.StringVar(value=str(source.get("host", "127.0.0.1")))
        self.port_var = tk.StringVar(value=str(source.get("port", 502)))
        self.station_var = tk.StringVar(value=str(source.get("station_id", 1)))

        frame = ttk.Frame(self, padding=14)
        frame.grid()
        ttk.Checkbutton(
            frame,
            text="啟用此PLC",
            variable=self.enable_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        fields = (
            ("PLC名稱", ttk.Entry(frame, textvariable=self.name_var, width=30)),
            ("主機 / IP", ttk.Entry(frame, textvariable=self.host_var, width=30)),
            ("TCP Port", ttk.Entry(frame, textvariable=self.port_var, width=12)),
            (
                "Unit ID",
                ttk.Spinbox(
                    frame,
                    textvariable=self.station_var,
                    from_=1,
                    to=247,
                    width=10,
                ),
            ),
        )
        for row, (label, widget) in enumerate(fields, start=1):
            ttk.Label(frame, text=label).grid(
                row=row,
                column=0,
                sticky="e",
                padx=(0, 8),
                pady=5,
            )
            widget.grid(row=row, column=1, sticky="w", pady=5)

        buttons = ttk.Frame(frame)
        buttons.grid(
            row=5,
            column=0,
            columnspan=2,
            sticky="e",
            pady=(12, 0),
        )
        ttk.Button(buttons, text="確定", command=self._ok).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(buttons, text="取消", command=self._cancel).pack(side=tk.LEFT)
        fields[0][1].focus_set()
        self.bind("<Return>", lambda _event: self._ok())
        self.bind("<Escape>", lambda _event: self._cancel())

    def _ok(self) -> None:
        name = self.name_var.get().strip()
        host = self.host_var.get().strip()
        if not name:
            messagebox.showerror(
                "輸入錯誤",
                "PLC名稱不可為空白。",
                parent=self,
            )
            return
        if not host:
            messagebox.showerror(
                "輸入錯誤",
                "主機/IP不可為空白。",
                parent=self,
            )
            return
        try:
            port = int(self.port_var.get())
            station_id = int(self.station_var.get())
        except ValueError:
            messagebox.showerror(
                "輸入錯誤",
                "TCP Port與Unit ID必須是整數。",
                parent=self,
            )
            return
        if not 1 <= port <= 65535:
            messagebox.showerror(
                "輸入錯誤",
                "TCP Port必須介於1到65535。",
                parent=self,
            )
            return
        if not 1 <= station_id <= 247:
            messagebox.showerror(
                "輸入錯誤",
                "Unit ID必須介於1到247。",
                parent=self,
            )
            return

        result = copy.deepcopy(self.source)
        result.update(
            {
                "enable": bool(self.enable_var.get()),
                "name": name,
                "host": host,
                "port": port,
                "station_id": station_id,
                "points": copy.deepcopy(self.source.get("points", [])),
            }
        )
        self.result = result
        self.destroy()
