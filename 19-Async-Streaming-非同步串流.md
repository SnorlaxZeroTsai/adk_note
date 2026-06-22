# 第十九章：Async Streaming 非同步串流

> **本章目標**：掌握 SSE 協議、async generator 的串流模式、以及生產級串流的背壓/fan-out/retry 處理。
>
> **預計閱讀時間**：45 分鐘
>
> **前置閱讀**：[第七章：串流處理機制](./07-串流處理機制.md)、[第十八章：HTTP/2 與連線池設計](./18-HTTP2-與連線池設計.md)
>
> **你會學到**：SSE 協議格式、async generator 組合技巧、Proxy 串流轉發、背壓控制、LiteLLM CustomStreamWrapper 設計

> LLM 回應可能需要 10-30 秒才生成完畢。不等全部生成完再回傳，而是**邊生成邊傳**——這就是 streaming。加上 async，就能用一個 event loop 同時服務上千個串流連線。

---

## 為什麼需要 Streaming？

### 沒有 Streaming

```
Client                          LLM Server
  │─── POST /chat/completions ──→│
  │                                │  生成 token 1...
  │          （等待 15 秒）         │  生成 token 2...
  │                                │  ...
  │                                │  生成 token 500
  │←── 200 OK (完整回應) ─────────│
  │                                │
  使用者盯著空白畫面 15 秒
```

### 有 Streaming

```
Client                          LLM Server
  │─── POST /chat/completions ──→│
  │←── token 1 ──────────────────│  50ms
  │←── token 2 ──────────────────│  100ms
  │←── token 3 ──────────────────│  150ms
  │    ...                         │
  │←── token 500 + [DONE] ───────│  15s
  │                                │
  使用者看到文字逐字出現，體感快很多
```

**Streaming 不會讓總時間變短，但讓使用者「感覺」更快**（首字延遲從 15s 降到 50ms）。

---

## Part 1：SSE（Server-Sent Events）協議

LLM API 的串流回應幾乎都用 SSE 格式。

### SSE 是什麼？

SSE 是 HTTP 的一種回應格式——伺服器持續推送事件，client 持續接收：

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive

data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hello"}}]}

data: {"id":"chatcmpl-1","choices":[{"delta":{"content":" world"}}]}

data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"!"}}]}

data: [DONE]
```

### SSE 格式規則

| 欄位 | 說明 | 範例 |
|------|------|------|
| `data:` | 事件資料（最常用） | `data: {"content": "hi"}` |
| `event:` | 事件類型（可選） | `event: message` |
| `id:` | 事件 ID（用於斷線重連） | `id: 42` |
| `retry:` | 重連間隔毫秒 | `retry: 3000` |
| 空行 | 事件結束標記 | （`\n\n`） |

每個事件以**兩個換行符**（`\n\n`）分隔。

### 為什麼選 SSE 而非 WebSocket？

| 特性 | SSE | WebSocket |
|------|-----|-----------|
| 方向 | 伺服器 → 客戶端（單向） | 雙向 |
| 協議 | 普通 HTTP | 升級為 ws:// |
| 代理/CDN 支援 | ✅ 完整 | 部分（需要特殊設定） |
| 自動重連 | ✅ 瀏覽器內建 | 需要自己做 |
| 複雜度 | 低（就是 HTTP 回應） | 高（狀態管理、心跳） |

LLM 串流是典型的**伺服器單向推送**場景——用 SSE 最合適。

---

## Part 2：Python Async 基礎回顧

### Generator vs Async Generator

**同步 Generator：**
```python
def count_sync():
    for i in range(3):
        yield i

for x in count_sync():
    print(x)  # 0, 1, 2
```

**Async Generator：**
```python
async def count_async():
    for i in range(3):
        await asyncio.sleep(0.1)  # 可以 await！
        yield i

async for x in count_async():
    print(x)  # 0, 1, 2（每個間隔 100ms）
