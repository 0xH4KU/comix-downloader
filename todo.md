# comix-downloader 改進 Todo

> **核心策略**：把這個 repo 視為「框架 + comix.to reference adapter」。框架是跨 fork 延續的核心資產（CLI、CF bypass、Chrome session、page pool、downloader、converter、history、settings），site 邏輯是可棄品。
>
> **未來情境**：comix.to 因不可抗力關站時 → archive 此 repo → fork 出新 repo → 替換 `sites/` 下唯一實作 → 改 package name → 即可運營新站。
>
> 規模：源碼 5463 行 / 24 檔，測試 4546 行 / 19 檔
> 本檔分四波次，務必按序處理。第一波未完成前，其他波次的改動可能白工。

---

## 第一波：Framework / Site 邊界拆分（核心解耦）

> 這波的目標是把 repo 切成「core/（純框架，fork 時整套帶走）」與「sites/comix_to.py（reference adapter，archive 時丟棄）」。完成後刪掉 `sites/comix_to.py` 應該剩下純框架。

### [F-1] 定義 SiteAdapter Protocol 與 sites/ 目錄結構

- **目標**：建立 framework 與 site 之間的合約
- **介面草案**
  ```python
  class SiteAdapter(Protocol):
      name: str                       # "comix.to"
      mirrors: list[str]              # 主 + 備援域名
      needs_browser: bool             # 整站是否強制走 CdpBrowser

      def matches_url(self, url: str) -> bool: ...
      def parse_identifier(self, url_or_slug: str) -> str | None: ...

      async def on_engine_ready(self, engine) -> None: ...    # 注入簽名 IIFE 等 hook
      async def probe_alive(self, engine) -> bool: ...        # mirror 探測

      async def search(self, engine, query: str, limit: int) -> list[SearchResult]: ...
      async def get_series(self, engine, identifier: str) -> SeriesInfo: ...
      async def get_chapter_images(self, engine, chapter_id) -> ChapterImages: ...

      def deduplicate(
          self, chapters: list[ChapterInfo]
      ) -> tuple[list[ChapterInfo], list[DedupDecision]]: ...
  ```
- **新增檔案**
  - `src/comix_dl/sites/__init__.py`：registry（`register / get_for_url / get_active`）
  - `src/comix_dl/sites/base.py`：`SiteAdapter` Protocol + `AdapterContext` dataclass
  - `src/comix_dl/sites/_template.py`：fork 樣板（帶 TODO 註解，照抄即可寫新站）
- **狀態**：[ ]

---

### [F-2] 重組 package 內部結構：core/ + sites/

- **目標**：讓 framework 內部 import 完全不認識 `comix` 字眼，fork 時改 package name 只動一處
- **目標結構**
  ```
  src/comix_dl/
    __init__.py
    __main__.py
    core/                       ← 純框架，fork 時整套帶走
      cli/
        __init__.py
        flows.py
        interactive.py
        display.py
      application/
        download_usecase.py
        query_usecase.py
        cleanup_usecase.py
        download_reporting.py
        session.py
      engines/
        cdp_browser.py          ← 通用 CF-aware browser engine
        browser_session.py      ← 待 P1-4 拆分
        http_engine.py          ← 階段二再加
      downloader.py
      converters.py
      errors.py
      fileio.py
      logging_utils.py
      notify.py
      history.py
      settings.py
      config.py                 ← 去掉所有 site URL
    sites/
      __init__.py               ← registry
      base.py                   ← Protocol
      _template.py              ← fork 樣板
      comix_to.py               ← 唯一實作
  ```
- **改動原則**
  - `core/` 內部全用相對 import（`from ..engines import ...`）
  - 不出現 `comix` / `comix.to` / 任何 site URL
  - 對外（CLI entry / pyproject.toml）保留 `comix_dl` 名稱不動
- **狀態**：[ ]

---

### [F-3] 把 comix.to 邏輯整段下沉到 sites/comix_to.py

