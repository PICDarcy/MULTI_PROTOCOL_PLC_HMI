# Domain Docs

本專案採 single-context 領域文件配置。工程技能探索或修改程式前，依本文件讀取領域語言與架構決策。

## Before exploring, read these

- 根目錄 `CONTEXT.md`。
- `docs/adr/` 中與本次工作範圍相關的 ADR。
- 若上述檔案尚不存在，直接繼續，不需事先建立或回報缺漏；待實際形成術語或架構決策時再由對應技能補上。

## File structure

```text
/
├── CONTEXT.md
├── docs/
│   ├── agents/
│   └── adr/
└── ...
```

## Vocabulary

Issue 標題、驗收條件、測試名稱與架構說明應優先使用 `CONTEXT.md` 定義的領域術語。若缺少必要術語，先確認是否沿用既有名稱；確有缺口時再記錄供 domain-modeling 流程處理。

## ADR conflicts

若工作內容與既有 ADR 衝突，必須在實作或票單中明確指出衝突，不得靜默覆寫既有決策。