```

關鍵差異：async generator 裡面可以 `await`，**讓出控制權**給其他 coroutine。

### `async for` 的本質

```python
async for chunk in stream:
    process(chunk)

# 等同於：
iterator = stream.__aiter__()
while True:
    try:
        chunk = await iterator.__anext__()
        process(chunk)
    except StopAsyncIteration:
        break
```

每次 `__anext__()` 都是一個 `await` 點——等待下一個 chunk 到來時，event loop 可以去處理其他事。

### 為什麼 async streaming 能服務大量連線？

```
Event Loop（單線程）：

時刻 1：Client A 的 chunk 到了 → 處理 → yield
時刻 2：Client B 的 chunk 到了 → 處理 → yield
時刻 3：等待中（沒有 chunk）→ 處理其他 IO
時刻 4：Client A 的下一個 chunk 到了 → 處理 → yield
...

1000 個 streaming 連線 = 1000 個 coroutine，共享一個線程
```

如果用同步：1000 個連線 = 1000 個線程 = 大量記憶體 + context switch。

---

## Part 3：從零實作 Async Streaming

### 第一步：讀取 SSE 原始串流

```python
import httpx

async def raw_sse_stream(url: str, payload: dict):
    """最基礎的 SSE 讀取"""
    async with httpx.AsyncClient() as client:
        async with client.stream("POST", url, json=payload) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = line[6:]  # 去掉 "data: " 前綴
                    if data == "[DONE]":
                        return
                    yield data
```

### 第二步：解析成結構化物件

```python
import json
from dataclasses import dataclass
from typing import Optional

@dataclass
class StreamDelta:
    content: Optional[str] = None
    role: Optional[str] = None
    finish_reason: Optional[str] = None

async def parse_stream(url: str, payload: dict):
    """解析 SSE 為結構化 delta"""
    async for raw in raw_sse_stream(url, payload):
        chunk = json.loads(raw)
        choice = chunk["choices"][0]
        delta = choice.get("delta", {})
        yield StreamDelta(
            content=delta.get("content"),
            role=delta.get("role"),
            finish_reason=choice.get("finish_reason"),
        )
```

### 第三步：組裝完整回應（accumulator 模式）

```python
async def stream_with_accumulate(url: str, payload: dict):
    """邊串流邊累積完整回應"""
    full_content = []

    async for delta in parse_stream(url, payload):
        if delta.content:
            full_content.append(delta.content)
            # 即時輸出給使用者
            print(delta.content, end="", flush=True)

    # 串流結束，回傳完整文字
    return "".join(full_content)
```

### 第四步：加入錯誤處理和超時

```python
async def robust_stream(url: str, payload: dict, timeout: float = 60):
    """生產級串流：超時、錯誤處理、資源清理"""
    client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5))

    try:
        async with client.stream("POST", url, json=payload) as response:
            if response.status_code != 200:
                body = await response.aread()
                raise StreamError(f"HTTP {response.status_code}: {body}")

            last_chunk_time = asyncio.get_event_loop().time()

            async for line in response.aiter_lines():
                # 檢查 chunk 間隔是否太久（心跳超時）
                now = asyncio.get_event_loop().time()
                if now - last_chunk_time > 30:
                    raise StreamTimeout("超過 30 秒沒收到新 chunk")
                last_chunk_time = now

                if not line or not line.startswith("data: "):
                    continue

                data = line[6:]
                if data == "[DONE]":
                    return

                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue  # 跳過格式錯誤的 chunk

    except httpx.ReadTimeout:
        raise StreamTimeout("讀取超時")
    except httpx.RemoteProtocolError:
        raise StreamError("連線被伺服器中斷")
    finally:
        await client.aclose()