整檔內容應包含以下，全部從現有檔案搬遷：

- **URL / 識別**
  - mirror domain list（含當前 `comix.to` 與已知備援）
  - URL pattern 正則
  - URL / slug → identifier 解析（從 `query_usecase.py` 搬）
- **API**（從 `comix_service.py` 搬）
  - `/api/v2/manga` / `/api/v2/manga/{hash_id}` / `/api/v2/manga/{hash_id}/chapters` / `/api/v2/chapters/{chapter_id}`
  - JSON schema 解析（`result.items[].hash_id` / `chapter_id` / `images[].url` 等）
  - dedup 三維邏輯（number + language + subtitle，含 `_resolve_unnamed_only` / `_resolve_named_with_unnamed` / `_pick_best`）
- **CF 配置**（從 `config.py` 搬）
  - `cf_titles` / `cf_selectors` / probe URL
- **簽名 IIFE 提取**（從 `cdp_browser.py:362-446` 搬，**同時做 P0-1 硬化與 P0-2 防線**）
  - 兩段重複 JS 抽成單一函式
  - 對抽到的 IIFE 算 hash 並 disk-cache 到 `~/.config/comix-dl/sites/comix_to/iife.cache.json`，失敗 fallback 上次成功版本
  - hash 比對白名單，異常 IIFE 拒絕 eval
  - 失敗訊息明確：「comix.to JS bundle 結構變化，請升級或回報 issue」
  - 透過 `on_engine_ready` hook 注入，framework 不認識它
- **狀態**：[ ]

---

### [F-4] CdpBrowser 改造成通用 CF-aware browser engine

- **問題**：當前 CdpBrowser 知道太多 comix.to 細節（probe URL、簽名注入時機、`/chapters` 簽名套用）
- **目標**：CdpBrowser 只提供
  - `get_json(url) / get_bytes(url) / post_json(url, payload) / fetch_page(url)`
  - `on_engine_ready_hooks: list[Callable]`，由 active adapter 註冊
  - `probe(probe_url, success_check)`，給 adapter 用
  - CF clearance 流程仍在 engine（這是 framework 資產），但「signature injection」、「mirror probe」這些 hook out
- **改動點**
  - `cdp_browser.py:319-348` 中 `if (url.includes('/chapters'))` 的硬編簽名套用 → 移到 adapter
  - adapter 在 `on_engine_ready` 時把 `before_request(url) -> url` transformer 註冊到 engine
  - engine 對每個 request 跑一遍 transformer chain
- **狀態**：[ ]

---

### [F-5] Mirror 切換機制

- **目標**：comix.to 換域名時改配置即可，不改代碼
- **介面**
  - adapter 提供 `mirrors: list[str]` 與 `probe_alive(engine) -> bool`
  - `application/session.py` 啟動時依序探測，第一個成功的當 active mirror
  - 探測結果寫 `~/.config/comix-dl/mirror_state.json`：`{"comix.to": {"active": "https://comix.cc", "checked_at": "..."}}`
  - 下次啟動先試上次成功的，失敗才重探
- **CLI**
  - 加 `--mirror <url>` 旗標可手動 override
  - `doctor` 命令顯示當前 active mirror 與最近探測歷史
- **狀態**：[ ]

---

### [F-6] AppConfig 解綁 site URL

- **問題**：當前 `AppConfig.service.base_url` 是 framework 配置但塞了 site URL
- **改動**
  - 從 `AppConfig` 移除 `service.base_url`、`browser.cf_titles`、`browser.cf_selectors` 等 site-specific 欄位
  - 改放到 `SiteAdapter` 實例屬性 / 方法回傳值
  - `AppConfig` 只保留純 engine 配置（timeouts、page pool size、download concurrency、retry policy）
- **狀態**：[ ]

---

### [F-7] 寫 FORKING.md

