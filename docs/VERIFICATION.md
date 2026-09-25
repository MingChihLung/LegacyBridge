<!-- Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan). SPDX-License-Identifier: MIT -->

# 驗證報告：Windows 11 實機、Audacity 3.7.9

- 日期：2026-09-25
- 環境：Windows 11（繁中系統）、Python 3.12.4、mcp 2.2.0（協定 2026-07-28）、pywinauto 0.6.9、rapidocr 1.4.4
- 腳本都在 `examples/audacity/`，原始數據在 [`verification-results/`](verification-results/)

## 結果總表

| # | 項目 | 結果 |
|---|---|---|
| 1 | pytest（Linux + Windows） | 19/19 ✅ |
| 2 | 全新設定：首次啟動 4 個對話框自動關閉 → 以 L0 啟用 scripting、切換語言 → 重啟 → 四層 coverage 1.0 | ✅ |
| 3 | MCP stdio 端對端（L0/L1/L2/L3、MRTR、截圖加 SoM、UI tree、resources、trace） | ✅ |
| 4 | 穩定性：`generate_tone` 每層 5 次，驗證條件為「音軌數 +1 且 clip 數 +1」 | L1 5/5（中位數 0.36 s）、L2 5/5（5.2 s）、L3 5/5（優化前 83 s → 優化後 29 s） |
| 5 | 鍵盤類動作用 L1 當標準答案比對：select_all 在 L2/L3 都選到 0→2.0 s；undo 在 L2/L3 都讓 clip 2→1 | ✅ |
| 6 | 狀態限制：人為開著 Tone 對話框時 → 狀態為 `Dialog`、`available_now` 排除 GUI 動作、`generate_tone` 被拒 → 按 Cancel → 恢復可用 | ✅ |
| 7 | MRTR 拒絕：使用者拒絕時 `remove_all_tracks` 不會執行（音軌 5→5） | ✅ |
| 8 | Tasks extension：`export_wav` 以 task 回傳，狀態 working→completed，statusMessage 顯示「100% Export2」 | ✅ |
| 9 | Fallback：停用 mod-script-pipe 後，不指定層也會自動改走 L2（tone 和音軌數）；只有 L1 能做的 `export_wav` 回報 no runnable layer，並由 explain 說明原因 | ✅ |
| 10 | Streamable HTTP（port 8799）：協定 2026-07-28，get_version、音軌數、偏好設定查詢 | ✅ |
| 11 | ctypes（user32 GetSystemMetrics = 2560、kernel32 GetTickCount64）、COM（FileSystemObject、WScript.Shell）、找不到 DLL 時回報 probe_failed | ✅ |
| 12 | close_app：遇到存檔提示時，`discard=true` 才按「No」；以行程結束作為成功判斷 | ✅ |

## 實機驗證找到並已修正的問題

1. Python 3.12 的 `os.path.exists` 會打開 named pipe，讓 Audacity 的 pipe 卡在半連線 → probe 改為只列出 `\\.\pipe\` 目錄。
2. 程式開著時寫設定檔，關閉時會被覆蓋 → 改為先關、再寫、再啟動。
3. 中文系統的 `tasklist` 輸出 cp950 → 改用 `oem` 解碼。
4. Win32 選單不在 UIA 樹裡，系統選單列名稱也會依系統語言改變 → 改用 win32 送 WM_COMMAND 執行 menu_select。
5. wx 的 Edit 不接受 UIA 的 ValuePattern → 改用鍵盤輸入。
6. 被擁有的對話框在 UIA 裡看不到或是巢狀 → 改用 win32 列舉該行程的視窗。
7. 啟動期間視窗一直出現又消失（handle 失效） → 例外逐一容錯，不讓整個流程失敗。
8. **postcondition 太弱會產生假成功**：L3 的 Tone 對話框其實沒按到 Generate，但因為有新增音軌，「音軌數 +1」仍然通過 → 加上「clip 數 +1」，並支援多重驗證條件。
9. **L3 把焦點給了主視窗**，按鍵送進主視窗而不是 modal 對話框 → 有對話框時改把焦點給對話框。
10. OCR 把整條選單列合併成一個文字框 → 依字元位置推算座標，並優先使用錄好的 template。
11. L3 太慢（全視窗 2 倍放大後做 OCR） → 先只 OCR 上次點擊附近的區域，等待對話框改用狀態判斷，83 s → 29 s。
12. close_app 原本以 `close()` 呼叫回傳就當成功 → 改為以行程結束作為成功判斷。

## 已知限制

- L3 仍然最慢，而且依賴 template 和 OCR。它是最後手段；要在 server 端自動完成語意定位，需要設定 VLM（LB_VLM_MODEL）。
- 用 Alt 助記鍵開選單（例如 Alt+G 再按 T）在這台機器上沒反應，目前只用 WM_COMMAND 或滑鼠開選單。
- L1 無法使用時，`generate_tone` 的驗證只剩「音軌數」這一項（clip 數沒有其他層能讀），可信度較低，結果中也會反映這點。
- 在 MCP 端，`resources/read` 只能套用一種快取時間；Tasks 狀態只能輪詢，沒有推播。
