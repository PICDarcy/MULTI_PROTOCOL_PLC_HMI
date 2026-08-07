# Triage Labels

工程技能使用五個固定分流角色；下表是角色與本專案 GitHub 標籤的一對一映射。

| Canonical role | GitHub label | Meaning |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | 等待維護者評估 |
| `needs-info` | `needs-info` | 等待提報者補充資訊 |
| `ready-for-agent` | `ready-for-agent` | 規格完整，可由代理獨立執行 |
| `ready-for-human` | `ready-for-human` | 需要人工處理或決策 |
| `wontfix` | `wontfix` | 不予處理 |

當技能提到上述 canonical role 時，必須使用同一列的 GitHub label。每張由 `to-tickets` 發布且可獨立交付的票，預設套用 `ready-for-agent`。
