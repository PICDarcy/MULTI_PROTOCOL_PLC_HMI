"""Tests for automatic database and table provisioning.

These tests deliberately provide a fake ``pymysql`` module.  They exercise the
DatabaseManager contract without requiring a MySQL server, the PyMySQL package,
or a Tkinter root window.
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _load_pymysql_for_tests():
    """Return PyMySQL, or install the smallest useful fake when it is absent."""

    try:
        import pymysql  # type: ignore
    except ImportError:
        pymysql = types.ModuleType("pymysql")
        pymysql.__path__ = []  # Make imports such as ``pymysql.err`` work.

        err_module = types.ModuleType("pymysql.err")

        class OperationalError(Exception):
            pass

        err_module.OperationalError = OperationalError
        pymysql.err = err_module
        pymysql.OperationalError = OperationalError
        pymysql.connect = MagicMock(name="pymysql.connect")

        sys.modules["pymysql"] = pymysql
        sys.modules["pymysql.err"] = err_module

    return pymysql


pymysql = _load_pymysql_for_tests()

from core.database_manager import DatabaseManager


class DatabaseManagerMissingDatabaseTests(unittest.TestCase):
    DATABASE_NAME = "plant`main"

    def setUp(self):
        self.config = {
            "enable": True,
            "host": "db.internal",
            "port": 3307,
            "user": "operator",
            "password": "secret",
            "database": self.DATABASE_NAME,
            "charset": "utf8mb4",
            "connect_timeout": 3,
        }
        self.config_manager = MagicMock()
        self.config_manager.get_section.return_value = dict(self.config)
        self.value_bus = MagicMock()
        self.log_func = MagicMock()
        self.manager = DatabaseManager(
            self.config_manager,
            self.value_bus,
            self.log_func,
        )

        self.connect_patcher = patch.object(pymysql, "connect")
        self.connect = self.connect_patcher.start()
        self.addCleanup(self.connect_patcher.stop)

    @staticmethod
    def _connection():
        connection = MagicMock()
        cursor = MagicMock()
        cursor_context = connection.cursor.return_value
        cursor_context.__enter__.return_value = cursor
        cursor_context.__exit__.return_value = False
        return connection, cursor

    @staticmethod
    def _is_success(result):
        if isinstance(result, dict):
            return bool(result.get("success", result.get("ok", False)))
        if isinstance(result, tuple):
            return bool(result[0]) if result else False
        return bool(result)

    def assert_cancelled(self, result):
        self.assertIsInstance(result, tuple)
        self.assertEqual(2, len(result))
        success, message = result
        self.assertIs(success, False)
        self.assertIsInstance(message, str)
        self.assertTrue(message)
        self.assertIs(getattr(result, "cancelled", None), True)
        self.assertEqual(
            "database_creation_declined",
            getattr(result, "reason", None),
        )
        self.assertEqual(
            self.DATABASE_NAME,
            getattr(result, "database", None),
        )

    @staticmethod
    def _executed_sql(cursor):
        return [
            call.args[0]
            for call in cursor.execute.call_args_list
            if call.args and isinstance(call.args[0], str)
        ]

    @staticmethod
    def _missing_database_error():
        return pymysql.err.OperationalError(1049, "Unknown database")

    def _route_missing_database_once(self):
        """Fail target-DB connections until the server-level create path runs."""

        server_connection, server_cursor = self._connection()
        database_connection, database_cursor = self._connection()
        database_created = False

        def connect(**kwargs):
            nonlocal database_created
            if kwargs.get("database") and not database_created:
                raise self._missing_database_error()
            if not kwargs.get("database"):
                database_created = True
                return server_connection
            return database_connection

        self.connect.side_effect = connect
        return (
            server_connection,
            server_cursor,
            database_connection,
            database_cursor,
        )

    def test_accepting_prompt_creates_quoted_database_and_both_tables(self):
        (
            server_connection,
            server_cursor,
            database_connection,
            database_cursor,
        ) = self._route_missing_database_once()
        handler = MagicMock(return_value=True)
        self.manager.set_missing_database_handler(handler)

        result = self.manager.test_connection()

        self.assertTrue(self._is_success(result), result)
        handler.assert_called_once_with(self.DATABASE_NAME)

        server_connect_calls = [
            call
            for call in self.connect.call_args_list
            if "database" not in call.kwargs
        ]
        self.assertEqual(1, len(server_connect_calls))
        server_kwargs = server_connect_calls[0].kwargs
        self.assertEqual("db.internal", server_kwargs["host"])
        self.assertEqual(3307, server_kwargs["port"])
        self.assertNotIn("database", server_kwargs)

        create_database_sql = [
            sql
            for sql in self._executed_sql(server_cursor)
            if "CREATE DATABASE" in sql.upper()
        ]
        self.assertEqual(1, len(create_database_sql))
        self.assertIn("`plant``main`", create_database_sql[0])
        server_connection.close.assert_called()

        table_sql = "\n".join(self._executed_sql(database_cursor)).lower()
        self.assertIn("create table if not exists plc_point_history", table_sql)
        self.assertIn("create table if not exists plc_point_latest", table_sql)
        database_connection.commit.assert_called()
        database_connection.close.assert_called()

    def test_declining_prompt_cancels_each_entry_point_without_creating(self):
        for method_name in (
            "test_connection",
            "ensure_tables",
            "start_auto_write",
        ):
            with self.subTest(method=method_name):
                manager = DatabaseManager(
                    self.config_manager,
                    self.value_bus,
                    self.log_func,
                )
                handler = MagicMock(return_value=False)
                manager.set_missing_database_handler(handler)
                self.connect.reset_mock()
                self.connect.side_effect = self._missing_database_error()
                self.value_bus.reset_mock()

                result = getattr(manager, method_name)()

                self.assert_cancelled(result)
                handler.assert_called_once_with(self.DATABASE_NAME)
                self.assertGreaterEqual(self.connect.call_count, 1)
                self.assertTrue(
                    all("database" in call.kwargs for call in self.connect.call_args_list)
                )
                self.value_bus.subscribe.assert_not_called()
                self.assertFalse(manager.is_auto_write_running())

    def test_only_operational_error_1049_prompts(self):
        errors = (
            pymysql.err.OperationalError(1044, "Access denied"),
            RuntimeError(1049, "This is not a PyMySQL OperationalError"),
        )

        for error in errors:
            with self.subTest(error=repr(error)):
                manager = DatabaseManager(
                    self.config_manager,
                    self.value_bus,
                    self.log_func,
                )
                handler = MagicMock(return_value=True)
                manager.set_missing_database_handler(handler)
                self.connect.reset_mock()
                self.connect.side_effect = error

                result = manager.test_connection()

                self.assertFalse(self._is_success(result), result)
                handler.assert_not_called()
                self.assertEqual(1, self.connect.call_count)

    def test_prompt_handler_exception_is_an_ordinary_failure(self):
        handler = MagicMock(side_effect=RuntimeError("prompt unavailable"))
        self.manager.set_missing_database_handler(handler)
        self.connect.side_effect = self._missing_database_error()

        result = self.manager.test_connection()

        self.assertIsInstance(result, tuple)
        self.assertEqual(2, len(result))
        success, message = result
        self.assertIs(success, False)
        self.assertTrue(message)
        self.assertFalse(getattr(result, "cancelled", False))
        handler.assert_called_once_with(self.DATABASE_NAME)
        self.assertTrue(
            all("database" in call.kwargs for call in self.connect.call_args_list)
        )

    def test_existing_database_test_connection_automatically_ensures_tables(self):
        connection, cursor = self._connection()
        self.connect.return_value = connection
        handler = MagicMock(return_value=True)
        self.manager.set_missing_database_handler(handler)

        result = self.manager.test_connection()

        self.assertTrue(self._is_success(result), result)
        handler.assert_not_called()
        sql = "\n".join(self._executed_sql(cursor)).lower()
        self.assertIn("create table if not exists plc_point_history", sql)
        self.assertIn("create table if not exists plc_point_latest", sql)
        connection.commit.assert_called()

    def test_start_auto_write_automatically_ensures_tables(self):
        connection, cursor = self._connection()
        self.connect.return_value = connection
        handler = MagicMock(return_value=True)
        self.manager.set_missing_database_handler(handler)
        worker = MagicMock()
        worker.is_alive.return_value = True

        with patch(
            "core.database_manager.threading.Thread",
            return_value=worker,
        ) as thread_class:
            result = self.manager.start_auto_write()

        self.assertTrue(self._is_success(result), result)
        handler.assert_not_called()
        sql = "\n".join(self._executed_sql(cursor)).lower()
        self.assertIn("create table if not exists plc_point_history", sql)
        self.assertIn("create table if not exists plc_point_latest", sql)
        connection.commit.assert_called()
        self.value_bus.subscribe.assert_called_once()
        thread_class.assert_called_once()
        worker.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
