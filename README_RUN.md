# MULTI_PROTOCOL_PLC_HMI執行說明

本專案使用Python Tkinter開發工業通訊整合HMI，整合Modbus RTU、OPC UA與MySQL/MariaDB資料庫。專案以`config.json`集中管理通訊設備、點位與資料庫設定。

## 1. 系統需求

- Windows 10或Windows 11
- Python 3.11或更新版本
- 可使用的RS-485通訊埠與USB轉RS-485設備（測試Modbus RTU時）
- 可連線的OPC UA Server（測試OPC UA時）
- MySQL或MariaDB（啟用資料庫功能時）

## 2. 專案目錄

```text
MULTI_PROTOCOL_PLC_HMI/
├─ main.py
├─ config.json
├─ requirements.txt
├─ README_RUN.md
├─ core/
├─ ui/
└─ sql/
   └─ create_tables.sql
```

## 3. 建立Python虛擬環境

請在專案根目錄執行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

如果PowerShell禁止執行啟動腳本，可先執行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

如果系統沒有`py`啟動器，也可以確認目前的`python`版本後執行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

## 4. 安裝套件

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

主要套件用途：

- `pymodbus`：Modbus RTU讀寫。
- `pyserial`：序列埠通訊支援。
- `asyncua`：OPC UA Client、訂閱與節點瀏覽。
- `pymysql`：MySQL/MariaDB資料庫連線。

## 5. 設定config.json

請依現場設備修改以下三個主要區塊：

- `database`：資料庫連線與自動寫入設定。
- `modbus_rtu`：序列埠參數、PLC站號與Modbus點位。
- `opcua`：OPC UA Server連線參數與Node設定。

所有功能區塊預設可使用`enable`控制是否啟用。個別設備、Server與點位也各自提供`enable`欄位。

### 安全注意事項

- 範例設定中的所有`password`欄位均為空字串。
- 不要把正式密碼、GitHub Token、API Key或其他機密資料提交到GitHub。
- 正式部署時建議使用環境變數或不納入版本控制的本機設定檔保存密碼。

## 6. Modbus RTU設定重點

- `port`：Windows通常為`COM3`、`COM4`等。
- `baudrate`：常見值為9600、19200、38400、115200。
- `parity`：可使用`N`、`E`、`O`。
- `station_id`：Modbus從站站號，通常為1至247。
- `type`支援：
  - `holding_register`
  - `input_register`
  - `coil`
  - `discrete_input`
- `address`使用PDU位址，通常從0開始。請依PLC或設備手冊確認是否需要將文件中的40001、30001等位址換算為0起始位址。
- `writable`只表示HMI是否允許寫入；實際是否能寫仍取決於點位類型與設備權限。

## 7. OPC UA設定重點

- `endpoint_url`格式範例：`opc.tcp://127.0.0.1:4840`。
- 啟用帳號驗證時，將`use_username`設為`true`並填入`username`。
- `password`範例保持空字串，請勿把正式密碼提交到repo。
- `node_id`格式範例：`ns=2;s=Machine.Running`或`ns=2;i=1001`。
- `subscribe`為`true`時，後續OPC UA管理器會建立資料變更訂閱。
- `data_type`可使用`Auto`，或明確填入`Boolean`、`Int16`、`UInt16`、`Int32`、`UInt32`、`Float`、`Double`、`String`等型別。

## 8. 建立資料庫與資料表

先建立與`config.json`內`database.database`相同名稱的資料庫。範例使用`plc_hmi`：

```sql
CREATE DATABASE IF NOT EXISTS plc_hmi
CHARACTER SET utf8mb4
COLLATE utf8mb4_unicode_ci;
```

再從專案根目錄執行：

```powershell
mysql -u root -p plc_hmi < sql/create_tables.sql
```

也可以使用MySQL Workbench或其他資料庫工具，先選擇目標資料庫，再開啟`sql/create_tables.sql`並執行。

建立的資料表：

- `plc_point_history`：保存點位歷史資料。
- `plc_point_latest`：每個`point_key`只保存最新一筆資料。

## 9. 啟動程式

完整專案檔案建立完成後，從專案根目錄執行：

```powershell
python main.py
```

## 10. 基本檢查

檢查全部Python檔案是否有語法錯誤：

```powershell
python -m compileall main.py core ui
```

檢查Tkinter是否可正常啟動：

```powershell
python -m tkinter
```

## 11. 常見問題

### 找不到序列埠

確認Windows裝置管理員中的COM埠名稱，並修改`config.json`內的`modbus_rtu.port`。

### Modbus讀取位址錯誤

確認設備手冊使用的是顯示位址還是PDU位址。例如文件中的40001通常可能對應程式位址0，但不同設備可能有不同定義。

### OPC UA連線失敗

確認Endpoint URL、網路、防火牆、Server安全性設定、匿名存取與帳號密碼是否一致。

### MySQL連線失敗

確認MySQL服務已啟動，並檢查`host`、`port`、`user`、`database`及帳號權限。

### 執行SQL時顯示No database selected

請確認MySQL命令已指定資料庫名稱，例如：

```powershell
mysql -u root -p plc_hmi < sql/create_tables.sql
```