```

---

## Part 4：轉發串流（Proxy 模式）

LiteLLM Gateway 的核心場景：從 LLM Provider 收到串流，轉發給客戶端。

### 基本轉發

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()

@app.post("/v1/chat/completions")
async def proxy_completion(request: Request):
    payload = await request.json()

    async def generate():
        async for chunk in robust_stream(upstream_url, payload):
            # 重新包裝成 SSE 格式轉發
            yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
```

### 邊轉發邊處理（tap 模式）

```python
@app.post("/v1/chat/completions")
async def proxy_with_logging(request: Request):
    payload = await request.json()
    collected_tokens = []
    start_time = time.time()

    async def generate():
        async for chunk in robust_stream(upstream_url, payload):
            # 1. 收集 token（用於計費、日誌）
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            if content := delta.get("content"):
                collected_tokens.append(content)

            # 2. 轉發給客戶端
            yield f"data: {json.dumps(chunk)}\n\n"

        yield "data: [DONE]\n\n"

        # 3. 串流結束後處理（非同步，不阻塞客戶端）
        asyncio.create_task(log_usage(
            tokens="".join(collected_tokens),
            duration=time.time() - start_time,
        ))

    return StreamingResponse(generate(), media_type="text/event-stream")
```

---

## Part 5：Async Generator 進階模式

### 5.1 扇出（Fan-out）：一個串流供多個消費者

```python
class StreamBroadcaster:
    """一個上游串流，廣播給多個下游消費者"""

    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue()
        self._subscribers.append(q)
        return q

    async def broadcast(self, upstream):
        """從上游讀取，推給所有訂閱者"""
        try:
            async for chunk in upstream:
                for q in self._subscribers:
                    await q.put(chunk)
        finally:
            # 串流結束，發送結束信號
            for q in self._subscribers:
                await q.put(None)

    async def consume(self, queue: asyncio.Queue):
        """訂閱者的消費介面"""
        while True:
            chunk = await queue.get()
            if chunk is None:
                return
            yield chunk
```

用途：同一個 LLM 回應同時寫入 cache + 轉發客戶端 + 記錄日誌。

### 5.2 合併（Merge）：多個串流合成一個

```python
async def merge_streams(*streams):
    """多個 async generator 合併為一個，誰先來先 yield"""
    queue = asyncio.Queue()
    active = len(streams)

    async def feed(stream, index):
        nonlocal active
        try:
            async for item in stream:
                await queue.put((index, item))
        finally:
            active -= 1
            if active == 0:
                await queue.put(None)  # 全部結束

    # 啟動所有 feeder
    tasks = [asyncio.create_task(feed(s, i)) for i, s in enumerate(streams)]

    # 消費合併後的結果
    while True:
        item = await queue.get()
        if item is None:
            break
        yield item

    # 確保清理
    for task in tasks:
        task.cancel()
```

用途：同時向多個 Provider 串流，取最快的結果（racing）。

### 5.3 背壓控制（Backpressure）

如果消費者比生產者慢怎麼辦？

```python
async def buffered_stream(upstream, max_buffer=100):
    """帶背壓的串流：buffer 滿了就暫停讀取上游"""
    buffer = asyncio.Queue(maxsize=max_buffer)

    async def producer():
        async for chunk in upstream:
            await buffer.put(chunk)  # buffer 滿了會阻塞，自動背壓
        await buffer.put(None)

    task = asyncio.create_task(producer())

    try:
        while True:
            chunk = await buffer.get()
            if chunk is None:
                return
            yield chunk
    finally:
        task.cancel()
```

### 5.4 超時控制（每個 chunk 之間）

```python
async def stream_with_chunk_timeout(upstream, chunk_timeout=30):
    """如果兩個 chunk 之間超過 N 秒，認為串流卡住"""
    async for chunk in upstream:
        try:
            # 等待下一個 chunk，最多等 chunk_timeout 秒
            yield chunk
        except asyncio.TimeoutError:
            raise StreamStalled(f"串流停滯超過 {chunk_timeout} 秒")

# 更精確的寫法：
async def timeout_aiter(aiter, timeout):
    """給任何 async iterator 加上 per-item timeout"""
    aiter = aiter.__aiter__()
    while True:
        try:
            item = await asyncio.wait_for(aiter.__anext__(), timeout=timeout)
            yield item
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise StreamStalled(f"等待下一個 item 超過 {timeout} 秒")
```

