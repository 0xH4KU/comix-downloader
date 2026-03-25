# TODO

本文件將上一輪架構審查收斂為可執行的工程清單。

目標：
- 提升高可用性
- 降低維護成本
- 消除靜默資料損壞
- 建立可持續演進的測試與發布基線

完成定義：
- 不再產生「成功但內容不完整」的輸出
- 下載、轉檔、恢復、清理流程具備明確狀態邊界
- 多實例行為可預期
- 核心模組有足夠測試保護
- 設定、儲存、CLI、基礎設施解耦

## P0 正確性與資料安全

- [x] 修正大型 PDF 轉檔的靜默截斷問題
說明：目前大於 `batch_size` 的 PDF 在缺少 `pikepdf/pypdf` 時只會保留第一批頁面，這是資料遺失。
完成標準：大章節 PDF 在任何支援路徑下都不能少頁；若依賴不足，必須明確失敗，不允許產出不完整檔案。

- [x] 將 PDF 合併依賴改為正式 runtime 依賴，或改寫為單一路徑實作
說明：目前合併能力依賴 optional import，但 `pyproject.toml` 沒有宣告。
完成標準：`pip install -e .` 後即可可靠產生大 PDF，無隱含依賴。

- [x] 禁止部分下載成功的章節直接進入轉檔
說明：目前只要不是全部失敗就會繼續轉檔，會把缺頁內容固化為輸出檔。
完成標準：章節必須明確區分 `complete / partial / failed / skipped`；只有 `complete` 才能轉檔、寫 history、發通知。

- [x] 為章節下載結果建立明確的 typed result model
說明：目前以例外與計數混合表達狀態，呼叫端無法準確判斷完整性。
完成標準：新增類似 `ChapterDownloadResult` / `SeriesDownloadResult` 的結構，攜帶頁數、失敗數、跳過數、位元組數、狀態。

- [x] 所有圖片寫入改為原子寫入
說明：目前直接寫最終檔名，異常中斷會留下半寫入檔案。
完成標準：使用 `*.part` 暫存、flush/fsync、`os.replace()` 置換，避免半成品被誤判為可恢復檔案。

- [x] 強化 resume 驗證規則
說明：目前只要 `size > 0` 就跳過，會永久接受損壞頁面。
完成標準：至少驗證 magic bytes；必要時可記錄 manifest 內的副檔名、大小、校驗資訊。

- [x] 為部分完成章節寫入 manifest
說明：目前只有 `.complete`，沒有 `.partial` 或失敗明細。
完成標準：新增類似 `chapter.state.json`，記錄成功頁、失敗頁、重試次數、最後錯誤，支援安全恢復。

- [x] 調整 history 與通知邏輯，只記錄真實成功結果
說明：目前可能把不完整下載記成成功。
完成標準：history 與通知內容以 typed result 為準，不再依賴散落的區域變數與推導。

## P0 測試與發布基線

- [x] 修正測試啟動方式，保證從乾淨環境可直接執行
說明：目前直接執行 `pytest` 會因 `src/` layout 導致 import 失敗。
完成標準：本地與 CI 在未手動設 `PYTHONPATH` 的情況下可直接執行測試。

- [x] 補齊 async 測試依賴與設定
說明：目前 `pytest-asyncio` 未穩定生效，async 測試在本地直接報錯。
完成標準：async 測試在標準開發流程可直接跑通，無 `Unknown config option: asyncio_mode` 類警告。

- [ ] 將核心模組覆蓋率提升到可接受水位
說明：目前 `cdp_browser`、`cli/flows`、`converters` 覆蓋率極低，正好是高風險模組。
完成標準：核心模組各自具備最低覆蓋門檻；總覆蓋率門檻由 35% 提升到至少 70%。

- [x] 為大型 PDF 建立回歸測試
完成標準：新增超過 20 頁的測試案例，驗證輸出頁數正確且不截斷。

