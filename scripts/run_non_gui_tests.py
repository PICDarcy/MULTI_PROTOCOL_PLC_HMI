"""Run the complete automated suite that does not require a Tk display."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


TEST_MODULES = (
    "tests.test_secure_config",
    "tests.test_canonical_model",
    "tests.test_database_manager_missing_database",
    "tests.test_readonly_gateway",
    "tests.test_protocol_server_readonly",
    "tests.test_local_protocol_harness",
    "tests.test_opcua_canonical_source",
    "tests.test_modbus_tcp_canonical_source",
    "tests.test_modbus_rtu_canonical_source",
    # Keep both standard-client tracer paths in every supported OS run.
    "tests.test_opcua_to_modbus_tracer",
    "tests.test_modbus_to_opcua_tracer",
    "tests.test_modbus_output_contract",
)


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromNames(TEST_MODULES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
