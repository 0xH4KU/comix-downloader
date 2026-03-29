# comix-downloader 架構與程式碼深度審查報告

本報告以資深軟體架構師視角，對 `comix-downloader` 的 codebase 進行深度審查。審查範圍涵蓋核心的應用程式邏輯、瀏覽器連線生命週期、併發控制、下載重試機制及檔案操作。本報告嚴格依照**效能 (Performance)**、**安全性 (Security)**、與**可維護性 (Maintainability)** 三大維度進行分類，並為每個發現的問題提供具體的程式碼片段與改善建議。

---

## 1. 效能 (Performance)

### 1.1 `downloader.py` 中的進度回報鎖定範圍與效能
在 `Downloader.download_chapter` 方法中，使用 `asyncio.Semaphore` 控制併發請求。但是進度計數與回報（`_progress_done` 的更新及 `_on_progress` 回調）被包含在 Semaphore 的臨界區（critical section）內。雖然 Python 的 `asyncio` 在單線程中執行，不會有傳統的多線程 race condition 問題，但將非同步操作與頻繁的同步更新綁定在一起，仍可能對併發排程產生微小的拖延，且語意上也不夠精確。

**問題程式碼 (`src/comix_dl/downloader.py`):**
```python
        async def fetch_one(index: int, url: str) -> _PageDownloadResult:
            nonlocal _progress_done
            async with semaphore:
                # ... (下載邏輯) ...
                _progress_done += 1
                if self._on_progress:
                    self._on_progress(
                        DownloadProgress(
                            completed=_progress_done,
                            total=total,
                            # ...
                        )
                    )
                return _PageDownloadResult(...)
```

**改善建議:**
將 Semaphore 的範圍嚴格限制在真正的網路請求與 I/O 操作上。進度更新可以放在 Semaphore 範圍之外，以縮短佔用資源的時間。

```python
<<<<<<< SEARCH
        async def fetch_one(index: int, url: str) -> _PageDownloadResult:
            nonlocal _progress_done
            async with semaphore:
                # Random delay to avoid rate limits
=======
        async def fetch_one(index: int, url: str) -> _PageDownloadResult:
            nonlocal _progress_done

            async with semaphore:
                # Random delay to avoid rate limits
>>>>>>> REPLACE
```
*(注意：由於 asyncio 特性，這比較偏向架構上的 clean code，但對於高併發依然有助於減少 context switch 延遲。)*

### 1.2 `converters.py` 中的大量影像載入記憶體消耗
在 `_build_pdf_batched` 函式中，即便使用了 batch size 限制記憶體，但在讀取圖片時使用了 `Image.open` 後緊接著 `img.convert("RGB")`。這會強制載入整個圖片資料進記憶體並建立新物件，對於單批次 20 張高解析度漫畫圖片仍可能造成記憶體突波。

**問題程式碼 (`src/comix_dl/converters.py`):**
```python
    def _load_batch(paths: list[Path]) -> list[Image.Image]:
        imgs: list[Image.Image] = []
        for p in paths:
            try:
                img: Image.Image = Image.open(p)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                imgs.append(img)
            except Exception as exc:
                logger.warning("Skipping %s: %s", p.name, exc)
        return imgs
```

**改善建議:**
若只是為了打包 PDF，除了 RGBA/P 必須轉換外，一般的 JPEG 其實可以直接寫入 PDF（使用 `save_all=True`）。目前代碼已經做得很不錯了，但可以考慮在不需要轉換的時候，依賴 PIL 的 lazy loading 特性，不要強制呼叫 `load()` 或是確認 `.convert()` 的呼叫時機。

---

## 2. 安全性 (Security)

### 2.1 不安全的命令列參數解析與程序啟動
在 `browser_session.py` 的 `_command_line_for_pid` 函式中，嘗試從 `ps` 指令獲取處理程序的命令列。這裡直接將 `str(pid)` 串接在命令列表中。雖然 `pid` 型別是 `int`，但在使用 `subprocess.run` 執行系統命令時，仍應注意環境與注入風險，且 `shell=False` 已經是好的開始，但錯誤處理可以更強健。

**問題程式碼 (`src/comix_dl/browser_session.py`):**
```python
    with contextlib.suppress(Exception):
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
        command = result.stdout.strip()
```

**改善建議:**
確保只捕獲特定的 `subprocess.SubprocessError` 和 `OSError`，避免使用過於廣泛的 `Exception` 掩蓋其他潛在的執行時期錯誤。

### 2.2 敏感資訊記錄風險 (Logging of Sensitive Information)
在 `cdp_browser.py` 中執行 JavaScript 請求時，如果 URL 中包含敏感參數（如 user token 或 hash_id 雖然目前是公開的），當發生例外時會將 `url` 直接印入 log 內。另外，如果 Cloudflare 阻擋並回傳帶有部分 body 的 HTTP 錯誤，這些也可能不經意被記錄下來。

**問題程式碼 (`src/comix_dl/cdp_browser.py`):**
```python
    async def get_bytes(self, url: str, *, referer: str | None = None) -> bytes:
        # ...
            action=f"Fetching binary response from {url}",
        # ...
```