### 5.5 重試（斷線續傳）

```python
async def resumable_stream(create_stream, max_retries=3):
    """串流中斷時自動重連並續傳"""
    collected = []
    retries = 0

    while retries <= max_retries:
        try:
            # 建立串流（如果有已收集的內容，帶上 offset）
            stream = create_stream(offset=len(collected))
            async for chunk in stream:
                collected.append(chunk)
                yield chunk
            return  # 正常結束

        except (StreamError, httpx.RemoteProtocolError):
            retries += 1
            if retries > max_retries:
                raise
            await asyncio.sleep(2 ** retries)  # 指數退避
```

---

## Part 6：LiteLLM 的串流架構

### 整體流程

```
Client Request (stream=True)
    │
    ▼
Router.acompletion()
    │
    ▼
AsyncHTTPHandler.stream("POST", provider_url)
    │
    ▼
httpx.AsyncClient.stream() → aiohttp transport
    │
    ▼
response.aiter_lines()  ──→  SSE 原始行
    │
    ▼
provider_config.get_model_response_iterator()
    │                    ──→  解析為 ModelResponse chunks
    ▼
CustomStreamWrapper  ──→  統一格式 + 回呼
    │
    ▼
StreamingResponse  ──→  轉發給客戶端
```

### CustomStreamWrapper 的角色

```python
class CustomStreamWrapper:
    """統一所有 Provider 的串流格式"""

    def __init__(self, completion_stream, model):
        self.completion_stream = completion_stream
        self.model = model
        self.response_uptil_now = ""    # 累積文字
        self.chunks = []                # 所有 chunk

    async def __anext__(self):
        # 1. 從底層取得 raw chunk
        chunk = await self.completion_stream.__anext__()

        # 2. 轉成統一的 ModelResponseStream 格式
        response = self.chunk_creator(chunk)

        # 3. 累積（用於回呼、計費）
        if content := response.choices[0].delta.content:
            self.response_uptil_now += content

        # 4. 觸發 streaming callback（日誌、metrics）
        await self.logging_obj.async_success_handler(response)

        return response
```

### 為什麼需要 CustomStreamWrapper？

不同 Provider 的串流格式不同：

```python
# OpenAI 格式
{"choices": [{"delta": {"content": "Hello"}}]}

# Anthropic 格式
{"type": "content_block_delta", "delta": {"text": "Hello"}}

# Google 格式
{"candidates": [{"content": {"parts": [{"text": "Hello"}]}}]}
```

CustomStreamWrapper 把它們全部轉成統一的 OpenAI 格式輸出。

---

## Part 7：FastAPI / Starlette 的 StreamingResponse 原理

### StreamingResponse 怎麼運作？

```python
class StreamingResponse:
    def __init__(self, content, media_type=None):
        self.body_iterator = content  # async generator

    async def stream_response(self, send):
        # 1. 先發 HTTP headers
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [...],
        })

        # 2. 逐 chunk 發送 body
        async for chunk in self.body_iterator:
            await send({
                "type": "http.response.body",
                "body": chunk.encode(),
                "more_body": True,       # 還有更多
            })

        # 3. 結束
        await send({
            "type": "http.response.body",
            "body": b"",
            "more_body": False,          # 沒了
        })
```

### ASGI 協議層

```
FastAPI → Starlette → ASGI Server (uvicorn)
                          │
                          ▼
                    asyncio event loop
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
         Connection 1  Connection 2  Connection 3
         (streaming)   (streaming)   (normal)
```

