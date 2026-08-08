"""安全設定流程的公開行為測試。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from core import config_manager


class EnabledCredentialValidationTests(unittest.TestCase):
    def test_enabled_database_requires_user_and_password(self):
        missing = config_manager.find_missing_enabled_credentials(
            {
                "database": {
                    "enable": True,
                    "user": "",
                    "password": "",
                }
            }
        )

        self.assertEqual(
            ("database.user", "database.password"),
            missing,
        )

    def test_text_false_does_not_enable_credential_validation(self):
        missing = config_manager.find_missing_enabled_credentials(
            {
                "database": {
                    "enable": "false",
                    "user": "",
                    "password": "",
                }
            }
        )

        self.assertEqual((), missing)

    def test_unknown_text_enable_value_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "JSON布林值"):
            config_manager.find_missing_enabled_credentials(
                {
                    "database": {
                        "enable": "flase",
                        "user": "",
                        "password": "",
                    }
                }
            )

    def test_enabled_opcua_username_login_requires_both_credentials(self):
        missing = config_manager.find_missing_enabled_credentials(
            {
                "opcua": {
                    "enable": True,
                    "servers": [
                        {
                            "enable": True,
                            "use_username": True,
                            "username": "",
                            "password": "",
                        },
                        {
                            "enable": False,
                            "use_username": True,
                            "username": "",
                            "password": "",
                        },
                        {
                            "enable": True,
                            "use_username": False,
                            "username": "",
                            "password": "",
                        },
                    ],
                }
            }
        )

        self.assertEqual(
            (
                "opcua.servers[0].username",
                "opcua.servers[0].password",
            ),
            missing,
        )

    def test_opcua_username_implies_login_and_requires_password(self):
        missing = config_manager.find_missing_enabled_credentials(
            {
                "opcua": {
                    "enable": True,
                    "servers": [
                        {
                            "enable": True,
                            "username": "operator",
                            "password": "",
                        }
                    ],
                }
            }
        )

        self.assertEqual(("opcua.servers[0].password",), missing)

    def test_loading_enabled_feature_without_credentials_stops_startup_safely(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "database": {
                            "enable": True,
                            "user": "",
                            "password": "",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(
                config_manager.MissingCredentialError
            ) as raised:
                config_manager.ConfigManager(config_path)

        self.assertEqual(
            "已啟用功能缺少必要憑證：database.user、database.password。"
            "請在未納入版本控制的config.json補齊後再啟動。",
            str(raised.exception),
        )


class ExampleCredentialScanTests(unittest.TestCase):
    def test_nested_non_empty_passwords_and_tokens_are_reported_by_path(self):
        problems = config_manager.find_obvious_credentials(
            {
                "database": {"password": "do-not-print-me"},
                "services": [
                    {"token": "also-do-not-print-me"},
                    {"password": ""},
                ],
            }
        )

        self.assertEqual(
            ("database.password", "services[0].token"),
            problems,
        )

    def test_command_line_check_rejects_secret_without_printing_its_value(self):
        project_root = Path(__file__).resolve().parents[1]
        checker = project_root / "scripts" / "check_example_credentials.py"
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "unsafe-example.json"
            config_path.write_text(
                json.dumps({"database": {"password": "do-not-print-me"}}),
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, str(checker), str(config_path)],
                cwd=project_root,
                capture_output=True,
                encoding="utf-8",
                errors="strict",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                check=False,
            )

        self.assertEqual(1, result.returncode)
        self.assertIn("database.password", result.stdout)
        self.assertNotIn("do-not-print-me", result.stdout + result.stderr)

    def test_repository_example_is_valid_json_without_credentials(self):
        project_root = Path(__file__).resolve().parents[1]
        example_path = project_root / "config.example.json"

        with example_path.open("r", encoding="utf-8-sig") as example_file:
            example = json.load(example_file)

        self.assertIsInstance(example, dict)
        self.assertEqual((), config_manager.find_obvious_credentials(example))

    def test_git_tracks_example_but_ignores_private_config(self):
        project_root = Path(__file__).resolve().parents[1]

        private_tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "config.json"],
            cwd=project_root,
            capture_output=True,
            check=False,
        )
        private_ignored = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", "config.json"],
            cwd=project_root,
            capture_output=True,
            check=False,
        )
        example_tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "config.example.json"],
            cwd=project_root,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(0, private_tracked.returncode)
        self.assertEqual(0, private_ignored.returncode)
        self.assertEqual(0, example_tracked.returncode)


if __name__ == "__main__":
    unittest.main()
