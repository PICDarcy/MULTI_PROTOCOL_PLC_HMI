"""專案設定檔管理模組。

負責建立、讀取、合併與安全儲存config.json，只使用Python標準函式庫。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .data_model import GatewayModel, allocate_modbus_output_addresses


DEFAULT_CONFIG: dict[str, Any] = {
    "app": {
        "title": "多協定PLC HMI",
        "window_width": 1280,
        "window_height": 800,
        "refresh_interval_ms": 500,
    },
    "modbus_rtu": {
        "enable": False,
        "port": "COM1",
        "baudrate": 9600,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "timeout": 1.0,
        "poll_interval": 1.0,
        "devices": [],
    },
    "modbus_tcp": {
        "enable": False,
        "timeout": 3.0,
        "poll_interval": 1.0,
        "devices": [],
    },
    "opcua": {
        "enable": False,
        "servers": [],
    },
    "gateway_outputs": {
        "modbus_tcp_server": {
            "enable": False,
            "host": "127.0.0.1",
            "port": 1502,
            "coil_start": 0,
            "coil_end": 65535,
            "register_start": 0,
            "register_end": 65535,
        },
        "opcua_server": {
            "enable": False,
            "endpoint": "opc.tcp://127.0.0.1:4841",
        },
    },
    "gateway_model": {
        "connections": [],
        "devices": [],
        "tags": [],
    },
    "database": {
        "enable": False,
        "host": "127.0.0.1",
        "port": 3306,
        "user": "",
        "password": "",
        "database": "plc_hmi",
        "charset": "utf8mb4",
        "connect_timeout": 5,
        "write_history": True,
        "write_latest": True,
        "write_only_on_change": True,
    },
}


class MissingCredentialError(ValueError):
    """已啟用功能缺少必要憑證。"""


SENSITIVE_FIELD_NAMES = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "private_key",
    }
)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "true":
            return True
        if text == "false":
            return False
    raise ValueError("enable與use_username必須是JSON布林值true或false")


def find_obvious_credentials(config: Mapping[str, Any]) -> tuple[str, ...]:
    """找出設定中明顯的非空憑證，只回傳欄位路徑。"""
    problems: list[str] = []

    def has_value(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (Mapping, list, tuple, set)):
            return bool(value)
        return True

    def visit(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_text = str(key)
                child_path = f"{path}.{key_text}" if path else key_text
                normalized_key = key_text.strip().lower().replace("-", "_")
                if normalized_key in SENSITIVE_FIELD_NAMES and has_value(child):
                    problems.append(child_path)
                    continue
                visit(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(config, "")
    return tuple(problems)


def find_missing_enabled_credentials(
    config: Mapping[str, Any],
) -> tuple[str, ...]:
    """回傳已啟用功能中缺少的憑證欄位路徑。"""
    missing: list[str] = []

    database = config.get("database", {})
    if isinstance(database, Mapping) and _as_bool(database.get("enable")):
        missing.extend(
            f"database.{key}"
            for key in ("user", "password")
            if not str(database.get(key, "") or "").strip()
        )

    opcua = config.get("opcua", {})
    if isinstance(opcua, Mapping) and _as_bool(opcua.get("enable")):
        servers = opcua.get("servers", [])
        if isinstance(servers, list):
            for index, server in enumerate(servers):
                if not isinstance(server, Mapping):
                    continue
                if not _as_bool(server.get("enable", True), True):
                    continue
                auth = (
                    server.get("auth")
                    if isinstance(server.get("auth"), Mapping)
                    else {}
                )
                username = str(
                    server.get("username", auth.get("username", "")) or ""
                ).strip()
                password = str(
                    server.get("password", auth.get("password", "")) or ""
                ).strip()
                use_username = _as_bool(
                    server.get("use_username", bool(username)),
                    bool(username),
                )
                if not use_username:
                    continue
                if not username:
                    missing.append(f"opcua.servers[{index}].username")
                if not password:
                    missing.append(f"opcua.servers[{index}].password")

    return tuple(missing)


class ConfigManager:
    """執行緒安全的JSON設定管理器。

    設定檔不存在時會自動建立。若config.json格式錯誤，會先備份為
    config_error_YYYYMMDD_HHMMSS.json，再使用預設設定重建。
    """

    DEFAULT_CONFIG = DEFAULT_CONFIG

    def __init__(
        self,
        config_path: str | os.PathLike[str] = "config.json",
        default_config: Mapping[str, Any] | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        # 保留既有程式可能使用的公開別名。
        self.path = self.config_path
        self.file_path = self.config_path
        self._lock = threading.RLock()
        self._default_config = self.deep_merge(DEFAULT_CONFIG, default_config or {})
        self.config: dict[str, Any] = {}
        self.load_config()

    @property
    def data(self) -> dict[str, Any]:
        """取得完整設定副本。"""
        return self.get_config()

    @property
    def settings(self) -> dict[str, Any]:
        """取得完整設定副本。"""
        return self.get_config()

    @staticmethod
    def deep_merge(
        base: Mapping[str, Any] | None,
        override: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """遞迴合併字典並回傳新副本，不修改輸入資料。"""
        if base is not None and not isinstance(base, Mapping):
            raise TypeError("base必須是Mapping或None")
        if override is not None and not isinstance(override, Mapping):
            raise TypeError("override必須是Mapping或None")

        result: dict[str, Any] = copy.deepcopy(dict(base or {}))
        for key, value in dict(override or {}).items():
            current_value = result.get(key)
            if isinstance(current_value, Mapping) and isinstance(value, Mapping):
                result[key] = ConfigManager.deep_merge(current_value, value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    def load_config(self) -> dict[str, Any]:
        """載入設定並以DEFAULT_CONFIG補齊缺少欄位。"""
        with self._lock:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)

            if not self.config_path.exists():
                self.config = self._prepare_config_for_save(
                    copy.deepcopy(self._default_config)
                )
                self.require_enabled_credentials()
                self.save_config()
                return self.get_config()

            try:
                with self.config_path.open("r", encoding="utf-8-sig") as file:
                    loaded = json.load(file)
                if not isinstance(loaded, dict):
                    raise ValueError("config.json根節點必須是JSON物件")
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
                self._backup_invalid_config()
                self.config = self._prepare_config_for_save(
                    copy.deepcopy(self._default_config)
                )
                self.require_enabled_credentials()
                self.save_config()
                return self.get_config()

            self.config = self._prepare_config_for_save(
                self.deep_merge(self._default_config, loaded)
            )
            self.require_enabled_credentials()
            return self.get_config()

    def save_config(self, config: Mapping[str, Any] | None = None) -> bool:
        """驗證完整模型後，以UTF-8及原子替換方式儲存設定。"""
        with self._lock:
            if config is not None and not isinstance(config, Mapping):
                raise TypeError("config必須是Mapping")

            source = config if config is not None else self.config
            if not source:
                source = self._default_config
            candidate = self._prepare_config_for_save(
                self.deep_merge(self._default_config, source)
            )

            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="\n",
                    dir=str(self.config_path.parent),
                    prefix=f".{self.config_path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary_file:
                    temporary_path = temporary_file.name
                    json.dump(
                        candidate,
                        temporary_file,
                        ensure_ascii=False,
                        indent=2,
                    )
                    temporary_file.write("\n")
                    temporary_file.flush()
                    os.fsync(temporary_file.fileno())

                os.replace(temporary_path, self.config_path)
                temporary_path = None
                self.config = candidate
                return True
            finally:
                if temporary_path and os.path.exists(temporary_path):
                    os.remove(temporary_path)

    def _prepare_config_for_save(
        self,
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        """驗證／配置 Gateway 映射並回傳不修改輸入的完整副本。"""
        candidate = copy.deepcopy(dict(config))
        model = GatewayModel.from_dict(candidate.get("gateway_model", {}))
        outputs = candidate.get("gateway_outputs", {})
        if not isinstance(outputs, Mapping):
            outputs = {}
        server = outputs.get("modbus_tcp_server", {})
        if not isinstance(server, Mapping):
            server = {}
        allocated_tags = allocate_modbus_output_addresses(
            model.tags,
            coil_start=server.get("coil_start", 0),
            coil_end=server.get("coil_end", 65535),
            register_start=server.get("register_start", 0),
            register_end=server.get("register_end", 65535),
        )
        candidate["gateway_model"] = GatewayModel(
            connections=model.connections,
            devices=model.devices,
            tags=allocated_tags,
        ).to_dict()
        return candidate

    def get_config(self) -> dict[str, Any]:
        """取得完整設定的深層副本。"""
        with self._lock:
            return copy.deepcopy(self.config)

    def get_missing_enabled_credentials(self) -> tuple[str, ...]:
        """取得已啟用功能中尚未填寫的必要憑證欄位。"""
        with self._lock:
            return find_missing_enabled_credentials(self.config)

    def require_enabled_credentials(self) -> None:
        """缺少必要憑證時停止載入，錯誤訊息只列欄位、不列值。"""
        missing = self.get_missing_enabled_credentials()
        if not missing:
            return
        fields = "、".join(missing)
        raise MissingCredentialError(
            f"已啟用功能缺少必要憑證：{fields}。"
            "請在未納入版本控制的config.json補齊後再啟動。"
        )

    def get_section(self, section_name: str, default: Any = None) -> Any:
        """取得指定設定區段的深層副本。"""
        if not isinstance(section_name, str) or not section_name.strip():
            raise ValueError("section_name不可為空")
        with self._lock:
            value = self.config.get(section_name, default)
            return copy.deepcopy(value)

    def update_section(
        self,
        section_name: str,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        """深層合併指定區段並立即寫回config.json。"""
        if not isinstance(section_name, str) or not section_name.strip():
            raise ValueError("section_name不可為空")
        if not isinstance(data, Mapping):
            raise TypeError("data必須是Mapping")

        with self._lock:
            current = self.config.get(section_name, {})
            if not isinstance(current, Mapping):
                current = {}
            updated = self.deep_merge(current, data)
            candidate = copy.deepcopy(self.config)
            candidate[section_name] = updated
            self.save_config(candidate)
            return copy.deepcopy(updated)

    def get_gateway_model(self) -> GatewayModel:
        """將 canonical 設定驗證、配置後反序列化為一致性模型。"""
        with self._lock:
            prepared = self._prepare_config_for_save(self.config)
        return GatewayModel.from_dict(prepared.get("gateway_model", {}))

    def set_gateway_model(self, model: GatewayModel) -> None:
        """保存 canonical 模型；實際寫檔仍由既有 save_config 控制。"""
        if not isinstance(model, GatewayModel):
            raise TypeError("model必須是GatewayModel")
        self.set_section("gateway_model", model.to_dict())

    def reload(self) -> dict[str, Any]:
        """重新從磁碟載入設定。"""
        return self.load_config()

    # 以下方法保留既有呼叫方式，並轉接至主要公開介面。
    def reload_config(self) -> dict[str, Any]:
        return self.reload()

    def load(self) -> dict[str, Any]:
        return self.load_config()

    def get_all(self) -> dict[str, Any]:
        return self.get_config()

    def as_dict(self) -> dict[str, Any]:
        return self.get_config()

    def to_dict(self) -> dict[str, Any]:
        return self.get_config()

    def get(self, key: str, default: Any = None) -> Any:
        return self.get_section(key, default)

    def set(self, key: str, value: Any) -> None:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("key不可為空")
        with self._lock:
            self.config[key] = copy.deepcopy(value)

    def set_section(self, name: str, value: Any) -> None:
        self.set(name, value)

    def set_config(self, config: Mapping[str, Any]) -> None:
        if not isinstance(config, Mapping):
            raise TypeError("config必須是Mapping")
        with self._lock:
            self.config = self.deep_merge(self._default_config, config)

    def save(self, config: Mapping[str, Any] | None = None) -> bool:
        return self.save_config(config)

    def write_config(self, config: Mapping[str, Any] | None = None) -> bool:
        return self.save_config(config)

    def persist(self, config: Mapping[str, Any] | None = None) -> bool:
        return self.save_config(config)

    def _backup_invalid_config(self) -> Path | None:
        """備份格式錯誤的config.json並避免檔名衝突。"""
        if not self.config_path.exists():
            return None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = self.config_path.suffix or ".json"
        backup_path = self.config_path.with_name(
            f"config_error_{timestamp}{suffix}"
        )
        sequence = 1
        while backup_path.exists():
            backup_path = self.config_path.with_name(
                f"config_error_{timestamp}_{sequence}{suffix}"
            )
            sequence += 1

        shutil.move(str(self.config_path), str(backup_path))
        return backup_path
