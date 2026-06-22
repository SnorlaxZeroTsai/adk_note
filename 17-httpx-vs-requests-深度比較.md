# 第十七章：httpx vs requests 深度比較

> **本章目標**：理解 LiteLLM 選擇 httpx 的架構原因，掌握兩個庫的核心差異，能在自己的專案中做出正確選擇。
>
> **預計閱讀時間**：25 分鐘
>
> **前置閱讀**：[第六章：HTTP Handler 統一通訊層](./06-HTTP-Handler統一通訊層.md)
>
> **你會學到**：httpx 的 Transport 架構、async 支援、連線池管理、從 requests 遷移的實務指南

> LiteLLM 為什麼選 httpx 而非 requests 做核心 HTTP 層？這不只是「新 vs 舊」的問題，而是架構需求驅動的選擇。

---

## 一句話結論

**httpx 用在 LLM 請求核心路徑**（需要 async + streaming + 連線池），**requests 只留在 Proxy 管理工具**（簡單的同步 admin 腳本）。

---

## 功能對比總表

| 特性 | requests | httpx | 影響 |
|------|----------|-------|------|
| 同步請求 | ✅ | ✅ | 兩者都行 |
| 非同步請求 | ❌ | ✅ `AsyncClient` | httpx 原生支援，不用 aiohttp 包一層 |
| HTTP/2 | ❌ | ✅ | 多路複用，減少連線數 |
| 串流讀取 | `.iter_lines()` | `.iter_lines()` + `.aiter_lines()` | httpx 有 async 版本 |
| 連線池 | `Session` | `Client` / `AsyncClient` | httpx 更精細的 Limits 控制 |
| Timeout 粒度 | 單一 timeout | connect / read / write / pool 分開設 | httpx 可以只設 connect 5s，read 600s |
| 型別提示 | 部分 | 完整 | httpx 對 IDE 更友好 |
| Transport 可替換 | 難 | ✅ `BaseTransport` | httpx 可以底層換成 aiohttp |
| API 相容性 | — | 刻意模仿 requests | 遷移成本低 |

---

## LiteLLM 的實際架構

```
┌─────────────────────────────────────────────────────┐
│                   LiteLLM Router                     │
├─────────────────────────────────────────────────────┤
│              BaseLLMHTTPHandler                       │
│         （統一所有 Provider 的 HTTP 介面）              │
├─────────────────────────────────────────────────────┤
│     AsyncHTTPHandler          HTTPHandler            │
│     (httpx.AsyncClient)       (httpx.Client)         │
├─────────────────────────────────────────────────────┤
│          LiteLLMAiohttpTransport（預設）               │
│          ↓ 可替換為 httpx 原生 AsyncHTTPTransport      │
├─────────────────────────────────────────────────────┤
│              aiohttp.ClientSession                   │
│         （實際的高效能 TCP 連線池）                     │
└─────────────────────────────────────────────────────┘
```

設計要點：
- httpx 是**介面層**，統一 sync/async API
- aiohttp 是**效能層**，作為 httpx 的 transport 插入
- 結合了 httpx 的好用 API + aiohttp 的高吞吐量

---

## 為什麼不直接用 aiohttp？

| 考量 | 直接用 aiohttp | httpx + aiohttp transport |
|------|----------------|--------------------------|
| 同步支援 | 需要自己包 `asyncio.run()` | httpx.Client 原生同步 |
| API 統一性 | sync/async 兩套完全不同的 API | 同一套 API，底層切換 |
| 可測試性 | Mock 困難 | Transport 可替換，容易 mock |
| 遷移風險 | 全部重寫 | 可以漸進遷移 transport |

---

## 核心差異詳解

### 1. Timeout 設計

**requests（粗粒度）：**
```python
response = requests.post(url, timeout=30)  # 一個數字管所有
# 或 timeout=(connect, read)，只有兩層
```