- **目標**：fork 流程傻瓜化，這是這套策略的核心配套文檔
- **內容**
  - 為什麼是「framework + reference adapter」設計
  - Step-by-step：複製 repo → grep replace `comix_dl` → 改 `pyproject.toml` 的 name / entry point → 刪 `sites/comix_to.py` → 抄 `sites/_template.py` → 實作 SiteAdapter 各方法 → 跑測試
  - 哪些檔案絕對不該動（core/）
  - 哪些檔案必須改（sites/、pyproject.toml、README、install.sh）
  - 常見坑（簽名邏輯、CF 假設、JSON schema 差異）
- **狀態**：[ ]

---

### [F-8] 寫 sites/_template.py 樣板

- **目標**：fork 時抄這個檔，照 TODO 填
- **內容要求**
  - 每個 Protocol 方法都有 stub + 說明
  - 註明哪些 hook 是 optional（例如 `on_engine_ready` 可留空）
  - 包含「最小可運作 adapter」範例（假設目標站是 plain JSON API、無 CF、無簽名）
  - 包含「複雜 adapter」範例註解（簽名 IIFE 提取、CF 處理、dedup override）
- **狀態**：[ ]

---

## 第二波：架構債清理

> 這波可在第一波完成後獨立進行。其中 P0-3 是第一波的前置依賴，建議插隊在 F-1 之前先做。

### [P0-3] 錯誤類型化（前置依賴，提早做）

- **問題**
  - `errors.py` 已設計 5 種 domain error，但 raise 點仍大量用 generic `Exception` + 字串檢查
  - SiteAdapter 介面要用 typed exception 才能設計乾淨，這項要先做
- **證據**
  - `src/comix_dl/downloader.py:137-145` `_describe_download_error` 用 `"timed out" in message`、`"HTTP 403" in message`
  - `src/comix_dl/cdp_browser.py:73` `_is_cf_access_error` 同樣
  - `src/comix_dl/comix_service.py:136-143` `_describe_api_error` 同樣
- **建議行動**
  - 新增子類：`Http403Error` / `BrowserTimeoutError` / `PagePoolUnavailableError` / `SchemaMismatchError`
  - `_describe_*_error` 改成 dispatch by exception type，僅做訊息渲染
  - 確保所有跨層 raise 都是 `errors.py` 子類
- **改動成本**：中（涉及 3 個模組）
- **狀態**：[ ]

---

### [P1-3] Downloader 進度回調封裝破壞

- **問題**：`download_usecase.py:172` 偷改 `downloader._on_progress` 屬性
- **建議行動**
  - `Downloader.download_chapter` 加 `on_progress=None` 參數，per-call override
  - 移除 use case 中的私屬性賦值
- **改動成本**：低
- **狀態**：[ ]

---

### [P1-4] 拆 `browser_session.py`（714 行單檔）

- **問題**：單檔同時負責 Chrome subprocess 生命週期、跨平台 file lock、Playwright CDP 接線、page pool 管理
- **建議行動**
  - 拆三個檔（落在 `core/engines/` 下）：
    - `chrome_process.py`：subprocess + 跨平台 lock + atexit
    - `cdp_connection.py`：Playwright wiring
    - `page_pool.py`：pool management
  - `BrowserSessionManager` 變成 facade
- **改動成本**：中高
- **狀態**：[ ]

---

### [P1-5] 清模組級可變狀態 `_process_state`

- **問題**：`browser_session.py:51` 的 `_process_state = _ChromeProcessState()` 是 module-level singleton
- **建議行動**
  - atexit 清理路徑改用 weakref registry 或 contextlib.ExitStack
  - 讓 instance 自負其責，不依賴全域變數
- **改動成本**：中
- **狀態**：[ ]

---

### [P1-6] CLI 互動與渲染分離

- **問題**
  - `cli/flows.py`（585 行）混合 Rich 渲染與互動控制
  - `download_usecase.py:312-326` 直接呼叫 `HistoryRepository` 和 `notify`，沒走 abstract port
