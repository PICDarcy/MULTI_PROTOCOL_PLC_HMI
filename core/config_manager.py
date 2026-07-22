"""config.json執行緒安全讀寫管理。"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any


class ConfigManager:
    """集中管理專案設定，並提供UI與各Manager一致的存取介面。"""

    def __init__(self, config_path: str | Path = "config.json"):
        self.config_path = Path(config_path)
        self.path = self.config_path
        self.file_path = self.config_path
        self._lock = threading.RLock()
        self.config: dict[str, Any] = {}
        self.reload_config()

    @property
    def data(self) -> dict[str, Any]:
        return self.config

    @property
    def settings(self) -> dict[str, Any]:
        return self.config

    def reload_config(self) -> dict[str, Any]:
        """從磁碟重新載入設定。"""
        with self._lock:
            if not self.config_path.exists():
                self.config = {}
                return {}
            with self.config_path.open("r", encoding="utf-8") as file:
                loaded = json.load(file)
            if not isinstance(loaded, dict):
                raise ValueError("config.json根節點必須是JSON物件")
            self.config = loaded
            return copy.deepcopy(self.config)

    def load_config(self) -> dict[str, Any]:
        return self.reload_config()

    def load(self) -> dict[str, Any]:
        return self.reload_config()

    def reload(self) -> dict[str, Any]:
        return self.reload_config()

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.config)

    def get_all(self) -> dict[str, Any]:
        return self.get_config()

    def as_dict(self) -> dict[str, Any]:
        return self.get_config()

    def to_dict(self) -> dict[str, Any]:
        return self.get_config()

    def get_section(self, name: str, default: Any = None) -> Any:
        with self._lock:
            value = self.config.get(name, default)
            return copy.deepcopy(value)

    def get(self, key: str, default: Any = None) -> Any:
        return self.get_section(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.config[str(key)] = copy.deepcopy(value)

    def set_section(self, name: str, value: Any) -> None:
        self.set(name, value)

    def update_section(self, name: str, value: Any) -> None:
        """以完整新內容取代指定區段，避免殘留已刪除的裝置或點位。"""
        self.set(name, value)

    def set_config(self, config: dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise TypeError("config必須是dict")
        with self._lock:
            self.config = copy.deepcopy(config)

    def save_config(self, config: dict[str, Any] | None = None) -> bool:
        """以UTF-8及原子替換方式儲存設定。"""
        with self._lock:
            if config is not None:
                if not isinstance(config, dict):
                    raise TypeError("config必須是dict")
                self.config = copy.deepcopy(config)

            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
            with temp_path.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(self.config, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            temp_path.replace(self.config_path)
            return True

    def save(self, config: dict[str, Any] | None = None) -> bool:
        return self.save_config(config)

    def write_config(self, config: dict[str, Any] | None = None) -> bool:
        return self.save_config(config)

    def persist(self, config: dict[str, Any] | None = None) -> bool:
        return self.save_config(config)
