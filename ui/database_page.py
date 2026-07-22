"""資料庫設定頁面。

本頁面只負責設定資料與呼叫DatabaseManager公開介面，
不會直接建立pymysql連線。
"""

from __future__ import annotations

import copy
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, Dict, Optional, Tuple


class DatabasePage(ttk.Frame):
    """資料庫設定與自動上傳控制頁面。"""

    def __init__(self, parent, app_context):
        super().__init__(parent)
        self.app_context = app_context
        self.config_manager = app_context["config_manager"]
        self.database_manager = app_context["database_manager"]
        self.log_func: Callable[[str], None] = app_context.get("log_func", print)
        self.refresh_all: Callable[[], None] = app_context.get(
            "refresh_all", lambda: None
        )

        self._busy = False
        self._auto_write_running = False

        self.enable_var = tk.BooleanVar(value=False)
        self.host_var = tk.StringVar(value="127.0.0.1")
        self.port_var = tk.StringVar(value="3306")
        self.user_var = tk.StringVar(value="root")
        self.password_var = tk.StringVar(value="")
        self.database_var = tk.StringVar(value="multi_protocol_plc_hmi")
        self.charset_var = tk.StringVar(value="utf8mb4")
        self.connect_timeout_var = tk.StringVar(value="5")
        self.write_history_var = tk.BooleanVar(value=True)
        self.write_latest_var = tk.BooleanVar(value=True)
        self.write_only_on_change_var = tk.BooleanVar(value=True)

        self.connection_status_var = tk.StringVar(value="尚未測試")
        self.auto_write_status_var = tk.StringVar(value="已停止")
        self.operation_status_var = tk.StringVar(value="就緒")

        self._build_ui()
        self.load_settings()
        self.refresh_status()

    def _build_ui(self) -> None:
        """建立頁面元件。"""
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        title_frame = ttk.Frame(self, padding=(12, 12, 12, 6))
        title_frame.grid(row=0, column=0, sticky="ew")
        title_frame.columnconfigure(0, weight=1)

        ttk.Label(
            title_frame,
            text="資料庫設定",
            font=("TkDefaultFont", 15, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            title_frame,
            text="設定MySQL/MariaDB連線，並控制即時資料自動上傳。",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        content = ttk.Frame(self, padding=(12, 6, 12, 6))
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)

        connection_frame = ttk.LabelFrame(content, text="連線設定", padding=12)
        connection_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        connection_frame.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            connection_frame,
            text="啟用資料庫功能",
            variable=self.enable_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self._add_entry(connection_frame, 1, "主機位址", self.host_var)
        self._add_entry(connection_frame, 2, "連接埠", self.port_var)
        self._add_entry(connection_frame, 3, "使用者名稱", self.user_var)
        self._add_entry(
            connection_frame,
            4,
            "密碼",
            self.password_var,
            show="*",
        )
        self._add_entry(connection_frame, 5, "資料庫名稱", self.database_var)

        ttk.Label(connection_frame, text="字元編碼").grid(
            row=6, column=0, sticky="w", padx=(0, 10), pady=4
        )
        charset_box = ttk.Combobox(
            connection_frame,
            textvariable=self.charset_var,
            values=("utf8mb4", "utf8", "latin1"),
            state="normal",
        )
        charset_box.grid(row=6, column=1, sticky="ew", pady=4)

        self._add_entry(
            connection_frame,
            7,
            "連線逾時（秒）",
            self.connect_timeout_var,
        )

        write_frame = ttk.LabelFrame(content, text="寫入設定", padding=12)
        write_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        write_frame.columnconfigure(0, weight=1)

        ttk.Checkbutton(
            write_frame,
            text="寫入歷史資料表",
            variable=self.write_history_var,
        ).grid(row=0, column=0, sticky="w", pady=5)
        ttk.Checkbutton(
            write_frame,
            text="更新最新值資料表",
            variable=self.write_latest_var,
        ).grid(row=1, column=0, sticky="w", pady=5)
        ttk.Checkbutton(
            write_frame,
            text="僅在數值變更時寫入",
            variable=self.write_only_on_change_var,
        ).grid(row=2, column=0, sticky="w", pady=5)

        ttk.Separator(write_frame).grid(row=3, column=0, sticky="ew", pady=12)

        status_grid = ttk.Frame(write_frame)
        status_grid.grid(row=4, column=0, sticky="ew")
        status_grid.columnconfigure(1, weight=1)

        ttk.Label(status_grid, text="資料庫連線：").grid(
            row=0, column=0, sticky="w", pady=4
        )
        self.connection_status_label = ttk.Label(
            status_grid, textvariable=self.connection_status_var
        )
        self.connection_status_label.grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(status_grid, text="自動上傳：").grid(
            row=1, column=0, sticky="w", pady=4
        )
        self.auto_write_status_label = ttk.Label(
            status_grid, textvariable=self.auto_write_status_var
        )
        self.auto_write_status_label.grid(row=1, column=1, sticky="w", pady=4)

        action_frame = ttk.LabelFrame(self, text="操作", padding=12)
        action_frame.grid(row=2, column=0, sticky="new", padx=12, pady=(6, 12))
        for column in range(5):
            action_frame.columnconfigure(column, weight=1)

        self.save_button = ttk.Button(
            action_frame,
            text="儲存設定",
            command=self.save_settings,
        )
        self.save_button.grid(row=0, column=0, sticky="ew", padx=4)

        self.test_button = ttk.Button(
            action_frame,
            text="測試連線",
            command=self.test_connection,
        )
        self.test_button.grid(row=0, column=1, sticky="ew", padx=4)

        self.ensure_button = ttk.Button(
            action_frame,
            text="自動建立資料表",
            command=self.ensure_tables,
        )
        self.ensure_button.grid(row=0, column=2, sticky="ew", padx=4)

        self.start_button = ttk.Button(
            action_frame,
            text="啟動自動上傳",
            command=self.start_auto_write,
        )
        self.start_button.grid(row=0, column=3, sticky="ew", padx=4)

        self.stop_button = ttk.Button(
            action_frame,
            text="停止自動上傳",
            command=self.stop_auto_write,
        )
        self.stop_button.grid(row=0, column=4, sticky="ew", padx=4)

        ttk.Label(
            action_frame,
            textvariable=self.operation_status_var,
            anchor="w",
        ).grid(row=1, column=0, columnspan=5, sticky="ew", padx=4, pady=(10, 0))

        self._action_buttons = (
            self.save_button,
            self.test_button,
            self.ensure_button,
            self.start_button,
            self.stop_button,
        )

    @staticmethod
    def _add_entry(
        parent: ttk.Frame,
        row: int,
        label_text: str,
        variable: tk.StringVar,
        show: Optional[str] = None,
    ) -> ttk.Entry:
        ttk.Label(parent, text=label_text).grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=4
        )
        entry = ttk.Entry(parent, textvariable=variable, show=show or "")
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        return entry

    def load_settings(self) -> None:
        """從ConfigManager載入database設定。"""
        config = self._get_full_config()
        database_config = config.get("database", {})
        if not isinstance(database_config, dict):
            database_config = {}

        self.enable_var.set(bool(database_config.get("enable", False)))
        self.host_var.set(str(database_config.get("host", "127.0.0.1")))
        self.port_var.set(str(database_config.get("port", 3306)))
        self.user_var.set(str(database_config.get("user", "root")))
        self.password_var.set(str(database_config.get("password", "")))
        self.database_var.set(
            str(database_config.get("database", "multi_protocol_plc_hmi"))
        )
        self.charset_var.set(str(database_config.get("charset", "utf8mb4")))
        self.connect_timeout_var.set(
            str(database_config.get("connect_timeout", 5))
        )
        self.write_history_var.set(
            bool(database_config.get("write_history", True))
        )
        self.write_latest_var.set(
            bool(database_config.get("write_latest", True))
        )
        self.write_only_on_change_var.set(
            bool(database_config.get("write_only_on_change", True))
        )
        self.operation_status_var.set("已載入資料庫設定")

    def save_settings(self, show_message: bool = True) -> bool:
        """驗證並保存database設定，成功後重新載入DatabaseManager。"""
        try:
            database_config = self._collect_database_config()
            full_config = self._get_full_config()
            full_config["database"] = database_config
            self._save_full_config(full_config)
            self.database_manager.reload_config()
            self.operation_status_var.set("資料庫設定已儲存")
            self._log("資料庫設定已儲存並重新載入")
            self.refresh_all()
            if show_message:
                messagebox.showinfo("儲存完成", "資料庫設定已成功儲存。")
            return True
        except (TypeError, ValueError) as exc:
            self.operation_status_var.set(f"設定錯誤：{exc}")
            if show_message:
                messagebox.showerror("設定錯誤", str(exc))
            return False
        except Exception as exc:  # UI層需顯示管理器或設定檔錯誤
            self.operation_status_var.set(f"儲存失敗：{exc}")
            self._log(f"資料庫設定儲存失敗：{exc}")
            if show_message:
                messagebox.showerror("儲存失敗", f"無法儲存資料庫設定：\n{exc}")
            return False

    def test_connection(self) -> None:
        """儲存設定後，透過DatabaseManager測試資料庫連線。"""
        if not self.save_settings(show_message=False):
            return

        self.connection_status_var.set("測試中...")
        self._run_background(
            operation_name="測試資料庫連線",
            target=self.database_manager.test_connection,
            on_success=self._on_connection_test_success,
            on_error=self._on_connection_test_error,
        )

    def ensure_tables(self) -> None:
        """透過DatabaseManager建立必要資料表。"""
        if not self.save_settings(show_message=False):
            return

        self._run_background(
            operation_name="建立資料表",
            target=self.database_manager.ensure_tables,
            on_success=lambda result: self._on_simple_success(
                "資料表建立完成", result
            ),
            on_error=lambda exc: self._on_simple_error("資料表建立失敗", exc),
        )

    def start_auto_write(self) -> None:
        """啟動DatabaseManager自動寫入。"""
        if not self.save_settings(show_message=False):
            return
        if not self.enable_var.get():
            messagebox.showwarning("尚未啟用", "請先勾選「啟用資料庫功能」。")
            return

        try:
            result = self.database_manager.start_auto_write()
            if result is False:
                raise RuntimeError("DatabaseManager回報啟動失敗")
            self._auto_write_running = True
            self.auto_write_status_var.set("執行中")
            self.operation_status_var.set("自動上傳已啟動")
            self._log("資料庫自動上傳已啟動")
            self.refresh_status()
            self.refresh_all()
        except Exception as exc:
            self._auto_write_running = False
            self.auto_write_status_var.set("啟動失敗")
            self.operation_status_var.set(f"啟動失敗：{exc}")
            self._log(f"資料庫自動上傳啟動失敗：{exc}")
            messagebox.showerror("啟動失敗", f"無法啟動自動上傳：\n{exc}")

    def stop_auto_write(self) -> None:
        """停止DatabaseManager自動寫入。"""
        try:
            result = self.database_manager.stop_auto_write()
            if result is False:
                raise RuntimeError("DatabaseManager回報停止失敗")
            self._auto_write_running = False
            self.auto_write_status_var.set("已停止")
            self.operation_status_var.set("自動上傳已停止")
            self._log("資料庫自動上傳已停止")
            self.refresh_status()
            self.refresh_all()
        except Exception as exc:
            self.operation_status_var.set(f"停止失敗：{exc}")
            self._log(f"資料庫自動上傳停止失敗：{exc}")
            messagebox.showerror("停止失敗", f"無法停止自動上傳：\n{exc}")

    def refresh_status(self) -> None:
        """更新自動上傳狀態與按鈕可用狀態。"""
        running = self._detect_auto_write_running()
        self._auto_write_running = running
        self.auto_write_status_var.set("執行中" if running else "已停止")

        if self._busy:
            for button in self._action_buttons:
                button.configure(state="disabled")
            return

        self.save_button.configure(state="normal")
        self.test_button.configure(state="normal")
        self.ensure_button.configure(state="normal")
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")

    def _collect_database_config(self) -> Dict[str, Any]:
        host = self.host_var.get().strip()
        user = self.user_var.get().strip()
        database_name = self.database_var.get().strip()
        charset = self.charset_var.get().strip()

        if not host:
            raise ValueError("主機位址不可為空白。")
        if not user:
            raise ValueError("使用者名稱不可為空白。")
        if not database_name:
            raise ValueError("資料庫名稱不可為空白。")
        if not charset:
            raise ValueError("字元編碼不可為空白。")

        try:
            port = int(self.port_var.get().strip())
        except ValueError as exc:
            raise ValueError("連接埠必須是整數。") from exc
        if not 1 <= port <= 65535:
            raise ValueError("連接埠必須介於1到65535。")

        try:
            connect_timeout = int(self.connect_timeout_var.get().strip())
        except ValueError as exc:
            raise ValueError("連線逾時必須是整數秒數。") from exc
        if connect_timeout < 1:
            raise ValueError("連線逾時必須大於或等於1秒。")

        return {
            "enable": bool(self.enable_var.get()),
            "host": host,
            "port": port,
            "user": user,
            "password": self.password_var.get(),
            "database": database_name,
            "charset": charset,
            "connect_timeout": connect_timeout,
            "write_history": bool(self.write_history_var.get()),
            "write_latest": bool(self.write_latest_var.get()),
            "write_only_on_change": bool(self.write_only_on_change_var.get()),
        }

    def _get_full_config(self) -> Dict[str, Any]:
        manager = self.config_manager
        config: Any

        if hasattr(manager, "get_config"):
            config = manager.get_config()
        elif hasattr(manager, "load_config"):
            config = manager.load_config()
        elif hasattr(manager, "config"):
            config = manager.config
        elif isinstance(manager, dict):
            config = manager
        else:
            raise AttributeError("ConfigManager未提供可讀取設定的公開介面。")

        if config is None:
            return {}
        if not isinstance(config, dict):
            raise TypeError("ConfigManager回傳的設定必須是dict。")
        return copy.deepcopy(config)

    def _save_full_config(self, config: Dict[str, Any]) -> None:
        manager = self.config_manager

        if hasattr(manager, "save_config"):
            try:
                manager.save_config(config)
            except TypeError:
                if hasattr(manager, "config"):
                    manager.config = config
                manager.save_config()
            return

        if hasattr(manager, "update_config"):
            manager.update_config(config)
            return

        if hasattr(manager, "set_config"):
            manager.set_config(config)
            if hasattr(manager, "save"):
                manager.save()
            return

        if isinstance(manager, dict):
            manager.clear()
            manager.update(config)
            return

        raise AttributeError("ConfigManager未提供可儲存設定的公開介面。")

    def _run_background(
        self,
        operation_name: str,
        target: Callable[[], Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        if self._busy:
            return

        self._busy = True
        self.operation_status_var.set(f"{operation_name}中...")
        self.refresh_status()

        def worker() -> None:
            try:
                result = target()
                if result is False:
                    raise RuntimeError(f"{operation_name}回報失敗")
                self.after(0, lambda: self._finish_background(on_success, result))
            except Exception as exc:
                self.after(0, lambda error=exc: self._finish_background(on_error, error))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_background(
        self,
        callback: Callable[[Any], None],
        value: Any,
    ) -> None:
        self._busy = False
        callback(value)
        self.refresh_status()

    def _on_connection_test_success(self, result: Any) -> None:
        detail = self._result_message(result)
        self.connection_status_var.set("連線成功")
        self.operation_status_var.set(
            f"資料庫連線成功{f'：{detail}' if detail else ''}"
        )
        self._log("資料庫連線測試成功")
        messagebox.showinfo("連線成功", "資料庫連線測試成功。")

    def _on_connection_test_error(self, exc: Exception) -> None:
        self.connection_status_var.set("連線失敗")
        self.operation_status_var.set(f"資料庫連線失敗：{exc}")
        self._log(f"資料庫連線測試失敗：{exc}")
        messagebox.showerror("連線失敗", f"資料庫連線測試失敗：\n{exc}")

    def _on_simple_success(self, title: str, result: Any) -> None:
        detail = self._result_message(result)
        self.operation_status_var.set(
            f"{title}{f'：{detail}' if detail else ''}"
        )
        self._log(title)
        messagebox.showinfo(title, title)

    def _on_simple_error(self, title: str, exc: Exception) -> None:
        self.operation_status_var.set(f"{title}：{exc}")
        self._log(f"{title}：{exc}")
        messagebox.showerror(title, f"{title}：\n{exc}")

    def _detect_auto_write_running(self) -> bool:
        manager = self.database_manager
        for method_name in ("is_auto_write_running", "is_running"):
            method = getattr(manager, method_name, None)
            if callable(method):
                try:
                    return bool(method())
                except Exception:
                    pass

        for attribute_name in (
            "auto_write_running",
            "_auto_write_running",
            "running",
            "_running",
        ):
            if hasattr(manager, attribute_name):
                try:
                    return bool(getattr(manager, attribute_name))
                except Exception:
                    pass

        return self._auto_write_running

    @staticmethod
    def _result_message(result: Any) -> str:
        if result is None or result is True:
            return ""
        if isinstance(result, tuple) and len(result) >= 2:
            return str(result[1])
        if isinstance(result, dict):
            for key in ("message", "detail", "status"):
                if key in result:
                    return str(result[key])
            return ""
        return str(result)

    def _log(self, message: str) -> None:
        try:
            self.log_func(message)
        except Exception:
            pass