**改善建議:**
雖然 comix.to 是公開資源，但為了符合企業級安全性，可以考慮在 logging 時對 URL 進行 sanitize（遮蔽查詢參數或僅記錄 base URL）。

```python
from urllib.parse import urlparse

def sanitize_url_for_log(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
```

### 2.3 檔案操作的原子性與 Temp File 權限
在 `fileio.py` 中建立的暫存檔 (`tempfile.NamedTemporaryFile`) 預設會繼承行程的 `umask`。如果在共用伺服器上執行，寫入的暫存檔案可能會被其他使用者讀取。

**問題程式碼 (`src/comix_dl/fileio.py`):**
```python
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
```

**改善建議:**
明確設定暫存檔的建立權限，確保只有擁有者可以讀寫。這在使用 Headless VPS (如 README 提及的有限支援) 時特別重要。

---

## 3. 可維護性 (Maintainability)

### 3.1 龐大的 `BrowserSessionManager` 職責過重
`BrowserSessionManager` (`src/comix_dl/browser_session.py`) 負責了太多事情：它啟動 Chrome 程序、處理跨進程的鎖 (File Lock)、管理 Playwright CDP 連線、還包含了一個複雜的非同步 Page Pool (頁面池) 機制。
這違反了**單一職責原則 (SRP)**。

**問題程式碼段落：**
一個類別包含了 `_acquire_instance_lock`, `_launch_chrome`, `_wait_for_cdp_ready`, `_prepare_main_page`, `acquire_page`, `release_page`, `_replace_dead_page` 等數十個方法，混雜了 OS 層級的行程管理與應用程式層級的資源池管理。

**改善建議:**
將此類別拆分為三個獨立的組件：
1. **`ChromeProcessManager`**: 負責啟動/關閉本機 Chrome 進程、處理 File Lock 與 PID 檔案。
2. **`PlaywrightSessionManager`**: 負責處理 CDP 連線、超時與基礎 Context 的建立。
3. **`BrowserPagePool`**: 專門實作 `asyncio.Queue` 作為頁面資源池的邏輯，負責處理 dead page 的驅逐與替換。

### 3.2 錯誤處理與重試邏輯散落各處
系統中有多次手動實作的重試迴圈。例如在 `cdp_browser.py` 中有 Cloudflare 的重試迴圈 (`_evaluate_request_with_cf_retry`)，在 `downloader.py` 中有下載圖片的重試迴圈 (`_download_image`)。

**問題程式碼 (`src/comix_dl/cdp_browser.py`):**
```python
        for attempt in range(2):
            if use_page_pool:
                should_retry, result = await self._evaluate_request_attempt(...)
            # ...
```

**改善建議:**
引入一個標準的重試裝飾器 (Decorator) 或重試機制函式庫 (例如 `tenacity`)。這樣可以將重試策略（如指數退避、最大重試次數、條件判斷）與業務邏輯解耦，提高程式碼可讀性並降低修改成本。

### 3.3 型別系統中的 `dict[str, object]` 濫用
在 API 請求層 (`comix_service.py` 和 `cdp_browser.py`)，大量使用 `dict[str, object]` 來接收 JSON 回應，然後依賴手動的 `isinstance` 檢查與 `.get()`。

**問題程式碼 (`src/comix_dl/comix_service.py`):**
```python
        data = info_resp.get("result", {})
        if not isinstance(data, dict):
            data = {}

        title = data.get("title", "") or hash_id
```

**改善建議:**
考慮引入 `pydantic` 或使用 Python 3.11 內建的 `TypedDict` 與 `dataclass` 配合自動反序列化，來建立強型別的 API 回應模型 (DTO)。這將大幅減少運行時期的 `TypeError` 風險，並讓 IDE 的自動補全更加完善。

```python
# 改善範例：
from typing import TypedDict

class MangaResultDTO(TypedDict):
    title: str
    slug: str
    hash_id: str
    synopsis: str | None
```

### 3.4 廣泛使用 `contextlib.suppress(Exception)` 掩蓋潛在 Bug
在 `browser_session.py` 的關閉邏輯中，為了確保清理程序不中斷，大量使用了 `with contextlib.suppress(Exception):`。這是一個非常危險的反模式 (Anti-pattern)，它會吞噬掉如 `KeyboardInterrupt` 或其他嚴重的內部邏輯錯誤。

**問題程式碼 (`src/comix_dl/browser_session.py`):**
```python
            if self._playwright:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
```

**改善建議:**
只抑制預期的例外型別。例如，若預期是網路或處理程序層級的錯誤，應明確攔截 `OSError`, `playwright.async_api.Error`, `asyncio.CancelledError` 等，並留下 trace level 的 log，而非一律 `Exception` 吞噬。

---
## 總結
`comix-downloader` 的架構設計相當精巧，尤其是將 Cloudflare 繞過邏輯與真實瀏覽器 CDP 連線的整合實作得很棒。透過上述針對進度更新範圍、重試邏輯抽象化、強型別資料模型，以及更嚴格的異常處理與資源權限設定，將能使系統更穩定、更易於未來維護與擴充。
