<!-- Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan). SPDX-License-Identifier: MIT -->

# 驗證原始數據（Windows 11、Audacity 3.7.9、2026-09-25）

[`../VERIFICATION.md`](../VERIFICATION.md) 中各項結果的原始輸出，由 `examples/audacity/` 的腳本產生。

| 檔案 | 產生的腳本 | 內容 |
|---|---|---|
| `pytest_windows.txt` | `pytest` | Windows 上的單元與整合測試（19 passed） |
| `e2e_report.json` | `mcp_e2e.py` | MCP stdio 端對端測試：L0/L1/L2/L3、MRTR、截圖加 SoM、UI tree、resources、trace |
| `stability.json` | `verify_stability.py 5` | `generate_tone` 在 L1/L2/L3 各跑 5 次（L3 為優化前的數據，約 83 s/次） |
| `stability_L3.json` | `verify_stability.py 5 L3` | L3 優化後的 5 次（約 29 s/次） |
| `input.json` | `verify_input.py` | 鍵盤類動作以 L1 為標準答案比對；人為開著對話框時的狀態限制 |
| `more.json` | `verify_more.py` | MRTR 拒絕、Tasks extension、resources、explain（其中「開著對話框」那段的做法已由 `input.json` 取代） |
| `fallback.json` | `verify_fallback.py` | 停用 mod-script-pipe 後自動改走 L2 |
| `http.json` | `verify_http.py` | Streamable HTTP transport |
| `winsmoke.json` | `verify_winsmoke.py` | ctypes（user32、kernel32）與 COM（FileSystemObject、WScript.Shell） |

每一步的 `ok` 欄位：`true` 表示通過，`false` 表示該步回傳錯誤（部分是刻意驗證「應該被拒絕」的情況，例如 `gated_refusal`、`export_blocked_without_pipe`），`null` 表示只是紀錄（例如 elicitation 的訊息）。
