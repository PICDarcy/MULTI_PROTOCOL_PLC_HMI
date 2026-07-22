"""MySQL資料庫管理模組。

此模組只接收PointValue資料，不直接處理Modbus、OPC UA或UI。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

try:
    import pymysql
except ImportError:
    pymysql = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from core.data_model import PointValue


class DatabaseManager:
    """負責MySQL連線、資料表建立與PointValue寫入。"""

    _VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    _COLUMN_NAMES = (
        "point_key, protocol, source_name, device_name, point_name, "
        "address_text, value, value_text, value_number, status_text, "
        "timestamp, writable, data_type, raw_config"
    )
    _COLUMN_VALUES = (
        "%(point_key)s, %(protocol)s, %(source_name)s, %(device_name)s, "
        "%(point_name)s, %(address_text)s, %(value)s, %(value_text)s, "
        "%(value_number)s, %(status_text)s, %(timestamp)s, %(writable)s, "
        "%(data_type)s, %(raw_config)s"
    )

    def __init__(
        self,
        config_manager: Any,
        value_bus: Any = None,
        log_callback: Optional[Callable[..., None]] = None,
    ) -> None:
        self.config_manager = config_manager
        self.value_bus = value_bus
        self.log_callback = log_callback

        self._config: dict[str, Any] = {}
        self._subscribed = False
        self._tables_ready = False
        self._lock = threading.RLock()
        self._last_values: dict[str, tuple[Any, ...]] = {}

        self.reload_config()

    def test_connection(self) -> bool:
        """測試目前database設定是否可以連線MySQL。"""
        connection = None
        try:
            connection = self._connect()
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                result = cursor.fetchone()

            success = bool(result and int(result[0]) == 1)
            if success:
                self._log("INFO", "MySQL連線測試成功。")
            else:
                self._log("WARNING", "MySQL連線測試結果異常。")
            return success
        except Exception as exc:
            self._log("ERROR", f"MySQL連線測試失敗：{exc}")
            return False
        finally:
            self._close(connection)

    def ensure_tables(self) -> bool:
        """依設定建立歷史資料表及最新值資料表。"""
        if not self._database_enabled():
            self._log("INFO", "database.enable=False，略過資料表建立。")
            return False

        if not self._write_history_enabled() and not self._write_latest_enabled():
            self._log("WARNING", "write_history與write_latest皆未啟用。")
            return False

        connection = None
        try:
            connection = self._connect()
            with connection.cursor() as cursor:
                if self._write_history_enabled():
                    cursor.execute(self._create_history_table_sql())
                if self._write_latest_enabled():
                    cursor.execute(self._create_latest_table_sql())

            connection.commit()
            self._tables_ready = True
            self._log("INFO", "MySQL資料表確認完成。")
            return True
        except Exception as exc:
            self._tables_ready = False
            self._rollback(connection)
            self._log("ERROR", f"建立MySQL資料表失敗：{exc}")
            return False
        finally:
            self._close(connection)

    def write_point_value(self, point_value: "PointValue") -> bool:
        """將一筆PointValue寫入啟用的資料表。"""
        try:
            if point_value is None:
                return False

            if not self._database_enabled():
                return False

            if not self._point_database_enabled(point_value):
                return False

            if not self._write_history_enabled() and not self._write_latest_enabled():
                return False

            point_key = str(getattr(point_value, "point_key", "")).strip()
            if not point_key:
                self._log("WARNING", "PointValue.point_key為空，略過資料庫寫入。")
                return False

            signature = self._make_change_signature(point_value)
            with self._lock:
                if (
                    self._write_only_on_change_enabled()
                    and self._last_values.get(point_key) == signature
                ):
                    return False

            if self._auto_ensure_tables_enabled() and not self._tables_ready:
                if not self.ensure_tables():
                    return False

            row = self._point_to_row(point_value)
            connection = None

            with self._lock:
                try:
                    connection = self._connect()
                    with connection.cursor() as cursor:
                        if self._write_history_enabled():
                            cursor.execute(self._history_insert_sql(), row)

                        if self._write_latest_enabled():
                            cursor.execute(self._latest_upsert_sql(), row)

                    connection.commit()
                    self._last_values[point_key] = signature
                    return True
                except Exception:
                    self._rollback(connection)
                    raise
                finally:
                    self._close(connection)
        except Exception as exc:
            self._log("ERROR", f"寫入PointValue失敗：{exc}")
            return False

    def start_auto_write(self) -> bool:
        """訂閱ValueBus，收到PointValue時自動寫入資料庫。"""
        with self._lock:
            if self._subscribed:
                return True

            if self.value_bus is None:
                self._log("ERROR", "未提供ValueBus，無法啟動自動寫入。")
                return False

            subscribe = getattr(self.value_bus, "subscribe", None)
            if not callable(subscribe):
                self._log("ERROR", "ValueBus缺少subscribe(callback)介面。")
                return False

            try:
                subscribe(self._on_point_value)
                self._subscribed = True
                self._log("INFO", "資料庫自動寫入已啟動。")
                return True
            except Exception as exc:
                self._log("ERROR", f"訂閱ValueBus失敗：{exc}")
                return False

    def stop_auto_write(self) -> bool:
        """取消訂閱ValueBus並停止自動寫入。"""
        with self._lock:
            if not self._subscribed:
                return True

            unsubscribe = getattr(self.value_bus, "unsubscribe", None)
            if not callable(unsubscribe):
                self._log("ERROR", "ValueBus缺少unsubscribe(callback)介面。")
                return False

            try:
                unsubscribe(self._on_point_value)
                self._subscribed = False
                self._log("INFO", "資料庫自動寫入已停止。")
                return True
            except Exception as exc:
                self._log("ERROR", f"取消訂閱ValueBus失敗：{exc}")
                return False

    def reload_config(self) -> None:
        """重新從config_manager讀取database設定。"""
        try:
            config = self._read_database_config()
            self._config = dict(config) if isinstance(config, Mapping) else {}
            self._tables_ready = False

            with self._lock:
                self._last_values.clear()

            self._log("INFO", "資料庫設定已重新載入。")
        except Exception as exc:
            self._config = {}
            self._tables_ready = False
            self._log("ERROR", f"載入資料庫設定失敗：{exc}")

    def _on_point_value(self, point_value: "PointValue") -> None:
        """ValueBus訂閱回呼。"""
        try:
            self.write_point_value(point_value)
        except Exception as exc:
            self._log("ERROR", f"資料庫自動寫入回呼失敗：{exc}")

    def _read_database_config(self) -> Mapping[str, Any]:
        manager = self.config_manager
        if manager is None:
            return {}

        method_candidates = (
            ("get_database_config", ()),
            ("get_section", ("database",)),
            ("get", ("database", {})),
            ("get", ("database",)),
        )

        for method_name, arguments in method_candidates:
            method = getattr(manager, method_name, None)
            if not callable(method):
                continue

            try:
                result = method(*arguments)
                if isinstance(result, Mapping):
                    return result
            except Exception:
                continue

        get_config = getattr(manager, "get_config", None)
        if callable(get_config):
            try:
                full_config = get_config()
                if isinstance(full_config, Mapping):
                    database_config = full_config.get("database")
                    if isinstance(database_config, Mapping):
                        return database_config
            except Exception:
                pass

        for attribute_name in ("config", "data", "settings"):
            full_config = getattr(manager, attribute_name, None)
            if isinstance(full_config, Mapping):
                database_config = full_config.get("database")
                if isinstance(database_config, Mapping):
                    return database_config

        return {}

    def _connect(self) -> Any:
        if pymysql is None:
            raise RuntimeError("尚未安裝pymysql，請先執行pip install pymysql。")

        database_name = str(
            self._config.get("database", self._config.get("name", ""))
        ).strip()
        if not database_name:
            raise ValueError("database.database未設定。")

        return pymysql.connect(
            host=str(self._config.get("host", "127.0.0.1")),
            port=self._to_int(self._config.get("port", 3306), 3306, 1, 65535),
            user=str(
                self._config.get("user", self._config.get("username", "root"))
            ),
            password=str(self._config.get("password", "")),
            database=database_name,
            charset=str(self._config.get("charset", "utf8mb4")),
            connect_timeout=self._to_int(
                self._config.get("connect_timeout", 5), 5, 1, 120
            ),
            autocommit=False,
        )

    def _create_history_table_sql(self) -> str:
        return f"""
        CREATE TABLE IF NOT EXISTS `{self._history_table_name()}` (
            `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            {self._common_columns_sql()},
            `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            PRIMARY KEY (`id`),
            KEY `idx_point_time` (`point_key`, `timestamp`),
            KEY `idx_protocol_time` (`protocol`, `timestamp`)
        ) ENGINE=InnoDB
          DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """

    def _create_latest_table_sql(self) -> str:
        return f"""
        CREATE TABLE IF NOT EXISTS `{self._latest_table_name()}` (
            {self._common_columns_sql()},
            `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                ON UPDATE CURRENT_TIMESTAMP(6),
            PRIMARY KEY (`point_key`),
            KEY `idx_protocol` (`protocol`),
            KEY `idx_device` (`device_name`),
            KEY `idx_timestamp` (`timestamp`)
        ) ENGINE=InnoDB
          DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """

    @staticmethod
    def _common_columns_sql() -> str:
        return """
            `point_key` VARCHAR(255) NOT NULL,
            `protocol` VARCHAR(32) NOT NULL,
            `source_name` VARCHAR(255) NOT NULL DEFAULT '',
            `device_name` VARCHAR(255) NOT NULL DEFAULT '',
            `point_name` VARCHAR(255) NOT NULL DEFAULT '',
            `address_text` VARCHAR(512) NOT NULL DEFAULT '',
            `value` LONGTEXT NULL,
            `value_text` LONGTEXT NULL,
            `value_number` DOUBLE NULL,
            `status_text` VARCHAR(255) NOT NULL DEFAULT '',
            `timestamp` DATETIME(6) NOT NULL,
            `writable` TINYINT(1) NOT NULL DEFAULT 0,
            `data_type` VARCHAR(64) NOT NULL DEFAULT '',
            `raw_config` LONGTEXT NULL
        """.strip()

    def _history_insert_sql(self) -> str:
        return (
            f"INSERT INTO `{self._history_table_name()}` "
            f"({self._COLUMN_NAMES}) VALUES ({self._COLUMN_VALUES})"
        )

    def _latest_upsert_sql(self) -> str:
        update_columns = (
            "protocol",
            "source_name",
            "device_name",
            "point_name",
            "address_text",
            "value",
            "value_text",
            "value_number",
            "status_text",
            "timestamp",
            "writable",
            "data_type",
            "raw_config",
        )
        update_sql = ", ".join(
            f"{column}=VALUES({column})" for column in update_columns
        )

        return (
            f"INSERT INTO `{self._latest_table_name()}` "
            f"({self._COLUMN_NAMES}) VALUES ({self._COLUMN_VALUES}) "
            f"ON DUPLICATE KEY UPDATE {update_sql}, "
            "updated_at=CURRENT_TIMESTAMP(6)"
        )

    def _point_to_row(self, point_value: "PointValue") -> dict[str, Any]:
        protocol = str(getattr(point_value, "protocol", ""))
        if protocol not in {"MODBUS_RTU", "OPCUA"}:
            self._log(
                "WARNING",
                f"PointValue.protocol不是MODBUS_RTU或OPCUA：{protocol}",
            )

        return {
            "point_key": str(getattr(point_value, "point_key", "")),
            "protocol": protocol,
            "source_name": str(getattr(point_value, "source_name", "")),
            "device_name": str(getattr(point_value, "device_name", "")),
            "point_name": str(getattr(point_value, "point_name", "")),
            "address_text": str(getattr(point_value, "address_text", "")),
            "value": self._serialize_value(getattr(point_value, "value", None)),
            "value_text": self._to_text(
                getattr(point_value, "value_text", None)
            ),
            "value_number": self._to_number(
                getattr(point_value, "value_number", None)
            ),
            "status_text": str(getattr(point_value, "status_text", "")),
            "timestamp": self._to_timestamp(
                getattr(point_value, "timestamp", None)
            ),
            "writable": 1 if bool(getattr(point_value, "writable", False)) else 0,
            "data_type": str(getattr(point_value, "data_type", "")),
            "raw_config": self._serialize_json(
                getattr(point_value, "raw_config", {})
            ),
        }

    def _make_change_signature(self, point_value: "PointValue") -> tuple[Any, ...]:
        return (
            self._serialize_value(getattr(point_value, "value", None)),
            self._to_text(getattr(point_value, "value_text", None)),
            self._to_number(getattr(point_value, "value_number", None)),
            str(getattr(point_value, "status_text", "")),
            str(getattr(point_value, "data_type", "")),
        )

    def _point_database_enabled(self, point_value: "PointValue") -> bool:
        raw_config = getattr(point_value, "raw_config", {})
        if isinstance(raw_config, Mapping) and "db_enable" in raw_config:
            return self._to_bool(raw_config.get("db_enable"), True)
        return True

    def _database_enabled(self) -> bool:
        return self._to_bool(self._config.get("enable"), False)

    def _write_history_enabled(self) -> bool:
        return self._to_bool(self._config.get("write_history"), True)

    def _write_latest_enabled(self) -> bool:
        return self._to_bool(self._config.get("write_latest"), True)

    def _write_only_on_change_enabled(self) -> bool:
        return self._to_bool(self._config.get("write_only_on_change"), False)

    def _auto_ensure_tables_enabled(self) -> bool:
        return self._to_bool(self._config.get("auto_ensure_tables"), True)

    def _history_table_name(self) -> str:
        value = self._config.get(
            "history_table",
            self._config.get("table_history", "point_value_history"),
        )
        return self._safe_identifier(value, "point_value_history")

    def _latest_table_name(self) -> str:
        value = self._config.get(
            "latest_table",
            self._config.get("table_latest", "point_value_latest"),
        )
        return self._safe_identifier(value, "point_value_latest")

    def _safe_identifier(self, value: Any, default: str) -> str:
        name = str(value or default).strip()
        if not self._VALID_IDENTIFIER.fullmatch(name):
            self._log(
                "WARNING",
                f"資料表名稱「{name}」不合法，改用「{default}」。",
            )
            return default
        return name

    @staticmethod
    def _to_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on", "enable", "enabled"}:
                return True
            if normalized in {
                "false",
                "0",
                "no",
                "off",
                "disable",
                "disabled",
                "",
            }:
                return False
        return default

    @staticmethod
    def _to_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(maximum, number))

    @staticmethod
    def _serialize_value(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool, Decimal)):
            return str(value)

        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                default=str,
                sort_keys=True,
            )
        except Exception:
            return str(value)

    @staticmethod
    def _serialize_json(value: Any) -> Optional[str]:
        if value is None:
            return None

        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                default=str,
                sort_keys=True,
            )
        except Exception:
            return json.dumps(str(value), ensure_ascii=False)

    @staticmethod
    def _to_text(value: Any) -> Optional[str]:
        if value is None:
            return None
        return str(value)

    @staticmethod
    def _to_number(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None

        try:
            if isinstance(value, bool):
                return float(int(value))
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _to_timestamp(value: Any) -> datetime:
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value.astimezone(timezone.utc).replace(tzinfo=None)
            return value

        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value, timezone.utc).replace(tzinfo=None)
            except (OverflowError, OSError, ValueError):
                return datetime.now()

        if isinstance(value, str) and value.strip():
            normalized = value.strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
                if parsed.tzinfo is not None:
                    return parsed.astimezone(timezone.utc).replace(tzinfo=None)
                return parsed
            except ValueError:
                pass

        return datetime.now()

    @staticmethod
    def _rollback(connection: Any) -> None:
        if connection is None:
            return
        try:
            connection.rollback()
        except Exception:
            pass

    @staticmethod
    def _close(connection: Any) -> None:
        if connection is None:
            return
        try:
            connection.close()
        except Exception:
            pass

    def _log(self, level: str, message: str) -> None:
        if self.log_callback is None:
            return

        try:
            self.log_callback(f"[資料庫][{level}] {message}")
        except TypeError:
            try:
                self.log_callback(level, message)
            except Exception:
                pass
        except Exception:
            pass
