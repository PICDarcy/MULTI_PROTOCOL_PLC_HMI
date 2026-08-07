# Issue tracker: GitHub

本專案的工作項目與 PRD 發布至 `PICDarcy/MULTI_PROTOCOL_PLC_HMI` 的 GitHub Issues。

## Conventions

- 建立、讀取、留言、標記與關閉工作項目時，操作此儲存庫的 GitHub Issues。
- 發布可交由代理執行的工作項目時，套用 `ready-for-agent`。
- 每張工作票在本文寫出 `Blocked by`；若平台工具支援原生 issue dependency，應同步建立原生阻擋關係。
- 建立工作票時依相依順序發布，讓阻擋項可以引用已存在的 Issue 編號。
- 不得因拆票而關閉或修改來源母票。

## Pull requests as a triage surface

**PRs as a request surface: no.**

外部 Pull Request 不進入本專案的 Issue 分流狀態機；分流流程只處理 GitHub Issues。

## Skill operations

- 當技能要求「publish to the issue tracker」時，建立 GitHub Issue。
- 當技能要求「fetch the relevant ticket」時，讀取該 Issue 的完整本文、標籤與留言。
- 當技能要求判斷工作 frontier 時，只選擇所有阻擋票均已關閉且尚未被領取的票。