**httpx（細粒度）：**
```python
timeout = httpx.Timeout(
    timeout=600.0,     # 總超時
    connect=5.0,       # TCP 連線建立
    read=600.0,        # 等待回應 body
    write=5.0,         # 發送 request body
    pool=5.0,          # 等待連線池空位
)
client = httpx.AsyncClient(timeout=timeout)
```

LiteLLM 的設定：
- connect：5 秒（快速失敗，不等慢的 DNS）
- read/total：600 秒（LLM 回應可能很慢，特別是長文本生成）

### 2. 連線池管理

**requests：**
```python
session = requests.Session()
# 預設 pool_connections=10, pool_maxsize=10
# 無法精細控制
```

**httpx：**
```python
limits = httpx.Limits(
    max_connections=100,        # 總連線數上限
    max_keepalive_connections=20,  # 保活連線數
    keepalive_expiry=120,       # 保活超時秒數
)
client = httpx.Client(limits=limits)
```

**LiteLLM 的 aiohttp transport（更極端的連線池配置）：**
```python
connector = aiohttp.TCPConnector(
    limit=1000,               # 總連線數
    limit_per_host=500,       # 每個 host 上限
    keepalive_timeout=120,    # 保活超時
    ttl_dns_cache=300,        # DNS 快取 5 分鐘
    enable_cleanup_closed=True,
)
```

### 3. 串流處理（最關鍵的差異）

**requests（只有同步）：**
```python
with requests.post(url, stream=True) as r:
    for line in r.iter_lines():
        if line:
            data = json.loads(line.decode())
            yield data
```

問題：
- 沒有 async 版本
- 需要手動處理 SSE 格式
- 無法在 event loop 中使用

**httpx（sync + async 都有）：**
```python
# 同步
with httpx.stream("POST", url, json=payload) as response:
    for line in response.iter_lines():
        yield parse_sse(line)

# 非同步
async with client.stream("POST", url, json=payload) as response:
    async for line in response.aiter_lines():
        yield parse_sse(line)
```

LiteLLM 的用法：
```python
# 同步串流
completion_stream = provider_config.get_model_response_iterator(
    streaming_response=response.iter_lines(),
    sync_stream=True,
)

# 非同步串流
completion_stream = provider_config.get_model_response_iterator(
    streaming_response=response.aiter_lines(),
    sync_stream=False,
)
```

### 4. 錯誤處理

**requests：**
```python
try:
    response = session.post(url)
    response.raise_for_status()
except requests.exceptions.HTTPError as e:
    print(e.response.status_code)
    print(e.response.text)  # 完整 body 已讀入記憶體
```

**httpx：**
```python
try:
    response = await client.post(url)
    response.raise_for_status()
except httpx.HTTPStatusError as e:
    # 串流模式下 body 可能還沒讀完
    body = await e.response.aread()  # 需要顯式讀取
    print(mask_sensitive_info(body))  # LiteLLM 會遮罩敏感資訊
```

LiteLLM 的安全錯誤處理：
```python
class MaskedHTTPStatusError(httpx.HTTPStatusError):
    """遮罩 API Key 和 prompt 內容的錯誤類別"""

async def _raise_masked_async_error(e, stream):
    if stream:
        # 串流模式：5 秒內讀取錯誤 body，超時就放棄
        body = await _safe_aread_response(
            e.response, timeout=5.0
        )
        raise MaskedHTTPStatusError(
            e, message=mask_sensitive_info(body)
        ) from None
```

### 5. Transport 可替換（httpx 獨有）

```python
# 正常使用 httpx 原生 transport
client = httpx.AsyncClient()

# 換成 aiohttp（更高吞吐量）
transport = LiteLLMAiohttpTransport(connector=connector)
client = httpx.AsyncClient(transport=transport)

# 測試時用 mock transport
transport = httpx.MockTransport(handler)
client = httpx.AsyncClient(transport=transport)
```

這是 httpx 最強大的設計——**介面和實作完全解耦**。

