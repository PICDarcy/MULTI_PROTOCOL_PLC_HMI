"""檢查公開範例設定是否含有明顯憑證。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config_manager import find_obvious_credentials


def main(arguments: Sequence[str] | None = None) -> int:
    """執行檢查；安全回傳0，發現憑證回傳1，檔案錯誤回傳2。"""
    args = list(sys.argv[1:] if arguments is None else arguments)
    config_path = Path(args[0]) if args else PROJECT_ROOT / "config.example.json"

    try:
        with config_path.open("r", encoding="utf-8-sig") as config_file:
            config = json.load(config_file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"無法檢查範例設定：{config_path}（{exc}）", file=sys.stderr)
        return 2

    if not isinstance(config, dict):
        print("無法檢查範例設定：JSON根節點必須是物件。", file=sys.stderr)
        return 2

    problems = find_obvious_credentials(config)
    if problems:
        print("範例設定含有非空憑證欄位：" + "、".join(problems))
        return 1

    print(f"範例設定憑證檢查通過：{config_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