每個 streaming response 是一個 coroutine，在 event loop 中與其他連線**協作式多工**。

---

## Part 8：常見問題與陷阱

### 陷阱 1：忘記處理客戶端斷線

客戶端可能中途關閉連線（使用者關閉瀏覽器）。如果不處理，上游串流會繼續消耗資源。

```python
async def safe_stream(upstream):
    """偵測客戶端斷線並清理上游"""
    try:
        async for chunk in upstream:
            yield chunk
    except asyncio.CancelledError:
        # FastAPI/Starlette 在客戶端斷線時會 cancel 這個 task
        # 確保上游資源被清理
        if hasattr(upstream, 'aclose'):
            await upstream.aclose()
        raise
```

### 陷阱 2：Async Generator 沒有正確關閉

```python
# ❌ 不好：如果中途 break，generator 不會執行 finally
async for chunk in stream:
    if should_stop:
        break  # generator 的 finally 不一定跑！

# ✅ 好：顯式關閉
stream = my_async_generator()
try:
    async for chunk in stream:
        if should_stop:
            break
finally:
    await stream.aclose()  # 確保 finally 塊執行
```

### 陷阱 3：在 async generator 中混用同步 IO

```python
# ❌ 阻塞整個 event loop
async def bad_stream():
    async for chunk in upstream:
        # 這會阻塞！其他所有 coroutine 都在等
        result = requests.post(logging_url, json=chunk)
        yield chunk

# ✅ 用 async IO 或丟到線程池
async def good_stream():
    async for chunk in upstream:
        # 非阻塞
        asyncio.create_task(async_log(chunk))
        yield chunk
```

### 陷阱 4：記憶體洩漏——無限累積

```python
# ❌ 如果串流很長，collected 會一直長大
async def leaky_stream():
    collected = []
    async for chunk in upstream:
        collected.append(chunk)  # 永遠不釋放
        yield chunk
    # collected 在這裡才能被 GC

# ✅ 如果不需要完整回應，就不要累積
# 如果需要，設上限或用 callback 即時處理
async def bounded_stream(max_chunks=10000):
    count = 0
    async for chunk in upstream:
        count += 1
        if count > max_chunks:
            raise StreamTooLong("超過最大 chunk 數")
        yield chunk
```

### 陷阱 5：沒有超時的串流永遠掛著

```python
# ❌ 如果伺服器停止發送但不關連線，這裡永遠等
async for line in response.aiter_lines():
    yield line

# ✅ 每個 chunk 有超時
async for line in timeout_aiter(response.aiter_lines(), timeout=30):
    yield line
```

---

## Part 9：效能考量

### Chunk 大小 vs 回呼頻率

| chunk 策略 | 延遲 | CPU 使用 | 網路效率 |
|-----------|------|---------|---------|
| 每個 token 一個 chunk | 最低延遲 | 高（大量小封包） | 低（header 開銷大） |
| 攢幾個 token 一批 | 稍高 | 中 | 中 |
| 固定時間間隔 flush | 可控 | 低 | 高 |

LLM API 通常是每個 token 一個 SSE event——因為使用者體驗優先。

### 併發串流數 vs 記憶體

每個活躍串流佔用：
- httpx response buffer：~4KB
- Python async generator frame：~1KB
- 累積文字（如果有的話）：看回應長度

1000 個並行串流 ≈ 5-10MB 記憶體（不計入累積文字），很輕量。

### 事件循環壓力

```python
# 用 uvloop 替代標準 asyncio event loop（快 2-4 倍）
import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
```

---

## Part 10：完整範例——生產級串流 Proxy

