"""MySQL/MariaDB點位資料寫入管理。"""

from __future__ import annotations

import json
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .data_model import PointValue


class DatabaseManager:
    """接收PointValue並寫入歷史表及最新值表。"""

    def __init__(self, config_manager, value_bus, log_func=None):
        self.config_manager = config_manager
        self.value_bus = value_bus
        self.log_func = log_func
        self._lock = threading.RLock()
        self._queue: queue.Queue[PointValue | None] = queue.Queue(maxsize=10000)
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._auto_write_running = False
        self._last_signature: dict[str, tuple[Any, ...]] = {}
        self._config: dict[str, Any] = {}
        self.reload_config()

    @property
    def auto_write_running(self) -> bool:
        return self.is_auto_write_running()

    def _log(self, message: str, level: str = "INFO") -> None:
        if callable(self.log_func):
            try:
                self.log_func(message, level)
            except TypeError:
                self.log_func(message)

    @staticmethod
    def _is_enabled(config: dict[str, Any]) -> bool:
        """同時相容enable與enabled設定名稱。"""
        return bool(config.get("enable", config.get("enabled", False)))

    def _config_snapshot(self) -> dict[str, Any]:
        getter = getattr(self.config_manager, "get_section", None)
        if callable(getter):
            value = getter("database", {})
            if isinstance(value, dict):
                return dict(value)
        getter = getattr(self.config_manager, "get_config", None)
        if callable(getter):
            root = getter()
            if isinstance(root, dict) and isinstance(root.get("database"), dict):
                return dict(root["database"])
        root = getattr(self.config_manager, "config", {})
        if isinstance(root, dict) and isinstance(root.get("database"), dict):
            return dict(root["database"])
        return {}

    def reload_config(self) -> dict[str, Any]:
        with self._lock:
            self._config = self._config_snapshot()
            return dict(self._config)

    def _connect(self):
        try:
            import pymysql
        except ImportError as exc:
            raise RuntimeError(
                "尚未安裝pymysql，請執行pip install -r requirements.txt"
            ) from exc

        with self._lock:
            config = dict(self._config)
        database = str(config.get("database", "")).strip()
        if not database:
            raise ValueError("database.database不可為空白")
        return pymysql.connect(
            host=str(config.get("host", "127.0.0.1")),
            port=int(config.get("port", 3306)),
            user=str(config.get("user", "")),
            password=str(config.get("password", "")),
            database=database,
            charset=str(config.get("charset", "utf8mb4")),
            connect_timeout=max(1, int(float(config.get("connect_timeout", 5)))),
            autocommit=False,
        )

    def test_connection(self):
        try:
            connection = self._connect()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
            finally:
                connection.close()
            return True, "資料庫連線成功"
        except Exception as exc:
            self._log(f"資料庫連線失敗：{exc}", "ERROR")
            return False, f"資料庫連線失敗：{exc}"

    def ensure_tables(self):
        sql_path = Path(__file__).resolve().parents[1] / "sql" / "create_tables.sql"
        try:
            sql_text = sql_path.read_text(encoding="utf-8")
            statements = [item.strip() for item in sql_text.split(";") if item.strip()]
            connection = self._connect()
            try:
                with connection.cursor() as cursor:
                    for statement in statements:
                        cursor.execute(statement)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            return True, "資料表確認與建立完成"
        except Exception as exc:
            self._log(f"資料表建立失敗：{exc}", "ERROR")
            return False, f"資料表建立失敗：{exc}"

    @staticmethod
    def _db_timestamp(point_value: PointValue) -> datetime:
        """將PointValue時間轉成MySQL可接受的無時區datetime。"""
        timestamp = point_value.timestamp
        if isinstance(timestamp, str):
            text = timestamp.strip().replace("Z", "+00:00")
            try:
                timestamp = datetime.fromisoformat(text)
            except ValueError:
                timestamp = datetime.now()
        elif not isinstance(timestamp, datetime):
            timestamp = datetime.now()

        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
        return timestamp

    @staticmethod
    def _json_text(value: Any) -> str:
        """將原始值或設定轉成可儲存的JSON文字。"""
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _parameters(point_value: PointValue) -> tuple[Any, ...]:
        """參數順序與sql/create_tables.sql欄位一致。"""
        return (
            point_value.point_key,
            point_value.protocol,
            point_value.source_name,
            point_value.device_name,
            point_value.point_name,
            point_value.address_text,
            DatabaseManager._json_text(point_value.value),
            point_value.value_text,
            point_value.value_number,
            point_value.status_text,
            DatabaseManager._db_timestamp(point_value),
            1 if point_value.writable else 0,
            point_value.data_type,
            DatabaseManager._json_text(point_value.raw_config),
        )

    def write_point_value(self, point_value: PointValue) -> bool:
        if not isinstance(point_value, PointValue):
            raise TypeError("write_point_value只接受PointValue")

        with self._lock:
            config = dict(self._config)
        if not self._is_enabled(config):
            return False

        signature = (
            point_value.value_text,
            point_value.value_number,
            point_value.status_text,
            point_value.data_type,
        )
        if bool(config.get("write_only_on_change", True)):
            with self._lock:
                if self._last_signature.get(point_value.point_key) == signature:
                    return False

        params = self._parameters(point_value)
        columns = (
            "point_key,protocol,source_name,device_name,point_name,address_text,"
            "value_json,value_text,value_number,status_text,point_timestamp,"
            "writable,data_type,raw_config"
        )
        placeholders = ",".join(["%s"] * 14)

        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                if bool(config.get("write_history", True)):
                    cursor.execute(
                        f"INSERT INTO plc_point_history ({columns}) "
                        f"VALUES ({placeholders})",
                        params,
                    )
                if bool(config.get("write_latest", True)):
                    cursor.execute(
                        f"""
                        INSERT INTO plc_point_latest ({columns})
                        VALUES ({placeholders})
                        ON DUPLICATE KEY UPDATE
                         protocol=VALUES(protocol),source_name=VALUES(source_name),
                         device_name=VALUES(device_name),point_name=VALUES(point_name),
                         address_text=VALUES(address_text),value_json=VALUES(value_json),
                         value_text=VALUES(value_text),value_number=VALUES(value_number),
                         status_text=VALUES(status_text),
                         point_timestamp=VALUES(point_timestamp),
                         writable=VALUES(writable),data_type=VALUES(data_type),
                         raw_config=VALUES(raw_config)
                        """,
                        params,
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        with self._lock:
            self._last_signature[point_value.point_key] = signature
        return True

    def _enqueue_point(self, point_value: PointValue) -> None:
        if not isinstance(point_value, PointValue):
            return
        if point_value.raw_config.get("db_enable", True) is False:
            return
        try:
            self._queue.put_nowait(point_value)
        except queue.Full:
            self._log("資料庫寫入佇列已滿，略過一筆點位資料", "WARNING")

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                point_value = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if point_value is None:
                self._queue.task_done()
                continue
            try:
                self.write_point_value(point_value)
            except Exception as exc:
                self._log(
                    f"寫入點位「{point_value.point_key}」失敗：{exc}",
                    "ERROR",
                )
            finally:
                self._queue.task_done()

    def start_auto_write(self):
        with self._lock:
            if self._auto_write_running:
                return True, "自動上傳已在執行"
            if not self._is_enabled(self._config):
                return False, "config.json尚未啟用database.enable"
            self._stop_event.clear()
            self._auto_write_running = True
            self.value_bus.subscribe(self._enqueue_point)
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="DatabaseAutoWriter",
                daemon=True,
            )
            self._worker.start()
        return True, "自動上傳已啟動"

    def stop_auto_write(self):
        with self._lock:
            self.value_bus.unsubscribe(self._enqueue_point)
            if not self._auto_write_running:
                return True, "自動上傳已停止"
            self._auto_write_running = False
            self._stop_event.set()
            worker = self._worker

        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=10.0)

        worker_alive = bool(worker and worker.is_alive())
        with self._lock:
            self._worker = worker if worker_alive else None

        if worker_alive:
            self._log("資料庫背景寫入執行緒未在10秒內停止", "WARNING")
            return False, "資料庫背景寫入執行緒停止逾時"
        return True, "自動上傳已停止"

    def is_auto_write_running(self) -> bool:
        with self._lock:
            return bool(
                self._auto_write_running
                and self._worker
                and self._worker.is_alive()
            )

    def is_auto_writing(self) -> bool:
        return self.is_auto_write_running()

    def is_running(self) -> bool:
        return self.is_auto_write_running()
