"""系統總覽頁面。"""

from __future__ import annotations

import asyncio
import inspect
import threading
import tkinter as tk
from collections.abc import Mapping
from datetime import datetime
from tkinter import ttk
from typing import Any, Callable


class OverviewPage(ttk.Frame):
    """顯示通訊服務、資料庫與ValueBus的整體狀態。"""

    REFRESH_INTERVAL_MS = 1000

    def __init__(self, parent, app_context):
        super().__init__(parent)

        self.app_context = app_context
        self.config_manager = app_context["config_manager"]
        self.value_bus = app_context["value_bus"]
        self.database_manager = app_context["database_manager"]
        self.modbus_manager = app_context["modbus_manager"]
        self.opcua_manager = app_context["opcua_manager"]
        self.log_func = app_context["log_func"]

        self.modbus_status_var = tk.StringVar(value="讀取中…")
        self.opcua_server_count_var = tk.StringVar(value="0")
        self.database_status_var = tk.StringVar(value="讀取中…")
        self.value_bus_count_var = tk.StringVar(value="0")
        self.last_refresh_var = tk.StringVar(value="尚未重新整理")

        self._refresh_after_id: str | None = None
        self._action_buttons: list[ttk.Button] = []
        self._action_running = False

        self._build_ui()
        self.refresh()
        self._schedule_refresh()

    def _build_ui(self) -> None:
        """建立頁面元件。"""
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header_frame = ttk.Frame(self, padding=(16, 14, 16, 8))
        header_frame.grid(row=0, column=0, sticky="ew")
        header_frame.columnconfigure(0, weight=1)

        ttk.Label(
            header_frame,
            text="系統總覽",
            font=("Microsoft JhengHei UI", 18, "bold"),
        ).grid(row=0, column=0, sticky="w")

        ttk.Label(
            header_frame,
            textvariable=self.last_refresh_var,
        ).grid(row=0, column=1, sticky="e")

        content_frame = ttk.Frame(self, padding=(16, 8, 16, 16))
        content_frame.grid(row=1, column=0, sticky="nsew")
        content_frame.columnconfigure(0, weight=1)
        content_frame.rowconfigure(1, weight=1)

        status_frame = ttk.LabelFrame(
            content_frame,
            text="目前狀態",
            padding=12,
        )
        status_frame.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        status_frame.columnconfigure(1, weight=1)

        self._add_status_row(
            status_frame,
            0,
            "Modbus RTU狀態",
            self.modbus_status_var,
        )
        self._add_status_row(
            status_frame,
            1,
            "OPC UA Server數量",
            self.opcua_server_count_var,
        )
        self._add_status_row(
            status_frame,
            2,
            "資料庫狀態",
            self.database_status_var,
        )
        self._add_status_row(
            status_frame,
            3,
            "ValueBus最新點位數量",
            self.value_bus_count_var,
        )

        action_frame = ttk.LabelFrame(
            content_frame,
            text="快捷操作",
            padding=12,
        )
        action_frame.grid(row=1, column=0, sticky="nsew")
        action_frame.columnconfigure(0, weight=1)
        action_frame.columnconfigure(1, weight=1)

        actions: list[tuple[str, Callable[[], Any]]] = [
            ("啟動Modbus輪詢", self.modbus_manager.start_polling),
            ("停止Modbus輪詢", self.modbus_manager.stop_polling),
            ("OPC UA全部連線", self.opcua_manager.connect_all),
            ("OPC UA全部斷線", self.opcua_manager.disconnect_all),
            ("OPC UA全部訂閱", self.opcua_manager.subscribe_all),
            ("啟動資料庫自動上傳", self.database_manager.start_auto_write),
            ("停止資料庫自動上傳", self.database_manager.stop_auto_write),
            ("重新整理狀態", self.refresh),
        ]

        for index, (button_text, command) in enumerate(actions):
            button = ttk.Button(
                action_frame,
                text=button_text,
                command=lambda text=button_text, func=command: self._run_action(
                    text,
                    func,
                ),
            )
            button.grid(
                row=index // 2,
                column=index % 2,
                sticky="ew",
                padx=5,
                pady=5,
                ipady=4,
            )
            self._action_buttons.append(button)

    @staticmethod
    def _add_status_row(
        parent: ttk.Frame,
        row: int,
        title: str,
        value_var: tk.StringVar,
    ) -> None:
        ttk.Label(parent, text=f"{title}：").grid(
            row=row,
            column=0,
            sticky="w",
            padx=(0, 16),
            pady=6,
        )
        ttk.Label(
            parent,
            textvariable=value_var,
            font=("Microsoft JhengHei UI", 10, "bold"),
        ).grid(row=row, column=1, sticky="w", pady=6)

    def refresh(self) -> None:
        """更新所有狀態文字。"""
        self._refresh_modbus_status()
        self._refresh_opcua_status()
        self._refresh_database_status()
        self._refresh_value_bus_status()

        self.last_refresh_var.set(
            f"最後更新：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def _refresh_modbus_status(self) -> None:
        enabled = self._read_enabled_setting(
            "modbus_rtu",
            "modbus",
            "MODBUS_RTU",
        )

        try:
            running = bool(self.modbus_manager.is_running())
        except Exception:
            running = False

        if enabled is None:
            enabled_text = "啟用狀態未知"
        else:
            enabled_text = "已啟用" if enabled else "未啟用"

        running_text = "輪詢中" if running else "已停止"
        self.modbus_status_var.set(f"{enabled_text}／{running_text}")

    def _refresh_opcua_status(self) -> None:
        servers = self._read_opcua_servers()
        self.opcua_server_count_var.set(str(len(servers)))

    def _refresh_database_status(self) -> None:
        enabled = self._read_enabled_setting(
            "database",
            "db",
            "mysql",
        )

        if enabled is None:
            self.database_status_var.set("啟用狀態未知")
        else:
            self.database_status_var.set("已啟用" if enabled else "未啟用")

    def _refresh_value_bus_status(self) -> None:
        try:
            latest_dict = self.value_bus.get_latest_dict()
            count = len(latest_dict) if latest_dict is not None else 0
        except Exception as exc:
            count = 0
            self._write_log("WARNING", f"讀取ValueBus點位數量失敗：{exc}")

        self.value_bus_count_var.set(str(count))

    def _run_action(
        self,
        action_name: str,
        action: Callable[[], Any],
    ) -> None:
        """在背景執行快捷操作，避免阻塞Tkinter主執行緒。"""
        if self._action_running:
            self._write_log("WARNING", "目前已有快捷操作執行中")
            return

        if action_name == "重新整理狀態":
            try:
                action()
                self._write_log("INFO", "已重新整理總覽狀態")
            except Exception as exc:
                self._write_log("ERROR", f"重新整理狀態失敗：{exc}")
            return

        self._action_running = True
        self._set_buttons_enabled(False)
        self._write_log("INFO", f"開始執行：{action_name}")

        def worker() -> None:
            try:
                result = action()
                if inspect.isawaitable(result):
                    asyncio.run(result)
                self._write_log("INFO", f"執行完成：{action_name}")
            except Exception as exc:
                self._write_log(
                    "ERROR",
                    f"執行失敗：{action_name}，原因：{exc}",
                )
            finally:
                try:
                    self.after(0, self._finish_action)
                except tk.TclError:
                    pass

        threading.Thread(
            target=worker,
            name=f"OverviewAction-{action_name}",
            daemon=True,
        ).start()

    def _finish_action(self) -> None:
        self._action_running = False
        self._set_buttons_enabled(True)
        self.refresh()

    def _set_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self._action_buttons:
            button.configure(state=state)

    def _schedule_refresh(self) -> None:
        self._refresh_after_id = self.after(
            self.REFRESH_INTERVAL_MS,
            self._automatic_refresh,
        )

    def _automatic_refresh(self) -> None:
        self.refresh()
        self._schedule_refresh()

    def _read_enabled_setting(self, *section_names: str) -> bool | None:
        section = self._read_config_section(*section_names)

        if isinstance(section, bool):
            return section

        if isinstance(section, Mapping):
            for key in ("enabled", "enable", "is_enabled", "active"):
                if key in section:
                    return self._to_bool(section[key])

        return None

    def _read_opcua_servers(self) -> list[Any]:
        section = self._read_config_section(
            "opcua",
            "opc_ua",
            "OPCUA",
            "opcua_servers",
        )

        if isinstance(section, Mapping):
            servers = section.get("servers", section.get("server_list", section))
        else:
            servers = section

        if isinstance(servers, Mapping):
            return list(servers.values())
        if isinstance(servers, (list, tuple)):
            return list(servers)
        return []

    def _read_config_section(self, *section_names: str) -> Any:
        config_data = self._get_config_mapping()

        for section_name in section_names:
            if section_name in config_data:
                return config_data[section_name]

        for method_name in ("get_section", "get"):
            method = getattr(self.config_manager, method_name, None)
            if not callable(method):
                continue

            for section_name in section_names:
                try:
                    if method_name == "get":
                        value = method(section_name, None)
                    else:
                        value = method(section_name)
                except TypeError:
                    try:
                        value = method(section_name)
                    except Exception:
                        continue
                except Exception:
                    continue

                if value is not None:
                    return value

        return None

    def _get_config_mapping(self) -> Mapping[str, Any]:
        for method_name in ("get_config", "get_all", "as_dict", "to_dict"):
            method = getattr(self.config_manager, method_name, None)
            if not callable(method):
                continue

            try:
                value = method()
            except Exception:
                continue

            if isinstance(value, Mapping):
                return value

        for attribute_name in ("config", "data", "_config"):
            value = getattr(self.config_manager, attribute_name, None)
            if isinstance(value, Mapping):
                return value

        return {}

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
                "enabled",
                "啟用",
            }
        return bool(value)

    def _write_log(self, level: str, message: str) -> None:
        """相容常見的log_func參數順序。"""
        try:
            self.log_func(message, level)
        except TypeError:
            try:
                self.log_func(level, message)
            except TypeError:
                self.log_func(f"[{level}] {message}")

    def destroy(self) -> None:
        if self._refresh_after_id is not None:
            try:
                self.after_cancel(self._refresh_after_id)
            except tk.TclError:
                pass
            self._refresh_after_id = None

        super().destroy()
