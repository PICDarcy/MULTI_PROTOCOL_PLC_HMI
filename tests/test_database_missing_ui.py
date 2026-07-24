"""UI contract tests for the missing-database prompt and cancellation flow."""

from __future__ import annotations

import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch

from ui.app import App
from ui.database_page import DatabasePage


class _CancelledResult(tuple):
    cancelled = True
    reason = "database_creation_declined"
    database = "factory_db"

    def __new__(cls):
        return super().__new__(cls, (False, '未建立資料庫"factory_db"'))


class MissingDatabasePromptTests(unittest.TestCase):
    def test_ui_thread_prompt_uses_exact_text_and_returns_yes_or_no(self):
        app = object.__new__(App)
        app._closing = False
        app._ui_thread_id = 8675309
        app.log_func = MagicMock()

        for user_answer in (True, False):
            with self.subTest(user_answer=user_answer):
                with (
                    patch(
                        "ui.app.threading.get_ident",
                        return_value=app._ui_thread_id,
                    ),
                    patch(
                        "ui.app.messagebox.askyesno",
                        return_value=user_answer,
                    ) as askyesno,
                ):
                    result = app._ask_to_create_missing_database("factory_db")

                self.assertIs(result, user_answer)
                askyesno.assert_called_once_with(
                    "建立資料庫",
                    '偵測不到資料庫是否建立資料庫"factory_db"',
                    parent=app,
                )

    def test_prompt_error_is_raised_instead_of_becoming_no(self):
        app = object.__new__(App)
        app._closing = False
        app._ui_thread_id = 8675309
        app.log_func = MagicMock()

        with (
            patch(
                "ui.app.threading.get_ident",
                return_value=app._ui_thread_id,
            ),
            patch(
                "ui.app.messagebox.askyesno",
                side_effect=tk.TclError("dialog unavailable"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                app._ask_to_create_missing_database("factory_db")


class DatabasePageCancellationTests(unittest.TestCase):
    def setUp(self):
        self.page = object.__new__(DatabasePage)
        self.page.connection_status_var = MagicMock()
        self.page.operation_status_var = MagicMock()
        self.page.auto_write_status_var = MagicMock()
        self.page._local_auto_write_running = True
        self.page.log_func = MagicMock()
        self.cancelled_result = _CancelledResult()

    def test_cancelled_actions_do_not_show_a_second_dialog(self):
        callbacks = (
            "_on_test_connection_finished",
            "_on_ensure_tables_finished",
            "_on_start_auto_write_finished",
        )

        with (
            patch("ui.database_page.messagebox.showerror") as showerror,
            patch("ui.database_page.messagebox.showinfo") as showinfo,
        ):
            for callback_name in callbacks:
                with self.subTest(callback=callback_name):
                    getattr(self.page, callback_name)(
                        self.cancelled_result,
                        None,
                    )

        showerror.assert_not_called()
        showinfo.assert_not_called()
        self.page.connection_status_var.set.assert_called_with("未建立資料庫")
        self.page.auto_write_status_var.set.assert_called_with("已停止")
        self.assertFalse(self.page._local_auto_write_running)


if __name__ == "__main__":
    unittest.main()