- **建議行動**
  - 為 history / notify 定義 `HistoryPort` / `NotifyPort` Protocol，注入 use case
  - flows.py 拆成 `cli/prompts.py`（純互動）+ `cli/render.py`（Rich 渲染）
- **改動成本**：中
- **狀態**：[ ]

---

## 第三波：HTTP Fast Path

### [P1-2] HTTP fast path 與 HttpEngine

- **問題**：當前 `comix-dl info` / `list` / `search` 等不下載圖的命令也要啟一顆 Chrome
- **建議行動**
  - 在 `core/engines/http_engine.py` 加 httpx-based engine
  - 共用 cf_clearance cookie（從 Chrome session 序列化到 disk）
  - SiteAdapter 透過 `needs_browser_for(action)` 決定走哪個 engine
  - 失敗時自動 fallback 到 Chrome
- **預期收益**：search / info / list 啟動時間從 5-10s 降到 <1s；VPS / Docker 場景變得可用
- **改動成本**：中高
- **狀態**：[ ]

---

## 第四波：品質、運營、可發現性

### [P2-1] 圖片優化與 PDF 合併走 executor

- **問題**：Pillow / pypdf 同步操作可能阻塞 event loop
- **建議行動**：覆查 `converters.py`（342 行），CPU-heavy 操作包進 `asyncio.to_thread()` 或 ProcessPoolExecutor
- **改動成本**：低
- **狀態**：[ ]

---

### [P2-2] 反爬退避策略

- **問題**：只有靜態 random delay，無 429/503 自適應退避，無 site-level token bucket
- **建議行動**
  - 在 `core/engines/` 包一層 `RateLimiter`（token bucket）
  - 觀察 429/503 → 指數退避 + 全域減速
  - structured log 記錄 backoff 事件
- **改動成本**：中
- **狀態**：[ ]

---

### [P2-3] 測試盲點 + Contract test

- **問題**
  - browser_session.py / cli/interactive.py / notify.py 自承低覆蓋
  - **最致命**：沒有針對 IIFE 簽名提取的 contract test
- **建議行動**
  - GitHub Actions 加 cron 每日跑「對 live comix.to 抽 IIFE」smoke test，失敗自動開 issue
  - `browser_session.py` 拆完後補單元測試（拆完更好測）
  - 為 SiteAdapter Protocol 寫 conformance test，新 adapter 跑通即合格
- **改動成本**：中
- **狀態**：[ ]

---

### [P2-4] doctor 命令升級為健康度報告

- **問題**：當前只檢查 Python / 依賴 / Chrome / 輸出目錄
- **建議行動**：加上
  - 當前 active mirror + 最近探測結果（F-5 後可用）
  - 能否拉到 site 的 JS chunks（site-specific，由 adapter 提供 health check hook）
  - 簽名 IIFE 是否能解析（site-specific）
  - CF cookie 是否有效
  - 一次端到端 search smoke
- **改動成本**：低
- **狀態**：[ ]

---

### [P2-5] 發布管道偏向開發者

- **問題**：install.sh 是 git clone + pip install -e；沒上 PyPI；沒 Docker
- **建議行動**
  - 上 PyPI（`pip install comix-dl`）
  - 加 trove classifiers
  - GitHub Actions release workflow（git tag → PyPI publish）
  - Docker 鏡像內建 xvfb + Chromium
- **改動成本**：中
- **狀態**：[ ]

---

### [P2-6] 觀察性（structured log 強化）

- **問題**：缺 request_id / span / trace
- **建議行動**
  - 每次 download 命令分配 `run_id`，注入所有 structured log
  - chapter 內加 `chapter_run_id`
  - doctor 可附 last_run summary
- **改動成本**：低
- **狀態**：[ ]

---

### [P2-7] dedup 對冷僻 chapter 的 round-trip

- **問題**：`_pick_best` 只在所有 image_count=0 時才 fallback 拉每章 detail
- **建議行動**：先量測實際 round-trip 數，超預期才改成預載
- **改動成本**：低
- **狀態**：[ ]

