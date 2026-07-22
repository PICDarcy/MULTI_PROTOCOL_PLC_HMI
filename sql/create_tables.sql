-- MULTI_PROTOCOL_PLC_HMI資料表
-- 請先建立並選擇目標資料庫後再執行本檔案。
-- protocol欄位固定使用MODBUS_RTU或OPCUA。

CREATE TABLE IF NOT EXISTS plc_point_history (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '歷史資料流水號',
    point_key VARCHAR(255) NOT NULL COMMENT '點位唯一識別鍵',
    protocol VARCHAR(32) NOT NULL COMMENT '通訊協定：MODBUS_RTU或OPCUA',
    source_name VARCHAR(128) NOT NULL DEFAULT '' COMMENT '資料來源名稱',
    device_name VARCHAR(128) NOT NULL DEFAULT '' COMMENT '設備或Server名稱',
    point_name VARCHAR(128) NOT NULL DEFAULT '' COMMENT '點位顯示名稱',
    address_text VARCHAR(512) NOT NULL DEFAULT '' COMMENT 'Modbus位址或OPC UA Node ID',
    value_json LONGTEXT NULL COMMENT '原始值的JSON文字',
    value_text TEXT NULL COMMENT '格式化文字值',
    value_number DOUBLE NULL COMMENT '可轉換時的數值',
    status_text VARCHAR(128) NOT NULL DEFAULT '' COMMENT '通訊或資料狀態',
    point_timestamp DATETIME(6) NOT NULL COMMENT '點位資料時間',
    writable TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否允許寫入',
    data_type VARCHAR(64) NOT NULL DEFAULT '' COMMENT '資料型別',
    raw_config LONGTEXT NULL COMMENT '點位原始設定JSON文字',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '資料庫寫入時間',
    PRIMARY KEY (id),
    KEY idx_history_point_key_time (point_key, point_timestamp),
    KEY idx_history_protocol_time (protocol, point_timestamp),
    KEY idx_history_source_time (source_name, point_timestamp),
    KEY idx_history_device_time (device_name, point_timestamp)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='PLC與OPC UA點位歷史資料';

CREATE TABLE IF NOT EXISTS plc_point_latest (
    point_key VARCHAR(255) NOT NULL COMMENT '點位唯一識別鍵',
    protocol VARCHAR(32) NOT NULL COMMENT '通訊協定：MODBUS_RTU或OPCUA',
    source_name VARCHAR(128) NOT NULL DEFAULT '' COMMENT '資料來源名稱',
    device_name VARCHAR(128) NOT NULL DEFAULT '' COMMENT '設備或Server名稱',
    point_name VARCHAR(128) NOT NULL DEFAULT '' COMMENT '點位顯示名稱',
    address_text VARCHAR(512) NOT NULL DEFAULT '' COMMENT 'Modbus位址或OPC UA Node ID',
    value_json LONGTEXT NULL COMMENT '原始值的JSON文字',
    value_text TEXT NULL COMMENT '格式化文字值',
    value_number DOUBLE NULL COMMENT '可轉換時的數值',
    status_text VARCHAR(128) NOT NULL DEFAULT '' COMMENT '通訊或資料狀態',
    point_timestamp DATETIME(6) NOT NULL COMMENT '點位資料時間',
    writable TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否允許寫入',
    data_type VARCHAR(64) NOT NULL DEFAULT '' COMMENT '資料型別',
    raw_config LONGTEXT NULL COMMENT '點位原始設定JSON文字',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '首次建立時間',
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最後更新時間',
    PRIMARY KEY (point_key),
    KEY idx_latest_protocol (protocol),
    KEY idx_latest_source_name (source_name),
    KEY idx_latest_device_name (device_name),
    KEY idx_latest_point_timestamp (point_timestamp)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='PLC與OPC UA點位最新資料';
