# MULTI_PROTOCOL_PLC_HMI執行說明

本專案使用Python Tkinter開發工業通訊整合HMI，預計整合Modbus RTU、OPC UA與MySQL資料庫。

## 1. 系統需求

- Windows 10或Windows 11
- Python 3.11
- 可使用的RS-485通訊埠（測試Modbus RTU時）
- 可連線的OPC UA Server（測試OPC UA時）
- MySQL或MariaDB（啟用資料庫功能時）

## 2. 建立虛擬環境

請在專案根目錄執行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

如果PowerShell禁止執行啟動腳本，可先執行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

如果系統沒有`py`啟動器，也可以確認目前的`python`是3.11後執行：

```powershell
python -m venv .venv
```

## 3. 安裝套件

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 4. 設定config.json

請依現場設備修改下列區塊：

- `database`：MySQL連線與資料寫入設定。
- `modbus_rtu`：序列埠、通訊參數、PLC站號與點位。
- `opcua`：OPC UA Server連線參數與Node設定。

安全注意事項：

- 範例設定中的所有`password`欄位均保留空字串。
- 不要把正式密碼、Token或API Key提交到GitHub。
- 建議正式部署時改用環境變數或本機私有設定檔保存密碼。

## 5. 建立資料庫與資料表

先在MySQL或MariaDB建立與`config.json`內`database.database`相同名稱的資料庫。範例設定使用`plc_hmi`：

```sql
CREATE DATABASE IF NOT EXISTS plc_hmi
CHARACTER SET utf8mb4
COLLATE utf8mb4_unicode_ci;
```

再從專案根目錄執行下列命令，將資料表建立在`plc_hmi`資料庫：

```powershell
mysql -u root -p plc_hmi < sql/create_tables.sql
```

如果你修改了`config.json`內的資料庫名稱，請把命令中的`plc_hmi`改成相同名稱。

也可以使用MySQL Workbench或其他資料庫工具，先選擇目標資料庫，再開啟`sql/create_tables.sql`並執行。

## 6. 啟動程式

確認所有專案檔案均已下載後，從專案根目錄執行：

```powershell
python main.py
```

## 7. 基本檢查

檢查全部Python檔案是否有語法錯誤：

```powershell
python -m compileall main.py core ui
```

檢查主要模組是否可匯入：

```powershell
python -c "import main; import ui.app; print('匯入成功')"
```

## 8. 常見問題

### 找不到序列埠

確認Windows裝置管理員中的COM埠名稱，並修改`config.json`內的`modbus_rtu.port`。

### OPC UA連線失敗

確認Endpoint URL、網路、防火牆、Server安全性設定與帳號密碼是否一致。

### MySQL連線失敗

確認MySQL服務已啟動，並檢查host、port、user、database及帳號權限。

### 執行SQL時顯示No database selected

請確認MySQL命令已指定資料庫名稱，例如：

```powershell
mysql -u root -p plc_hmi < sql/create_tables.sql
```

### Tkinter無法載入

Windows官方Python通常已包含Tkinter，可使用以下命令測試：

```powershell
python -m tkinter
```
