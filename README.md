<!-- Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan). SPDX-License-Identifier: MIT -->

# legacy-bridge

把舊工具包成 AI agent 可操作的介面，包含四層 backend（L0–L3）、capability 查詢、**MCP 2026-07-28** server，以及 JSON CLI。

```
Agent ──MCP (2026-07-28) / JSON CLI──▶ Capability API + Action API
                                          │
                                        Router  (依 probe 與執行統計選層、驗證、fallback、降級)
                                          │
              ┌──────────────┬────────────┼─────────────┬──────────────┐
              L0 native      L1 script    L2 UIA        L3 vision
              INI/檔案/DB/DLL CLI/COM      pywinauto     截圖+SoM+OCR+輸入
```

## 目錄

| 路徑 | 內容 |
|---|---|
| `manifests/legacy_flasher.yaml` | 範例 manifest：宣告每個動作在各層「理論上」怎麼做 |
| `demo_app/legacy_flasher.py` | 模擬的舊工具，提供 INI、log、batch CLI 和 Tk GUI |
| `src/legacy_bridge/backends/` | `l0_native` `l1_script` `l2_uia` `l3_vision`，以及 `sim`（測試用的虛擬 GUI） |
| `src/legacy_bridge/registry.py` | Capability registry：manifest 宣告 × probe 實測 × 執行統計 |
| `src/legacy_bridge/router.py` | 選層、postcondition 驗證、fallback、連續失敗後降級 |
| `src/legacy_bridge/session.py` | Session handle、畫面狀態偵測、`wait_for`、SoM 標號表 |
| `src/legacy_bridge/mcp_server.py` | MCP server（`MCPServer`，mcp 2.x） |
| `src/legacy_bridge/tasks.py` / `tasks_client.py` | Tasks extension 的 server 端與 client 端 |
| `src/legacy_bridge/cli.py` | `lb` JSON CLI |

## 安裝與啟動

```bash
pip install -e ".[dev]"            # 核心 + 測試
pip install -e ".[windows]"        # Windows 實機：pywinauto / pywin32 / mss / pyautogui / opencv / rapidocr

# MCP（stdio，本機 agent）
lb-mcp --manifest manifests/legacy_flasher.yaml
# MCP（Streamable HTTP，遠端 agent）
lb-mcp --manifest manifests/legacy_flasher.yaml --transport streamable-http --port 8765

# CLI
lb --manifest manifests/legacy_flasher.yaml caps
lb --manifest manifests/legacy_flasher.yaml run set_baudrate value=115200
lb --manifest manifests/legacy_flasher.yaml run flash_firmware path=C:/fw.bin --confirm
lb --manifest manifests/legacy_flasher.yaml ui shot --out s.png   # 附 SoM 標號
```

Claude Code / Claude Desktop 的 MCP 設定範例放在 `examples/mcp.json`。

> **Windows 注意事項（工程判斷）：** server 必須跑在使用者已登入的互動桌面，不能包成 Windows Service，因為 session 0 碰不到 GUI，L2 和 L3 都會失效。

## Capability 查詢介面

| MCP tool | CLI | 回傳 |
|---|---|---|
| `get_capabilities` | `lb caps` | 每個動作在各層的支援狀態、優先層、confidence、backend 健康度與 coverage、目前畫面狀態、低階操作支援哪些層 |
| `available_now` | `lb caps --now` | 目前畫面狀態下可執行的動作 |
| `describe` | `lb describe X` | 參數 schema、副作用、verify 條件、各層 impl 和成功/失敗統計 |
| `explain` | `lb explain X` | fallback 順序、被擋住的層及原因、下一步建議 |
| `get_backends` / `probe` | `lb backends` / `lb probe` | 四層健康度；工具更新後可重新 probe |

支援狀態分成這幾種：`ok`、`degraded`（連續失敗 ≥2 次或 confidence 低）、`needs_restart`（例如 GUI 開著時寫 INI）、`needs_state`、`unsupported`、`probe_failed`。

畫面狀態的限制規則：
- 狀態符合 manifest 宣告：四層都可以用。
- GUI 沒開（`NotRunning`）：只用 L0/L1。
- GUI 開著但在其他狀態（例如燒錄中）：一律不執行，避免干擾進行中的操作。

## MCP 2026-07-28 對應（已對照官方 changelog 與 SDK 原始碼）

| 規格變更 | 實作方式 |
|---|---|
| 移除 protocol session / `initialize` | `open_session` 發 handle，每個 tool 都帶 `session` 參數 |
| `tools/list` 不隨連線變化、要固定順序 | 靜態 tool 清單；`CacheHint` 帶 `ttlMs`/`cacheScope` |
| MRTR（`InputRequiredResult`） | 危險動作用 `Resolve(...)` + `Elicit`，SDK 會封裝並簽章 `requestState` |
| Tasks extension | `tasks/get` / `tasks/update` / `tasks/cancel`；執行中的提問用 `input_required` 加 `tasks/update` |
| `subscriptions/listen` | 動作執行後對 `legacy://{session}/state` 發 resource-updated |
| Logging、Sampling deprecated | log 寫到 stderr 並用 OpenTelemetry span；L3 的 VLM grounding 直接呼叫 provider API，不走 sampling |
| JSON Schema 2020-12 `outputSchema` | 語意動作統一回傳 `ActionResult` 的 `structuredContent` |

## Manifest 支援的 impl