```python
import asyncio
import json
import time
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

# 全域 client，複用連線池
client = httpx.AsyncClient(
    timeout=httpx.Timeout(600, connect=5),
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
)


async def upstream_stream(
    url: str,
    headers: dict,
    payload: dict,
    chunk_timeout: float = 30,
) -> AsyncGenerator[dict, None]:
    """從上游 LLM Provider 讀取串流"""

    async with client.stream("POST", url, headers=headers, json=payload) as resp:
        if resp.status_code != 200:
            body = await resp.aread()
            raise httpx.HTTPStatusError(
                f"Upstream error", request=resp.request, response=resp
            )

        line_iter = resp.aiter_lines()
        while True:
            try:
                line = await asyncio.wait_for(
                    line_iter.__anext__(), timeout=chunk_timeout
                )
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                raise TimeoutError(f"上游 {chunk_timeout}s 無回應")

            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                return
            yield json.loads(data)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    payload = await request.json()
    start = time.time()
    token_count = 0

    async def generate():
        nonlocal token_count
        try:
            async for chunk in upstream_stream(
                url="https://api.openai.com/v1/chat/completions",
                headers={"Authorization": request.headers.get("Authorization")},
                payload={**payload, "stream": True},
            ):
                token_count += 1
                yield f"data: {json.dumps(chunk)}\n\n"

            yield "data: [DONE]\n\n"

        except asyncio.CancelledError:
            # 客戶端斷線
            pass
        except Exception as e:
            # 串流中途錯誤，發一個 error event
            error_chunk = {"error": {"message": str(e), "type": "stream_error"}}
            yield f"data: {json.dumps(error_chunk)}\n\n"
        finally:
            # 不管正常還是異常，都記錄用量
            duration = time.time() - start
            asyncio.create_task(record_usage(token_count, duration))

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"X-Stream-Start": str(start)},
    )


async def record_usage(tokens: int, duration: float):
    """非同步記錄用量（不阻塞回應）"""
    print(f"Stream complete: {tokens} chunks in {duration:.2f}s")
```

---

## 概念總覽圖

```
┌─────────────────────────────────────────────────────────────┐
│                     Async Streaming 全景                      │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  協議層     SSE (text/event-stream)                          │
│             data: {...}\n\n                                   │
│                                                              │
│  傳輸層     httpx.AsyncClient.stream()                       │
│             → response.aiter_lines()                         │
│                                                              │
│  解析層     SSE parser → JSON → ModelResponse                │
│             （Provider 特定格式轉統一格式）                     │
│                                                              │
│  控制層     timeout / backpressure / cancel / retry          │
│                                                              │
│  消費層     StreamingResponse / WebSocket / 寫檔 / 日誌       │
│                                                              │
└─────────────────────────────────────────────────────────────┘

Event Loop 中的協作：

  ┌──────┐  await   ┌──────┐  await   ┌──────┐
  │Stream│ ──────── │Stream│ ──────── │Stream│
  │  A   │          │  B   │          │  C   │
  └──┬───┘          └──┬───┘          └──┬───┘
     │                  │                  │
     └──────────────────┴──────────────────┘
                        │
                   Event Loop
              （一個線程服務所有串流）
```

---

## 快速複習

| 重點 | 一句話 |
|------|--------|
| SSE 格式 | `data: {JSON}\n\n`，結束時送 `data: [DONE]\n\n` |
| async generator | `async def stream(): yield chunk`，配合 `async for` 消費 |
| Proxy 串流轉發 | 上游 `aiter_lines()` → parse → 包成 `StreamingResponse` → 下游 |
| 背壓 | `asyncio.Queue(maxsize=N)` + `await queue.put()` 自然阻塞快生產者 |
| Fan-out/Merge | 一個 source 推給 N 個 consumer / N 個 source 合併成一個 stream |
| LiteLLM 做法 | `CustomStreamWrapper` 統一各 Provider 的串流格式差異 |

---

[← 上一章：HTTP/2 與連線池設計](./18-HTTP2-與連線池設計.md) | [下一章：User Budget 查詢與管理 →](./20-LiteLLM-User-Budget-查詢與管理.md)

*最後更新：2026-06-22*