- [x] 為 partial download recovery 建立回歸測試
完成標準：新增中途中斷、半寫入、損壞檔、重跑恢復的測試組合。

- [x] 為多語章節與重複章節建立回歸測試
完成標準：覆蓋同章節不同語言、不同名稱、不同 image count 的去重規則。

- [x] 為瀏覽器頁面池與併發建立測試
完成標準：驗證頁面池容量、等待、替換 dead page、避免共享主頁競態。

- [x] 調高 CI 失敗門檻
完成標準：CI 對測試、型別檢查、覆蓋率、格式與文件一致性設置明確 gate。

## P1 瀏覽器會話與高可用性

- [x] 移除危險的全域 PID 殺程序邏輯
說明：目前新實例可能把舊實例的 Chrome 當 stale process 清掉。
完成標準：不再依賴單一全域 PID 檔進行粗暴清理。

- [x] 明確定義單實例或多實例策略
說明：目前行為模糊且危險。
完成標準：二選一：
1. 單實例：使用 lock file，第二個實例直接拒絕啟動
2. 多實例：每個實例使用獨立 profile、獨立 session、獨立 pid/lock

- [x] 將瀏覽器生命週期抽為 `BrowserSessionManager`
說明：目前 `CdpBrowser` 同時處理啟動、清理、CF 檢測、頁面池、傳輸。
完成標準：啟動、恢復、關閉、頁面池、CF 管理職責拆開，降低單類別複雜度。

- [x] 為所有 browser request 加入 timeout 與 cancellation
說明：目前 config 定義了 timeout，但未真正接線。
完成標準：`get_json/get_bytes/post_json/fetch_page` 都有 request timeout、取消傳播與明確錯誤類型。

- [x] 403 / CF 過期後自動重置 clearance 並有限次重試
說明：目前 `_cf_cleared` 一旦設為 True 就幾乎不會自癒。
完成標準：偵測 403 或 challenge 信號後能 reset session state，重新檢查 clearance，再重試一次。

- [x] 讓頁面池容量與實際併發配置一致
說明：目前圖片併發可能高於 page pool，最後退回共享主頁，存在競態風險。
完成標準：池滿時等待，不再回退到共享主頁；最大併發由可配置 pool 明確控制。

- [x] 為 dead page replacement 建立更可靠的健康檢查
完成標準：dead page 不會被重複放回池中，replacement 失敗時有可觀測告警與可恢復路徑。

## P1 狀態管理與儲存

- [x] 移除全域 mutable `CONFIG`
說明：目前 `load_settings()` 會直接修改全域 singleton，狀態來源不透明。
完成標準：啟動時載入 immutable runtime config，透過 constructor 注入給 service/use case。

- [x] 抽出 `SettingsRepository`
完成標準：設定讀寫、預設值、驗證、migration 集中在 repository，不再散落在 CLI 與 module import side effects。

- [x] 抽出 `HistoryRepository`
完成標準：history 的讀寫、排序、trim、清除集中管理，CLI 不直接碰 JSON 細節。

- [x] 將 JSON 寫入改為原子寫入
說明：`settings.json` / `history.json` 目前直接覆寫。
完成標準：寫入使用臨時檔與 `os.replace()`，避免損壞設定與歷史紀錄。

- [x] 建立設定驗證與 migration 機制
完成標準：壞格式、未知欄位、舊版本設定都能被安全處理，且有測試覆蓋。

## P1 CLI 與應用層解耦

- [x] 將 `cli/flows.py` 拆分為 application use cases
說明：目前 CLI 同時負責 UI、下載協調、轉檔、history、通知、清理。
完成標準：至少拆為：
1. `application/download_usecase.py`
2. `application/query_usecase.py`
3. `application/cleanup_usecase.py`

- [x] 讓 CLI 僅負責輸入解析與輸出渲染
完成標準：Rich prompt、table、panel 僅存在於 presentation layer。

