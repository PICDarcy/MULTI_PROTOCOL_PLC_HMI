-- MULTI_PROTOCOL_PLC_HMI資料表
-- 請先建立並選擇目標資料庫後再執行本檔案。

CREATE TABLE IF NOT EXISTS plc_point_history (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '流水號',
    point_key VARCHAR(255) NOT NULL COMMENT '點位唯一鍵',
    protocol VARCHAR(32) NOT NULL COMMENT '通訊協定：MODBUS_RTU或OPCUA',
    source_name VARCHAR(128) NOT NULL DEFAULT '' COMMENT '來源名稱',
    device_name VARCHAR(128) NOT NULL DEFAULT '' COMMENT '裝置名稱',
    point_name VARCHAR(128) NOT NULL DEFAULT '' COMMENT '點位名稱',
    address_text VARCHAR(512) NOT NULL DEFAULT '' COMMENT '位址文字',
    value_text TEXT NULL COMMENT '文字值',
    value_number DOUBLE NULL COMMENT '數值',
    status_text VARCHAR(64) NOT NULL DEFAULT '' COMMENT '狀態文字',
    data_type VARCHAR(64) NOT NULL DEFAULT '' COMMENT '資料型別',
    writable TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否可寫入',
    point_timestamp DATETIME(6) NOT NULL COMMENT '點位時間',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '寫入時間',
    PRIMARY KEY (id),
    KEY idx_history_point_key_time (point_key, point_timestamp),
    KEY idx_history_protocol_time (protocol, point_timestamp),
    KEY idx_history_device_time (device_name, point_timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='PLC點位歷史資料';

CREATE TABLE IF NOT EXISTS plc_point_latest (
    point_key VARCHAR(255) NOT NULL COMMENT '點位唯一鍵',
    protocol VARCHAR(32) NOT NULL COMMENT '通訊協定：MODBUS_RTU或OPCUA',
    source_name VARCHAR(128) NOT NULL DEFAULT '' COMMENT '來源名稱',
    device_name VARCHAR(128) NOT NULL DEFAULT '' COMMENT '裝置名稱',
    point_name VARCHAR(128) NOT NULL DEFAULT '' COMMENT '點位名稱',
    address_text VARCHAR(512) NOT NULL DEFAULT '' COMMENT '位址文字',
    value_text TEXT NULL COMMENT '文字值',
    value_number DOUBLE NULL COMMENT '數值',
    status_text VARCHAR(64) NOT NULL DEFAULT '' COMMENT '狀態文字',
    data_type VARCHAR(64) NOT NULL DEFAULT '' COMMENT '資料型別',
    writable TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否可寫入',
    point_timestamp DATETIME(6) NOT NULL COMMENT '點位時間',
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新時間',
    PRIMARY KEY (point_key),
    KEY idx_latest_protocol (protocol),
    KEY idx_latest_device_name (device_name),
    KEY idx_latest_point_timestamp (point_timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='PLC點位最新資料';
