"""MULTI_PROTOCOL_PLC_HMI核心模組套件。

本套件負責設定管理、共用資料模型、ValueBus、資料庫、Modbus RTU與OPC UA通訊功能。
"""

SUPPORTED_PROTOCOLS = ("MODBUS_RTU", "OPCUA")

__all__ = ["SUPPORTED_PROTOCOLS"]