- [x] 建立明確的 domain errors
說明：目前大量使用 `RuntimeError` 與裸 `Exception`。
完成標準：至少區分：
1. `ConfigurationError`
2. `CloudflareChallengeError`
3. `RemoteApiError`
4. `PartialDownloadError`
5. `ConversionError`

- [x] 移除 application 層對 `console.quiet` 等 UI 細節的依賴
完成標準：quiet mode 由 CLI 控制輸出，不再滲透進底層 use case。

## P1 章節模型與業務規則

- [x] 修正 chapter number 使用 `float` 的設計
說明：`float` 不適合表示章節編號與排序，容易引入語意與比較誤差。
完成標準：改用原始字串 + 正規化排序鍵，或改用 `Decimal`。

- [x] 重寫章節去重 key
說明：目前只看 `number/name`，忽略 `language` 與其他版本訊號。
完成標準：至少將 `language` 納入判斷，並對 unnamed / named / multi-uploader case 建立可測規則。

- [x] 讓去重規則可配置或可觀測
完成標準：下載前能清楚顯示哪些章節被合併、為什麼被合併、保留的是哪個版本。

## P2 效能與資源使用

- [x] 優化 resume 掃描效能
說明：目前每張圖片都做一次 `glob()`。
完成標準：章節開始時一次性建立既有檔名索引，下載過程 O(1) 查找。

- [ ] 降低轉檔期間的暫存檔與記憶體尖峰
完成標準：大章節轉檔有清楚的資源上限與測試，避免 temp file 遺留。

- [ ] 讓章節與圖片併發策略可以按環境調整
完成標準：桌面環境、低資源環境、CI 環境可有不同預設或 profile。

## P2 可觀測性

- [x] 導入結構化 logging
說明：目前 log 雜訊多，但關鍵欄位少。
完成標準：至少包含 series、chapter_id、chapter_title、retry_count、status、bytes、elapsed。

- [x] 為下載摘要與失敗原因建立一致格式
完成標準：CLI summary、history、notification 使用同一套結果來源，不再各自拼湊。

- [x] 為關鍵失敗情境增加明確訊息
完成標準：區分 CF 過期、圖片超時、API 403、頁面池耗盡、PDF merge dependency 缺失。

## P2 文件與專案衛生

- [x] 更新 `ARCHITECTURE.md`
完成標準：文件描述與實作一致，包含新的分層、狀態模型、瀏覽器會話策略、恢復機制。

- [x] 修正 `DEVELOPMENT.md` 中的過期內容
說明：目前仍提到不存在的模組與錯誤命令。
完成標準：開發文件與實際專案結構一致，安裝、測試、執行指令都能直接使用。

- [x] 增加 `CONTRIBUTING.md`
完成標準：定義開發環境、測試要求、PR gate、覆蓋率門檻、回歸測試要求。

- [x] 清理無效設定與死欄位
說明：目前有多個 config 欄位未接線。
完成標準：要嘛落實使用，要嘛移除，避免假配置增加維護成本。

## 建議實作順序

- [x] Milestone 1：修正資料正確性
範圍：大 PDF、partial download、atomic write、resume 驗證、history/notification 修正。

- [x] Milestone 2：修正瀏覽器高可用性
範圍：session manager、timeout、CF reset、page pool、單實例/多實例策略。

- [x] Milestone 3：完成架構解耦
範圍：移除全域 CONFIG、repository/use case 分層、domain errors。

- [ ] Milestone 4：補齊測試與發布門檻
範圍：async test、核心覆蓋率、CI gate、整合測試。

- [ ] Milestone 5：更新文件與發版
範圍：README、ARCHITECTURE、DEVELOPMENT、migration note、release checklist。

## 發版前驗收清單

- [x] `pytest` 可從乾淨環境直接通過
- [ ] 核心模組覆蓋率達標
- [x] 大章節 PDF 無少頁
- [x] 中途中斷可安全恢復
- [x] 多實例策略明確且受測
- [x] 403 / CF 過期可自動恢復或明確失敗
- [x] history / notification 不再誤報成功
- [x] 文件與實作一致
