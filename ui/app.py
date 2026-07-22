"""Tkinter主視窗與各服務整合。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import inspect
import queue
import threading
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from core.config_manager import ConfigManager
from core.database_manager import DatabaseManager
from core.modbus_manager import ModbusRtuManager
from core.opcua_manager import OpcuaMultiServerManager
from core.value_bus import ValueBus


class LogPage(ttk.Frame):
    """顯示主程式與各通訊模組的紀錄。"""

    def __init__(self, parent, app_context):
        super().__init__(parent)
        self.app_context = app_context
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.text = tk.Text(
            self,
            wrap="word",
            state="disabled",
            font=("Consolas", 10),
        )
        scrollbar = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self.text.yview,
        )
        self.text.configure(yscrollcommand=scrollbar.set)

        self.text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

    def append_line(self, text: str) -> None:
        """加入一行紀錄並自動捲到最新內容。"""
        self.text.configure(state="normal")
        self.text.insert("end", f"{text}\n")
        self.text.configure(state="disabled")
        self.text.see("end")


class _UnavailablePage(ttk.Frame):
    """其他部分尚未完成或載入失敗時的提示頁面。"""

    def __init__(self, parent, app_context, page_title: str, error_text: str):
        super().__init__(parent, padding=24)
        self.app_context = app_context
        self.columnconfigure(0, weight=1)

        ttk.Label(
            self,
            text=f"{page_title}目前無法載入",
            font=("Microsoft JhengHei UI", 16, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 12))
        ttk.Label(
            self,
            text="請確認對應程式檔案與類別已完成，並查看紀錄頁的詳細錯誤。",
            wraplength=720,
        ).grid(row=1, column=0, sticky="w")
        ttk.Label(
            self,
            text=error_text,
            wraplength=720,
        ).grid(row=2, column=0, sticky="w", pady=(12, 0))


class App(tk.Tk):
    """工業通訊整合HMI主程式。"""

    PAGE_SPECS = (
        ("overview", "總覽", "ui.overview_page", "OverviewPage"),
        ("monitor", "統一監控/讀寫", "ui.monitor_page", "MonitorPage"),
        ("modbus", "Modbus RTU設定", "ui.modbus_page", "ModbusPage"),
        (
            "opcua_server",
            "OPC UA Server設定",
            "ui.opcua_server_page",
            "OpcuaServerPage",
        ),
        (
            "opcua_browse",
            "OPC UA瀏覽/掃描",
            "ui.opcua_browse_page",
            "OpcuaBrowsePage",
        ),
        ("database", "資料庫設定", "ui.database_page", "DatabasePage"),
    )

    def __init__(self):
        super().__init__()
        self.title("多協定PLC HMI")
        self.geometry("1280x800")
        self.minsize(1024, 680)

        self._closing = False
        self._cleanup_complete = False
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._log_after_id: str | None = None
        self._force_close_after_id: str | None = None
        self.pages: dict[str, ttk.Frame] = {}

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.report_callback_exception = self._report_callback_exception
        self._configure_style()

        project_root = Path(__file__).resolve().parents[1]
        self.config_path = project_root / "config.json"

        self.config_manager = self._construct_component(
            ConfigManager,
            "ConfigManager",
            {"config_path": str(self.config_path)},
        )
        self.value_bus = self._construct_component(
            ValueBus,
            "ValueBus",
            {},
        )

        service_dependencies = {
            "config_manager": self.config_manager,
            "value_bus": self.value_bus,
            "log_func": self.log_func,
            "config_path": str(self.config_path),
        }
        self.database_manager = self._construct_component(
            DatabaseManager,
            "DatabaseManager",
            service_dependencies,
        )
        self.modbus_manager = self._construct_component(
            ModbusRtuManager,
            "ModbusRtuManager",
            service_dependencies,
        )
        self.opcua_manager = self._construct_component(
            OpcuaMultiServerManager,
            "OpcuaMultiServerManager",
            service_dependencies,
        )

        self.app_context = {
            "config_manager": self.config_manager,
            "value_bus": self.value_bus,
            "database_manager": self.database_manager,
            "modbus_manager": self.modbus_manager,
            "opcua_manager": self.opcua_manager,
            "log_func": self.log_func,
            "refresh_all": self.refresh_all,
            "root": self,
            "app": self,
        }

        self._build_ui()
        self._log_after_id = self.after(100, self._drain_log_queue)
        self.log_func("主程式啟動完成", "INFO")

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        available_themes = style.theme_names()
        if "vista" in available_themes:
            style.theme_use("vista")
        elif "clam" in available_themes:
            style.theme_use("clam")
        style.configure("TNotebook.Tab", padding=(12, 7))

    def _build_ui(self) -> None:
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(container)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)
        self.app_context["notebook"] = self.notebook

        for page_key, tab_title, module_name, class_name in self.PAGE_SPECS:
            page = self._create_page(tab_title, module_name, class_name)
            self.pages[page_key] = page
            self.notebook.add(page, text=tab_title)

        self.log_page = LogPage(self.notebook, self.app_context)
        self.pages["log"] = self.log_page
        self.notebook.add(self.log_page, text="紀錄")

    def _create_page(
        self,
        tab_title: str,
        module_name: str,
        class_name: str,
    ) -> ttk.Frame:
        try:
            module = importlib.import_module(module_name)
            page_class = getattr(module, class_name)
            page = page_class(self.notebook, self.app_context)
            if not isinstance(page, ttk.Frame):
                raise TypeError(f"{class_name}必須繼承ttk.Frame")
            return page
        except Exception as exc:
            error_text = f"{module_name}.{class_name}：{exc}"
            self.log_func(error_text, "ERROR")
            return _UnavailablePage(
                self.notebook,
                self.app_context,
                tab_title,
                error_text,
            )

    @staticmethod
    def _construct_component(
        component_class: type,
        component_name: str,
        available_values: dict[str, Any],
    ) -> Any:
        """依建構子參數名稱注入相依元件，降低各部分整合耦合。"""
        aliases = {
            "config": "config_manager",
            "config_mgr": "config_manager",
            "cfg_manager": "config_manager",
            "bus": "value_bus",
            "logger": "log_func",
            "log": "log_func",
            "log_callback": "log_func",
            "callback": "log_func",
            "path": "config_path",
            "file_path": "config_path",
            "filename": "config_path",
        }

        try:
            signature = inspect.signature(component_class)
        except (TypeError, ValueError):
            return component_class()

        positional_args: list[Any] = []
        keyword_args: dict[str, Any] = {}
        has_var_keyword = False

        for parameter in signature.parameters.values():
            if parameter.name == "self":
                continue
            if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
                continue
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                has_var_keyword = True
                continue

            source_name = parameter.name
            if source_name not in available_values:
                source_name = aliases.get(source_name, source_name)

            if source_name in available_values:
                value = available_values[source_name]
                if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
                    positional_args.append(value)
                else:
                    keyword_args[parameter.name] = value
                continue

            if parameter.default is inspect.Parameter.empty:
                raise TypeError(
                    f"無法建立{component_name}：缺少必要建構參數「{parameter.name}」"
                )

        if has_var_keyword:
            for key in ("config_manager", "value_bus", "log_func", "config_path"):
                if key in available_values:
                    keyword_args.setdefault(key, available_values[key])

        return component_class(*positional_args, **keyword_args)

    def log_func(self, message: Any, level: str = "INFO") -> None:
        """執行緒安全的統一紀錄入口。"""
        known_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        message_text = str(message)
        level_text = str(level).upper()

        # 相容少數模組以log_func(level, message)的順序呼叫。
        if message_text.upper() in known_levels and level_text not in known_levels:
            message_text, level_text = str(level), message_text.upper()
        if level_text not in known_levels:
            level_text = "INFO"

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._log_queue.put(f"{timestamp} [{level_text}] {message_text}")

    def _drain_log_queue(self) -> None:
        """在Tkinter主執行緒更新紀錄頁並捲到最新位置。"""
        try:
            while True:
                line = self._log_queue.get_nowait()
                if hasattr(self, "log_page") and self.log_page.winfo_exists():
                    self.log_page.append_line(line)
                else:
                    print(line)
        except queue.Empty:
            pass

        if not self._closing:
            self._log_after_id = self.after(100, self._drain_log_queue)

    def refresh_all(self) -> None:
        """呼叫所有已載入頁面的重新整理方法。"""
        for page_key, page in tuple(self.pages.items()):
            if page_key == "log" or isinstance(page, _UnavailablePage):
                continue

            for method_name in ("refresh", "refresh_data", "reload_data", "update_view"):
                method = getattr(page, method_name, None)
                if not callable(method):
                    continue
                try:
                    method()
                except Exception as exc:
                    self.log_func(
                        f"頁面重新整理失敗（{page_key}.{method_name}）：{exc}",
                        "ERROR",
                    )
                break

    def _report_callback_exception(
        self,
        exception_type: type[BaseException],
        exception_value: BaseException,
        exception_traceback,
    ) -> None:
        detail = "".join(
            traceback.format_exception(
                exception_type,
                exception_value,
                exception_traceback,
            )
        )
        self.log_func(detail.rstrip(), "ERROR")
        try:
            messagebox.showerror(
                "程式錯誤",
                f"執行操作時發生錯誤：\n{exception_value}",
                parent=self,
            )
        except tk.TclError:
            print(detail)

    @staticmethod
    def _call_maybe_async(method) -> None:
        result = method()
        if isinstance(result, concurrent.futures.Future):
            result.result(timeout=10)
        elif inspect.isawaitable(result):
            asyncio.run(result)

    def on_close(self) -> None:
        """停止背景服務後安全關閉Tkinter。"""
        if self._closing:
            return
        self._closing = True
        self.title("多協定PLC HMI－正在關閉")
        self.log_func("開始停止通訊服務並關閉主程式", "INFO")

        opcua_stop_method = getattr(
            self.opcua_manager,
            "shutdown",
            self.opcua_manager.disconnect_all,
        )

        def cleanup_worker() -> None:
            cleanup_steps = (
                ("停止Modbus輪詢", self.modbus_manager.stop_polling),
                ("停止OPC UA服務", opcua_stop_method),
                ("停止資料庫自動上傳", self.database_manager.stop_auto_write),
            )
            for label, method in cleanup_steps:
                try:
                    self._call_maybe_async(method)
                    self.log_func(f"{label}完成", "INFO")
                except Exception as exc:
                    self.log_func(f"{label}失敗：{exc}", "ERROR")
            self._cleanup_complete = True
            try:
                self.after(0, self._final_destroy)
            except tk.TclError:
                pass

        threading.Thread(
            target=cleanup_worker,
            name="ApplicationCleanup",
            daemon=True,
        ).start()

        # 避免第三方通訊函式永久阻塞而無法關閉視窗。
        self._force_close_after_id = self.after(12000, self._final_destroy)

    def _final_destroy(self) -> None:
        if self._log_after_id is not None:
            try:
                self.after_cancel(self._log_after_id)
            except tk.TclError:
                pass
            self._log_after_id = None

        if self._force_close_after_id is not None:
            try:
                self.after_cancel(self._force_close_after_id)
            except tk.TclError:
                pass
            self._force_close_after_id = None

        # 關閉前最後一次輸出尚未寫入紀錄頁的內容。
        try:
            self._drain_log_queue()
        except tk.TclError:
            pass

        try:
            self.destroy()
        except tk.TclError:
            pass