---

## 效能比較

### 基準數據（典型場景）

| 指標 | requests | httpx (原生) | httpx + aiohttp |
|------|----------|-------------|-----------------|
| 單次請求延遲 | ~基準 | ~同 requests | ~同 requests |
| 並發 100 請求 | 需要線程池 | 原生 async | 原生 async |
| 連線複用 | Session 層級 | Client 層級 | Connector 層級 |
| 記憶體（串流） | 整段讀入 | 逐行消費 | 逐行消費 |
| HTTP/2 多路複用 | ❌ | ✅ | 視 transport |

### 為什麼並發很重要？

LLM Gateway 的典型場景：
- 同時處理數百個 completion 請求
- 每個請求持續數秒到數十秒（等 LLM 生成）
- 串流回應需要長時間保持連線

用 requests + 線程：
```python
# 100 個並發 = 100 個線程 = 大量記憶體 + context switch 開銷
with ThreadPoolExecutor(max_workers=100) as pool:
    futures = [pool.submit(requests.post, url, json=payload) for _ in range(100)]
```

用 httpx async：
```python
# 100 個並發 = 100 個 coroutine = 極低記憶體 + 無 context switch
async with httpx.AsyncClient() as client:
    tasks = [client.post(url, json=payload) for _ in range(100)]
    responses = await asyncio.gather(*tasks)
```

---

## LiteLLM 中的 Client 生命週期管理

### 問題：Client 建立是昂貴的

每次建一個新 `httpx.AsyncClient` 意味著：
- TCP 連線池初始化
- SSL context 設定
- DNS 解析

### 解法：TTL Cache

```python
# 全域 client 快取，1 小時 TTL
_DEFAULT_TTL_FOR_HTTPX_CLIENTS = 3600

# Cache key = provider 名 + 自定義參數（如 SSL 設定）
litellm.in_memory_llm_clients_cache.get_or_create(
    cache_key=f"{provider}:{ssl_config}",
    factory=lambda: create_client(...),
    ttl=3600,
)
```

### 連線錯誤自動重建

```python
async def post(self, url, ...):
    try:
        return await self.client.post(url, ...)
    except (httpx.RemoteProtocolError, httpx.ConnectError):
        # 連線壞了，建新 client 重試一次
        new_client = self.create_client(timeout=timeout)
        try:
            return await new_client.post(url, ...)
        finally:
            await new_client.aclose()
```

### 程式結束時清理

```python
# atexit 註冊清理函式
def register_async_client_cleanup():
    def cleanup():
        loop = asyncio.new_event_loop()
        loop.run_until_complete(close_litellm_async_clients())
        loop.close()
    atexit.register(cleanup)
```

---

## SSL/TLS 設計

### SSL Context 快取

建立 SSL context 也是昂貴操作，LiteLLM 做了快取：

```python
_ssl_context_cache: Dict[
    Tuple[Optional[str], Optional[str], Optional[str]],
    ssl.SSLContext
] = {}
# key = (ca_file, security_level, ecdh_curve)
```

### 設定優先順序

```
1. 函式參數 ssl_verify
2. 環境變數 SSL_VERIFY
3. 全域設定 litellm.ssl_verify
4. 環境變數 SSL_CERT_FILE
5. certifi 預設 CA bundle
```

---

## requests 還在哪裡用？

| 位置 | 用途 | 為什麼不換 httpx |
|------|------|----------------|
| `proxy/client/` | Proxy admin CLI | 簡單同步腳本，不需要 async |
| `integrations/sqs.py` | SQS 整合 | 外圍功能，不在核心路徑 |
| `integrations/s3_v2.py` | S3 上傳 | 同上 |
| `llms/bedrock/files/` | Bedrock 檔案操作 | 特定 provider 的一次性操作 |
| `llms/databricks/` | Databricks 工具 | 同上 |

規律：**非核心路徑、低頻率、純同步**的場景留 requests，不值得為它們引入 async 改造。

