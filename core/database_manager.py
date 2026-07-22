"""MySQL/MariaDB點位資料寫入管理。"""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .data_model import PointValue


class DatabaseManager:
    """接收PointValue並寫入歷史表及最新值表。

    修正重點：
    - 背景worker停止時最多清空queue一段可設定時間，避免程式關閉無限等待。
    - 停止逾時時保留worker引用，start_auto_write會拒絕建立第二個worker。
    - 所有寫入入口都會檢查database.enable與point.raw_config.db_enable。
    """

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
    def _as_bool(value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "y", "on", "是", "啟用"}:
                return True
            if text in {"0", "false", "no", "n", "off", "否", "停用"}:
                return False
        return bool(value)

    @classmethod
    def _is_enabled(cls, config: dict[str, Any]) -> bool:
        return cls._as_bool(config.get("enable", config.get("enabled")), False)

    @classmethod
    def _point_db_enabled(cls, point_value: PointValue) -> bool:
        raw_config = point_value.raw_config if isinstance(point_value.raw_config, dict) else {}
        return cls._as_bool(raw_config.get("db_enable", True), True)

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
            read_timeout=max(1, int(float(config.get("read_timeout", config.get("connect_timeout", 5))))),
            write_timeout=max(1, int(float(config.get("write_timeout", config.get("connect_timeout", 5))))),
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
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _parameters(point_value: PointValue) -> tuple[Any, ...]:
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
        if not self._point_db_enabled(point_value):
            return False

        signature = (
            point_value.value_text,
            point_value.value_number,
            point_value.status_text,
            point_value.data_type,
        )
        if self._as_bool(config.get("write_only_on_change", True), True):
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
                if self._as_bool(config.get("write_history", True), True):
                    cursor.execute(
                        f"INSERT INTO plc_point_history ({columns}) "
                        f"VALUES ({placeholders})",
                        params,
                    )
                if self._as_bool(config.get("write_latest", True), True):
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
        if not self._point_db_enabled(point_value):
            return
        try:
            self._queue.put_nowait(point_value)
        except queue.Full:
            self._log("資料庫寫入佇列已滿，略過一筆點位資料", "WARNING")

    def _queue_get_with_stop(self, timeout: float = 0.2):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _discard_remaining_queue(self) -> int:
        count = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._queue.task_done()
            except ValueError:
                pass
            if item is not None:
                count += 1
        return count

    def _worker_loop(self) -> None:
        drain_deadline: float | None = None
        try:
            while True:
                if self._stop_event.is_set():
                    if drain_deadline is None:
                        with self._lock:
                            config = dict(self._config)
                        drain_seconds = max(
                            0.0,
                            float(config.get("stop_drain_timeout", 5.0)),
                        )
                        drain_deadline = time.monotonic() + drain_seconds
                    if self._queue.empty():
                        break
                    if time.monotonic() > drain_deadline:
                        dropped = self._discard_remaining_queue()
                        if dropped:
                            self._log(
                                f"資料庫背景寫入停止逾時，已略過佇列中{dropped}筆未寫入資料",
                                "WARNING",
                            )
                        break

                point_value = self._queue_get_with_stop(timeout=0.2)
                if point_value is None:
                    continue

                try:
                    self.write_point_value(point_value)
                except Exception as exc:
                    self._log(f"寫入點位「{point_value.point_key}」失敗：{exc}", "ERROR")
                finally:
                    self._queue.task_done()
        finally:
            with self._lock:
                if self._worker is threading.current_thread():
                    self._worker = None
                self._auto_write_running = False

    def start_auto_write(self):
        with self._lock:
            if self._worker and self._worker.is_alive():
                return False, "資料庫背景寫入執行緒仍在停止中，請稍後再啟動"
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
            try:
                self.value_bus.unsubscribe(self._enqueue_point)
            except Exception as exc:
                self._log(f"取消資料庫自動上傳訂閱失敗：{exc}", "WARNING")
            worker = self._worker
            if worker is None or not worker.is_alive():
                self._auto_write_running = False
                self._worker = None
                return True, "自動上傳已停止"
            self._auto_write_running = False
            self._stop_event.set()

        if worker is not threading.current_thread():
            worker.join(timeout=10.0)

        worker_alive = bool(worker and worker.is_alive())
        with self._lock:
            self._worker = worker if worker_alive else None

        if worker_alive:
            self._log("資料庫背景寫入執行緒未在10秒內停止，將禁止建立第二個worker。", "WARNING")
            return False, "資料庫背景寫入執行緒停止逾時"
        return True, "自動上傳已停止"

    def is_auto_write_running(self) -> bool:
        with self._lock:
            return bool(self._worker and self._worker.is_alive())

    def is_auto_writing(self) -> bool:
        return self.is_auto_write_running()

    def is_running(self) -> bool:
        return self.is_auto_write_running()