---

### [P2-8] 貢獻者文檔

- **問題**：CONTRIBUTING.md 只 3KB，不夠
- **建議行動**
  - 加「第一個 PR 的 30 分鐘路線」段
  - 與 FORKING.md（F-7）區分清楚：CONTRIBUTING 給「貢獻 comix.to adapter 改進」的人，FORKING 給「複製整套去做新站」的人
- **改動成本**：低
- **狀態**：[ ]

---

## 已合併或廢止的舊項目

> 為避免讀舊版本的人困惑，原 P0-1 / P0-2 / P1-1 已併入第一波，記錄如下：

- ~~P0-1（簽名 IIFE 提取硬化）~~ → 併入 [F-3]，下沉到 `sites/comix_to.py` 時一併硬化
- ~~P0-2（eval 站方 JS 無防線）~~ → 併入 [F-3]，hash 校驗 + disk cache + 降級訊息
- ~~P1-1（站點抽象層）~~ → 升級為整個第一波（F-1 ~ F-8）

---

## 推薦執行順序

| 順序 | 項目 | 理由 |
|------|------|------|
| 1 | P0-3 | 前置依賴，typed exception 是 adapter 介面的基礎 |
| 2 | F-1 | 先建 Protocol，後續搬遷有目標 |
| 3 | F-2 | 目錄重組，內部 import 全改 |
| 4 | F-4 | CdpBrowser 通用化（先做這個，F-3 才搬得乾淨） |
| 5 | F-6 | AppConfig 解綁，與 F-4 配套 |
| 6 | F-3 | 主搬遷，含 P0-1 / P0-2 硬化 |
| 7 | F-5 | mirror 切換 |
| 8 | F-8 | 樣板檔 |
| 9 | F-7 | FORKING.md 文檔（這時介面才穩定） |
| 10+ | 第二、三、四波各項 | 按各自波次推進 |

---

## 第一波完成後的驗收標準

- [ ] `core/` 內 `grep -ri "comix" .` 只在註解 / docstring / 對外字串可能出現，無功能性 import / URL / schema 引用
- [ ] 刪除 `sites/comix_to.py` 後，`pytest core/` 全綠（可能要 mock SiteAdapter）
- [ ] 寫一個假站 `sites/_demo.py` 透過 SiteAdapter Protocol，能跑通 search → list chapters → download flow（用 fixture 餵假數據）
- [ ] FORKING.md 中的「fork 步驟」實測可走通：複製 repo、改名、寫新 adapter、跑通基本流程
- [ ] mirror 切換可手動驗證：把當前 base URL 故意打錯，自動切到備援；`doctor` 命令顯示正確狀態
- [ ] 簽名 IIFE 提取失敗時錯誤訊息明確，不再靜默回 0 章節

---

## 索引：按檔案聚合

便於開檔時知道有哪些待辦在等：

- `cdp_browser.py`：F-3, F-4, P0-3
- `downloader.py`：P0-3, P1-3
- `comix_service.py`：F-3（整檔搬遷至 sites/comix_to.py）, P0-3
- `browser_session.py`：F-2（移到 core/engines/）, P1-4, P1-5, P2-3
- `application/download_usecase.py`：P0-3, P1-3, P1-6
- `cli/flows.py`、`cli/__init__.py`：F-2, P1-6
- `application/session.py`：F-2, F-5, P1-2
- `config.py`：F-6
- `converters.py`：F-2, P2-1
- `logging_utils.py`：F-2, P2-6
- `pyproject.toml` / install.sh：F-2（package path 確認）, P2-5
- 新增檔案：`sites/__init__.py`, `sites/base.py`, `sites/_template.py`, `sites/comix_to.py`, `FORKING.md`
- doctor 命令（散落）：F-5, P2-4

---

## 待討論

> 此區留給後續想法

- [ ] （待補）