- **L0**：`ini_read` `ini_write` `file_tail` `sqlite_query` `ctypes_call`
- **L1**：`cli`（支援 `progress_re`、`ok_re`、timeout、取消）`com_call`
- **L2**：`uia_value` `uia_select` `uia_text` `uia_click` `uia_set_text` `uia_sequence`
- **L3**：`ocr_region` `vision_select` `vision_sequence`（找目標的順序：template → OCR → VLM；都找不到時請 agent 自己用 screenshot 加 click）

要接你自己的工具，複製一份 manifest 改寫即可，不需要改程式。

## Windows 實機驗證：Audacity 3.7.9（`examples/audacity/`）

完整的驗證矩陣與數據見 [`docs/VERIFICATION.md`](docs/VERIFICATION.md)。

測試環境為 Windows 11、繁中系統、Python 3.12、mcp 2.2.0、pywinauto 0.6.9、rapidocr 1.4.4。MCP 走 stdio，協定版本 2026-07-28。

| 動作 | L0 | L1 pipe | L2 UIA | L3 vision |
|---|---|---|---|---|
| get_version（exe 版本資源） | ✅ 3.7.9.0 | | | |
| enable_scripting / set_language（寫 cfg） | ✅（Audacity 關閉時寫入） | | | |
| get_preference | ✅ | ✅ | | |
| remove_all_tracks（MRTR 確認） | | ✅ | | |
| generate_tone（每層都新增 1 條音軌，以 `increases_by` 驗證） | | ✅ | ✅ | ✅ |
| get_track_count | | ✅ | ✅（TrackView 資料列） | |
| undo / select_all | | ✅ | ✅ | ✅ |
| export_wav（以 L0 檢查檔案大小） | | ✅ | | |
| close_app（處理存檔提示需 `discard=true`） | | ✅ | ✅ | |
| 首次啟動的 4 個對話框 | | | ✅ 自動關閉 | |
| screenshot + SoM | | | | ✅ 100 個以上標號 |

實機上發現、並已修正到程式裡的問題：

1. **Python 3.12 的 `os.path.exists(r"\\.\pipe\X")` 會真的打開 pipe**，讓 Audacity 的 pipe 卡在半連線狀態。現在 L1 的 probe 只列出 `\\.\pipe\` 目錄確認 pipe 存在，不會打開它；連線改為每個行程只建立一條並持續使用。
2. **GUI 開著時寫 cfg 會被覆蓋**：wx 程式關閉時會把設定寫回檔案。所以 `needs_restart` 的正確做法是先關閉程式、再寫入、再重新啟動。
3. **`tasklist` 在中文系統輸出 cp950**，改成用 `oem` 解碼。
4. **Win32 選單不在 UIA 樹裡**，而且系統選單列的名稱會依系統語言變成「系統」「應用程式」。L2 改用 win32 backend 送 `WM_COMMAND` 執行 `menu_select`，找不到才退回用點擊。
5. **wx 的 Edit 不接受 UIA 的 ValuePattern**，`set_field` 失敗時改用鍵盤輸入。
6. **對話框在 UIA 裡是巢狀或看不到的**，改用 win32 列舉該行程的所有頂層視窗。
7. **各層語意必須一致**：原本 L2/L3 的 Tone 是寫進目前選取範圍，L1 則是新增音軌。以 `increases_by` 驗證就抓出這個差異，後來 L2/L3 都補上新增音軌的步驟。
8. **匯出只有 44 bytes 的 WAV（只有檔頭）**被 postcondition 攔下來，沒有被當成成功。
9. **L3 的限制**：視窗被遮住時截到的是別的程式；OCR 會把整條選單列合併成一個文字框，對 L3 選單列的 OCR 也不穩定。已改為操作前先把視窗帶到前景、在合併的文字框內依字元位置推算座標，並優先使用錄好的 template（`templates/`），OCR 退為備援。
10. **UI 操作失敗時會跑 `cleanup`**（例如按 Cancel、按 Esc），避免殘留的 modal 對話框卡住後面所有動作。router 也會在狀態是 `Dialog` 時拒絕執行需要 `MainWindow` 的動作。

## 驗證狀態

- **Windows 實機（Audacity，見上一節）：** L0、L1、L2、L3、MCP stdio、MRTR、首次啟動對話框自動處理都實際跑過。
- **已測試（Linux 與 Windows，19 個 pytest 全過）：** L0/L1 對 demo 工具實際執行；L2/L3 用 sim；MCP 走 stdio JSON-RPC 用 mcp 2.2.0 的 client 端對端測試，涵蓋協定版本 2026-07-28、server/discover 宣告 Tasks extension、MRTR 確認的接受與拒絕、Tasks 的 input_required 往返、沒有 Tasks 時的同步執行加 progress。Streamable HTTP 手動 smoke test 通過。
- **未測試：** COM（`com_call`）、`ctypes_call`，以及在實機上走 Tasks extension（Audacity 的 manifest 沒有長時間動作；Tasks 在 Linux 上已做過端對端測試）。

## 已知限制

- `MCPServer` 只能為每個 method 設一種 cache hint，所以 `resources/read` 一律 `ttl 0`，manifest resource 沒辦法單獨設長 TTL。
- Tasks 的 `notifications/tasks` 推播沒有實作，目前只支援輪詢（規格的預設方式）。
- 沒有 Tasks 的同步路徑無法在執行中途提問，遇到會安全地拒絕並回報。
- SoM 候選框目前是 UIA、OCR、輪廓的簡單合併；不同工具可能需要調參數，或錄 template。

## 授權

MIT License，Copyright (c) 2026 Ming-Chih Lung (Taiwan, Tainan)。全文見 [`LICENSE`](LICENSE)。