---

## 遷移指南：requests → httpx

如果你的專案想從 requests 遷移到 httpx：

### 基本替換

```python
# requests
import requests
resp = requests.get("https://api.example.com/data")
resp.raise_for_status()
data = resp.json()

# httpx（幾乎一樣）
import httpx
resp = httpx.get("https://api.example.com/data")
resp.raise_for_status()
data = resp.json()
```

### Session → Client

```python
# requests
session = requests.Session()
session.headers.update({"Authorization": "Bearer xxx"})
resp = session.post(url, json=payload)

# httpx
client = httpx.Client(headers={"Authorization": "Bearer xxx"})
resp = client.post(url, json=payload)
client.close()  # 或用 with 語法
```

### 加入 async

```python
# httpx async（requests 完全沒有對應）
async with httpx.AsyncClient() as client:
    resp = await client.post(url, json=payload)
    data = resp.json()
```

### 串流遷移

```python
# requests
with requests.post(url, json=payload, stream=True) as r:
    for line in r.iter_lines(decode_unicode=True):
        process(line)

# httpx sync
with httpx.stream("POST", url, json=payload) as r:
    for line in r.iter_lines():
        process(line)

# httpx async
async with client.stream("POST", url, json=payload) as r:
    async for line in r.aiter_lines():
        process(line)
```

### 注意差異

| 行為 | requests | httpx |
|------|----------|-------|
| 自動跟隨 redirect | 預設開啟 | 需要 `follow_redirects=True` |
| Response encoding | 自動猜 | 嚴格按 Content-Type |
| Connection close | GC 清理 | 建議顯式 `.close()` |
| Timeout 預設值 | 無（永遠等） | 5 秒 |

---

## 什麼時候該用哪個？

### 用 requests

- 寫 CLI 腳本、一次性工具
- 團隊對 requests 很熟，沒有 async 需求
- 依賴的第三方 SDK 是基於 requests 的

### 用 httpx

- 需要 async（web server、高並發）
- 需要細粒度 timeout 控制
- 需要 HTTP/2
- 需要可替換的 transport（測試、效能優化）
- 新專案（httpx 是 requests 的超集）

### 用 httpx + aiohttp transport（LiteLLM 的做法）

- 極端效能需求（千級並發）
- 需要 httpx 的好用 API，但要 aiohttp 的吞吐量
- 願意承擔多一層抽象的複雜度

---

## 設計啟示

LiteLLM 的 HTTP 層設計給我們的啟發：

1. **介面和實作分離**：httpx 是介面，aiohttp 是引擎，可以獨立替換
2. **漸進遷移**：不需要一次全改，核心路徑先遷 httpx，邊緣功能慢慢來
3. **資源生命週期管理**：Client 要快取 + TTL + 程式結束清理
4. **安全第一**：錯誤訊息遮罩 API key、SSL context 正確配置
5. **可觀測性**：連線錯誤自動重建 + 日誌，不靜默失敗

---

## 快速複習

| 重點 | 一句話 |
|------|--------|
| 為什麼選 httpx | async 原生支援 + HTTP/2 + 可替換 Transport + 更細粒度的 timeout |
| requests 的問題 | 沒有 async、timeout 只有一個數字、不支援 HTTP/2 |
| LiteLLM 的做法 | httpx API 做介面 + aiohttp transport 做引擎（兼顧好用和高吞吐） |
| 遷移要點 | `Session` → `Client`、`resp.json()` 一樣、timeout 改為 `Timeout(connect=5, read=30)` |
| 設計啟示 | 介面和實作分離，可獨立替換引擎而不改上層程式碼 |

---

[← 上一章：Cooldown Cache 設計解析](./16-Cooldown-Cache-設計解析.md) | [下一章：HTTP/2 與連線池設計 →](./18-HTTP2-與連線池設計.md)

*最後更新：2026-06-22*
