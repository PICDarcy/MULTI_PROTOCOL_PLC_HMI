"""資料庫設定頁面。

此模組僅負責資料庫設定與操作介面。實際連線、建表及資料上傳
皆透過app_context.database_manager執行，不在UI層直接操作pymysql。
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, Dict, Optional, Tuple


class DatabasePage(ttk.Frame):
    """資料庫設定與自動上傳控制頁面。"""

    def __init__(self, parent, app_context):
        super().__init__(parent)
        self.app_context = app_context
        self.config_manager = self._context_get(
            app_context,
            "config_manager",
        )
        self.database_manager = self._context_get(
            app_context,
            "database_manager",
        )
        self.log_func: Callable[[str], None] = self._context_get(
            app_context,
            "log_func",
            lambda message: None,
        )
        self.refresh_all: Callable[[], None] = self._context_get(
            app_context,
            "refresh_all",
            lambda: None,
        )

        self._operation_running = False
        self._local_auto_write_running = False
        self._status_after_id: Optional[str] = None

        self.enable_var = tk.BooleanVar(value=False)
        self.host_var = tk.StringVar(value="127.0.0.1")
        self.port_var = tk.StringVar(value="3306")
        self.user_var = tk.StringVar(value="")
        self.password_var = tk.StringVar(value="")
        self.database_var = tk.StringVar(value="")
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
        self._schedule_status_refresh()

    @staticmethod
    def _context_get(
        app_context: Any,
        key: str,
        default: Any = None,
    ) -> Any:
        """同時支援字典與屬性型app_context。"""
        if isinstance(app_context, dict):
            if key in app_context:
                return app_context[key]
        elif hasattr(app_context, key):
            return getattr(app_context, key)

        if default is not None:
            return default
        raise KeyError(f"app_context缺少必要項目：{key}")

    def _build_ui(self) -> None:
        """建立頁面元件。"""
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        header.columnconfigure(0, weight=1)

        ttk.Label(
            header,
            text="資料庫設定",
            font=("TkDefaultFont", 15, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="設定資料庫連線資訊、寫入模式與自動上傳狀態",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        self._build_connection_frame(body)
        self._build_write_frame(body)
        self._build_status_frame(body)
        self._build_button_frame()

    def _build_connection_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="資料庫連線設定", padding=12)
        frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        frame.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            frame,
            text="啟用資料庫功能",
            variable=self.enable_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        fields = (
            ("主機位址", self.host_var, False),
            ("連接埠", self.port_var, False),
            ("使用者名稱", self.user_var, False),
            ("密碼", self.password_var, True),
            ("資料庫名稱", self.database_var, False),
            ("字元編碼", self.charset_var, False),
            ("連線逾時（秒）", self.connect_timeout_var, False),
        )

        for row, (label_text, variable, is_password) in enumerate(fields, start=1):
            ttk.Label(frame, text=label_text).grid(
                row=row,
                column=0,
                sticky="w",
                padx=(0, 10),
                pady=4,
            )
            entry = ttk.Entry(
                frame,
                textvariable=variable,
                show="●" if is_password else "",
            )
            entry.grid(row=row, column=1, sticky="ew", pady=4)

    def _build_write_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="資料寫入設定", padding=12)
        frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        frame.columnconfigure(0, weight=1)

        ttk.Checkbutton(
            frame,
            text="寫入歷史資料",
            variable=self.write_history_var,
        ).grid(row=0, column=0, sticky="w", pady=5)

        ttk.Checkbutton(
            frame,
            text="更新最新資料",
            variable=self.write_latest_var,
        ).grid(row=1, column=0, sticky="w", pady=5)

        ttk.Checkbutton(
            frame,
            text="僅在數值變更時寫入",
            variable=self.write_only_on_change_var,
        ).grid(row=2, column=0, sticky="w", pady=5)

        ttk.Separator(frame, orient="horizontal").grid(
            row=3,
            column=0,
            sticky="ew",
            pady=12,
        )

        ttk.Label(
            frame,
            text=(
                "密碼欄位會交由ConfigManager儲存。"
                "請勿將含有真實密碼的設定檔提交至GitHub。"
            ),
            wraplength=300,
            justify="left",
        ).grid(row=4, column=0, sticky="ew")

    def _build_status_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="資料庫狀態", padding=12)
        frame.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(12, 0),
        )
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="連線狀態：").grid(
            row=0,
            column=0,
            sticky="w",
            pady=3,
        )
        ttk.Label(
            frame,
            textvariable=self.connection_status_var,
            font=("TkDefaultFont", 10, "bold"),
        ).grid(row=0, column=1, sticky="w", pady=3)

        ttk.Label(frame, text="自動上傳：").grid(
            row=1,
            column=0,
            sticky="w",
            pady=3,
        )
        ttk.Label(
            frame,
            textvariable=self.auto_write_status_var,
            font=("TkDefaultFont", 10, "bold"),
        ).grid(row=1, column=1, sticky="w", pady=3)

        ttk.Label(frame, text="目前操作：").grid(
            row=2,
            column=0,
            sticky="w",
            pady=3,
        )
        ttk.Label(
            frame,
            textvariable=self.operation_status_var,
        ).grid(row=2, column=1, sticky="w", pady=3)

    def _build_button_frame(self) -> None:
        frame = ttk.Frame(self)
        frame.grid(row=2, column=0, sticky="ew", padx=12, pady=(6, 12))

        self.save_button = ttk.Button(
            frame,
            text="儲存設定",
            command=self.save_settings,
        )
        self.save_button.grid(row=0, column=0, padx=(0, 6), pady=4)

        self.test_button = ttk.Button(
            frame,
            text="測試連線",
            command=self.test_connection,
        )
        self.test_button.grid(row=0, column=1, padx=6, pady=4)

        self.ensure_tables_button = ttk.Button(
            frame,
            text="自動建立資料表",
            command=self.ensure_tables,
        )
        self.ensure_tables_button.grid(row=0, column=2, padx=6, pady=4)

        self.start_button = ttk.Button(
            frame,
            text="啟動自動上傳",
            command=self.start_auto_write,
        )
        self.start_button.grid(row=0, column=3, padx=6, pady=4)

        self.stop_button = ttk.Button(
            frame,
            text="停止自動上傳",
            command=self.stop_auto_write,
        )
        self.stop_button.grid(row=0, column=4, padx=(6, 0), pady=4)

    def load_settings(self) -> None:
        """從ConfigManager載入database區段。"""
        try:
            config = self._get_database_config()
        except Exception as exc:
            self.operation_status_var.set("載入設定失敗")
            self._log(f"載入資料庫設定失敗：{exc}")
            return

        self.enable_var.set(self._as_bool(config.get("enable", False)))
        self.host_var.set(str(config.get("host", "127.0.0.1")))
        self.port_var.set(str(config.get("port", 3306)))
        self.user_var.set(str(config.get("user", "")))
        self.password_var.set(str(config.get("password", "")))
        self.database_var.set(str(config.get("database", "")))
        self.charset_var.set(str(config.get("charset", "utf8mb4")))
        self.connect_timeout_var.set(str(config.get("connect_timeout", 5)))
        self.write_history_var.set(
            self._as_bool(config.get("write_history", True))
        )
        self.write_latest_var.set(
            self._as_bool(config.get("write_latest", True))
        )
        self.write_only_on_change_var.set(
            self._as_bool(config.get("write_only_on_change", True))
        )
        self.operation_status_var.set("設定已載入")

    def save_settings(self) -> None:
        """驗證並儲存資料庫設定，再重新載入DatabaseManager。"""
        try:
            database_config = self._collect_database_config()
            self._save_database_config(database_config)
            self.database_manager.reload_config()
            self.refresh_all()
        except Exception as exc:
            self.operation_status_var.set("儲存設定失敗")
            self._log(f"儲存資料庫設定失敗：{exc}")
            messagebox.showerror("儲存失敗", str(exc), parent=self)
            return

        self.operation_status_var.set("設定已儲存")
        self._log("資料庫設定已儲存，DatabaseManager已重新載入設定。")
        messagebox.showinfo(
            "儲存完成",
            "資料庫設定已成功儲存。",
            parent=self,
        )

    def test_connection(self) -> None:
        """呼叫DatabaseManager測試資料庫連線。"""
        self.connection_status_var.set("測試中…")
        self._run_manager_action(
            status_text="正在測試資料庫連線…",
            action=self.database_manager.test_connection,
            callback=self._on_test_connection_finished,
        )

    def ensure_tables(self) -> None:
        """呼叫DatabaseManager建立必要資料表。"""
        self._run_manager_action(
            status_text="正在確認並建立資料表…",
            action=self.database_manager.ensure_tables,
            callback=self._on_ensure_tables_finished,
        )

    def start_auto_write(self) -> None:
        """呼叫DatabaseManager啟動自動上傳。"""
        self._run_manager_action(
            status_text="正在啟動自動上傳…",
            action=self.database_manager.start_auto_write,
            callback=self._on_start_auto_write_finished,
        )

    def stop_auto_write(self) -> None:
        """呼叫DatabaseManager停止自動上傳。"""
        self._run_manager_action(
            status_text="正在停止自動上傳…",
            action=self.database_manager.stop_auto_write,
            callback=self._on_stop_auto_write_finished,
        )

    def _collect_database_config(self) -> Dict[str, Any]:
        """驗證UI欄位並建立database設定字典。"""
        host = self.host_var.get().strip()
        charset = self.charset_var.get().strip()

        if not host:
            raise ValueError("主機位址不可為空白。")
        if not charset:
            raise ValueError("字元編碼不可為空白。")

        try:
            port = int(self.port_var.get().strip())
        except ValueError as exc:
            raise ValueError("連接埠必須是整數。") from exc

        if not 1 <= port <= 65535:
            raise ValueError("連接埠必須介於1到65535。")

        try:
            connect_timeout_number = float(
                self.connect_timeout_var.get().strip()
            )
        except ValueError as exc:
            raise ValueError("連線逾時必須是數字。") from exc

        if connect_timeout_number <= 0:
            raise ValueError("連線逾時必須大於0秒。")

        connect_timeout: Any
        if connect_timeout_number.is_integer():
            connect_timeout = int(connect_timeout_number)
        else:
            connect_timeout = connect_timeout_number

        return {
            "enable": bool(self.enable_var.get()),
            "host": host,
            "port": port,
            "user": self.user_var.get().strip(),
            "password": self.password_var.get(),
            "database": self.database_var.get().strip(),
            "charset": charset,
            "connect_timeout": connect_timeout,
            "write_history": bool(self.write_history_var.get()),
            "write_latest": bool(self.write_latest_var.get()),
            "write_only_on_change": bool(
                self.write_only_on_change_var.get()
            ),
        }

    def _get_database_config(self) -> Dict[str, Any]:
        """相容常見ConfigManager讀取介面。"""
        manager = self.config_manager

        get_section = getattr(manager, "get_section", None)
        if callable(get_section):
            section = get_section("database")
            return dict(section) if isinstance(section, dict) else {}

        get_config = getattr(manager, "get_config", None)
        if callable(get_config):
            config = get_config()
            if isinstance(config, dict):
                section = config.get("database", {})
                return dict(section) if isinstance(section, dict) else {}

        get_value = getattr(manager, "get", None)
        if callable(get_value):
            try:
                section = get_value("database", {})
            except TypeError:
                section = get_value("database")
            return dict(section) if isinstance(section, dict) else {}

        config = getattr(manager, "config", None)
        if isinstance(config, dict):
            section = config.get("database", {})
            return dict(section) if isinstance(section, dict) else {}

        raise AttributeError("ConfigManager未提供可用的設定讀取介面。")

    def _save_database_config(self, database_config: Dict[str, Any]) -> None:
        """相容常見ConfigManager寫入與儲存介面。"""
        manager = self.config_manager

        update_section = getattr(manager, "update_section", None)
        if callable(update_section):
            update_section("database", database_config)
            self._call_save_without_arguments()
            return

        set_section = getattr(manager, "set_section", None)
        if callable(set_section):
            set_section("database", database_config)
            self._call_save_without_arguments()
            return

        config = self._get_full_config()
        config["database"] = database_config

        save_config = getattr(manager, "save_config", None)
        if callable(save_config):
            try:
                save_config(config)
            except TypeError:
                self._replace_manager_config(config)
                save_config()
            return

        save = getattr(manager, "save", None)
        if callable(save):
            try:
                save(config)
            except TypeError:
                self._replace_manager_config(config)
                save()
            return

        set_value = getattr(manager, "set", None)
        if callable(set_value):
            set_value("database", database_config)
            self._call_save_without_arguments()
            return

        if isinstance(getattr(manager, "config", None), dict):
            self._replace_manager_config(config)
            self._call_save_without_arguments()
            return

        raise AttributeError("ConfigManager未提供可用的設定儲存介面。")

    def _get_full_config(self) -> Dict[str, Any]:
        manager = self.config_manager

        get_config = getattr(manager, "get_config", None)
        if callable(get_config):
            config = get_config()
            return dict(config) if isinstance(config, dict) else {}

        config = getattr(manager, "config", None)
        if isinstance(config, dict):
            return dict(config)

        load_config = getattr(manager, "load_config", None)
        if callable(load_config):
            config = load_config()
            return dict(config) if isinstance(config, dict) else {}

        load = getattr(manager, "load", None)
        if callable(load):
            config = load()
            return dict(config) if isinstance(config, dict) else {}

        return {}

    def _replace_manager_config(self, config: Dict[str, Any]) -> None:
        current = getattr(self.config_manager, "config", None)
        if not isinstance(current, dict):
            raise AttributeError("ConfigManager沒有可更新的config字典。")
        current.clear()
        current.update(config)

    def _call_save_without_arguments(self) -> None:
        for method_name in ("save_config", "save"):
            method = getattr(self.config_manager, method_name, None)
            if callable(method):
                try:
                    method()
                except TypeError:
                    continue
                return

    def _run_manager_action(
        self,
        status_text: str,
        action: Callable[[], Any],
        callback: Callable[[Any, Optional[BaseException]], None],
    ) -> None:
        """在背景執行DatabaseManager操作，避免凍結Tkinter。"""
        if self._operation_running:
            messagebox.showwarning(
                "操作進行中",
                "目前已有資料庫操作正在執行，請稍後再試。",
                parent=self,
            )
            return

        self._operation_running = True
        self.operation_status_var.set(status_text)
        self._set_buttons_enabled(False)

        def worker() -> None:
            result: Any = None
            error: Optional[BaseException] = None
            try:
                result = action()
            except BaseException as exc:
                error = exc

            try:
                self.after(
                    0,
                    lambda: self._finish_manager_action(
                        callback,
                        result,
                        error,
                    ),
                )
            except tk.TclError:
                return

        threading.Thread(target=worker, daemon=True).start()

    def _finish_manager_action(
        self,
        callback: Callable[[Any, Optional[BaseException]], None],
        result: Any,
        error: Optional[BaseException],
    ) -> None:
        self._operation_running = False
        self._set_buttons_enabled(True)
        callback(result, error)

    def _on_test_connection_finished(
        self,
        result: Any,
        error: Optional[BaseException],
    ) -> None:
        success, message = self._normalise_result(
            result,
            error,
            "資料庫連線成功",
            "資料庫連線失敗",
        )
        self.connection_status_var.set(
            "連線成功" if success else "連線失敗"
        )
        self.operation_status_var.set(message)
        self._log(message)

        dialog = messagebox.showinfo if success else messagebox.showerror
        dialog("測試連線", message, parent=self)

    def _on_ensure_tables_finished(
        self,
        result: Any,
        error: Optional[BaseException],
    ) -> None:
        success, message = self._normalise_result(
            result,
            error,
            "資料表確認與建立完成",
            "資料表建立失敗",
        )
        self.operation_status_var.set(message)
        self._log(message)

        dialog = messagebox.showinfo if success else messagebox.showerror
        dialog("資料表作業", message, parent=self)

    def _on_start_auto_write_finished(
        self,
        result: Any,
        error: Optional[BaseException],
    ) -> None:
        success, message = self._normalise_result(
            result,
            error,
            "自動上傳已啟動",
            "啟動自動上傳失敗",
        )
        self._local_auto_write_running = success
        self.auto_write_status_var.set(
            "執行中" if success else "啟動失敗"
        )
        self.operation_status_var.set(message)
        self._log(message)

        if not success:
            messagebox.showerror("自動上傳", message, parent=self)

    def _on_stop_auto_write_finished(
        self,
        result: Any,
        error: Optional[BaseException],
    ) -> None:
        success, message = self._normalise_result(
            result,
            error,
            "自動上傳已停止",
            "停止自動上傳失敗",
        )
        if success:
            self._local_auto_write_running = False
        self.auto_write_status_var.set(
            "已停止" if success else "停止失敗"
        )
        self.operation_status_var.set(message)
        self._log(message)

        if not success:
            messagebox.showerror("自動上傳", message, parent=self)

    def _normalise_result(
        self,
        result: Any,
        error: Optional[BaseException],
        success_message: str,
        failure_message: str,
    ) -> Tuple[bool, str]:
        """統一DatabaseManager可能使用的回傳格式。"""
        if error is not None:
            return False, f"{failure_message}：{error}"

        if isinstance(result, tuple) and result:
            success = bool(result[0])
            detail = str(result[1]) if len(result) > 1 and result[1] else ""
            return (
                success,
                detail or (success_message if success else failure_message),
            )

        if isinstance(result, dict):
            status = result.get(
                "success",
                result.get("ok", result.get("status")),
            )
            success = self._as_bool(status) if status is not None else True
            detail = result.get("message") or result.get("error") or ""
            return (
                success,
                str(detail) or (
                    success_message if success else failure_message
                ),
            )

        if isinstance(result, bool):
            return (
                result,
                success_message if result else failure_message,
            )

        if isinstance(result, str):
            text = result.strip()
            lowered = text.lower()
            failed = any(
                keyword in lowered
                for keyword in ("fail", "error", "失敗", "錯誤")
            )
            return (
                not failed,
                text or (failure_message if failed else success_message),
            )

        if result is None:
            return True, success_message

        success = bool(result)
        return (
            success,
            success_message if success else failure_message,
        )

    def _schedule_status_refresh(self) -> None:
        self._refresh_auto_write_status()
        try:
            self._status_after_id = self.after(
                1000,
                self._schedule_status_refresh,
            )
        except tk.TclError:
            self._status_after_id = None

    def _refresh_auto_write_status(self) -> None:
        running = self._query_auto_write_status()
        if running is None:
            running = self._local_auto_write_running

        self.auto_write_status_var.set(
            "執行中" if running else "已停止"
        )

    def _query_auto_write_status(self) -> Optional[bool]:
        """在不要求額外公開介面的前提下讀取管理器狀態。"""
        manager = self.database_manager

        for method_name in (
            "is_auto_write_running",
            "is_auto_writing",
            "is_running",
        ):
            method = getattr(manager, method_name, None)
            if callable(method):
                try:
                    return bool(method())
                except Exception:
                    return None

        for attribute_name in (
            "auto_write_running",
            "auto_writing",
            "_auto_write_running",
        ):
            if hasattr(manager, attribute_name):
                return bool(getattr(manager, attribute_name))

        return None

    def _set_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in (
            self.save_button,
            self.test_button,
            self.ensure_tables_button,
            self.start_button,
            self.stop_button,
        ):
            button.configure(state=state)

    def _log(self, message: str) -> None:
        try:
            self.log_func(message)
        except Exception:
            pass

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
                "enable",
                "enabled",
                "啟用",
                "是",
            }
        return bool(value)

    def destroy(self) -> None:
        """取消狀態更新排程後銷毀頁面。"""
        if self._status_after_id is not None:
            try:
                self.after_cancel(self._status_after_id)
            except tk.TclError:
                pass
            self._status_after_id = None
        super().destroy()
