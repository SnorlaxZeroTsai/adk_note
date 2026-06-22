# 第十八章：HTTP/2 與連線池設計

> **本章目標**：理解 HTTP/2 多路複用的底層原理，能從零設計一個支援 TTL、健康檢查、背壓的連線池。
>
> **預計閱讀時間**：40 分鐘
>
> **前置閱讀**：[第十七章：httpx vs requests 深度比較](./17-httpx-vs-requests-深度比較.md)
>
> **你會學到**：HTTP/2 frame/stream 結構、連線池三個版本的演進（基礎→TTL→生產級）、HTTP/2 stream 追蹤

> 為什麼一條 TCP 連線就能同時處理上百個請求？連線池又是怎麼管理這些連線的？從 HTTP/1.1 的痛點開始，一路講到自己設計一個生產級連線池。

---

## Part 1：HTTP/2 是什麼？

### HTTP/1.1 的根本問題：隊頭阻塞（Head-of-Line Blocking）

```
HTTP/1.1 一條連線同一時間只能處理一個請求：

Client                          Server
  │─── GET /api/models ──────────→│
  │                                │ （等待回應...）
  │←── 200 OK ────────────────────│
  │─── POST /chat/completions ───→│  ← 必須等上一個完成才能發
  │                                │
  │←── 200 OK ────────────────────│
```

想要並行？只能開**多條 TCP 連線**（瀏覽器通常限制 6 條/domain）。

### HTTP/2 的解法：多路複用（Multiplexing）

```
HTTP/2 一條連線同時跑多個請求（Stream）：

Client                          Server
  │═══ Stream 1: GET /models ════→│
  │═══ Stream 3: POST /chat ═════→│  ← 同時發，不用等
  │═══ Stream 5: POST /embed ════→│
  │                                │
  │←══ Stream 1: 200 OK ═════════│  ← 誰先好誰先回
  │←══ Stream 5: 200 OK ═════════│
  │←══ Stream 3: 200 OK ═════════│
```

**一條 TCP 連線 = 多個邏輯 Stream**，互不阻塞。

### HTTP/2 核心概念

| 概念 | 說明 |
|------|------|
| Frame | 最小通訊單位（類似 TCP segment） |
| Stream | 一組雙向 Frame，對應一個 request-response |
| Connection | 一條 TCP 連線，承載多個 Stream |
| HPACK | Header 壓縮演算法（HTTP/1.1 header 不壓縮，每次重傳很浪費） |
| Server Push | 伺服器主動推送資源（實務中很少用） |

### Frame 結構

```
+-----------------------------------------------+
|                 Length (24 bit)                |
+---------------+---------------+---------------+
|   Type (8)    |   Flags (8)   |
+-+-------------+---------------+---------------+
|R|                Stream ID (31 bit)           |
+-+---------------------------------------------+
|                 Frame Payload ...              |
+-----------------------------------------------+
```

Type 常見值：
- `DATA (0x0)`：request/response body
- `HEADERS (0x1)`：HTTP headers
- `SETTINGS (0x4)`：連線參數協商
- `WINDOW_UPDATE (0x8)`：流量控制
- `GOAWAY (0x7)`：優雅關閉連線

### 多路複用實際傳輸

```
一條 TCP 連線上的 Frame 交錯排列：

[HEADERS Stream 1][HEADERS Stream 3][DATA Stream 1 chunk1]
[DATA Stream 3 chunk1][DATA Stream 1 chunk2][DATA Stream 3 chunk2]
[DATA Stream 1 END][DATA Stream 3 END]
```

Stream 1 和 Stream 3 的資料**交錯傳輸**，接收端根據 Stream ID 重組。

### HTTP/2 vs HTTP/1.1 效能差異

| 場景 | HTTP/1.1 | HTTP/2 |
|------|----------|--------|
| 10 個並行請求 | 需要 10 條 TCP 連線 | 1 條就夠 |
| TCP 握手開銷 | 10 次 × (1 RTT + TLS 2 RTT) | 1 次 |
| 連線池大小 | 大（每個並行請求一條） | 小（1-2 條/host 就夠） |
| Header 大小 | 每次完整傳送（~500B-2KB） | HPACK 壓縮後 ~20-50B |
| 隊頭阻塞 | 應用層阻塞 | 應用層不阻塞（但 TCP 層仍有） |

### HTTP/2 的限制

1. **TCP 層隊頭阻塞**：一個封包丟失 → 整條連線所有 Stream 等待重傳
2. **單連線瓶頸**：所有 Stream 共享一條 TCP 連線的頻寬
3. **Server Push 已被淘汰**：Chrome 2022 年移除支援

這些問題是 HTTP/3（QUIC，基於 UDP）要解決的。

---

## Part 2：為什麼需要連線池？

### 沒有連線池的世界

```python
# 每次請求都建新連線
for prompt in prompts:
    response = httpx.post("https://api.openai.com/v1/chat/completions",
                          json={"messages": [{"role": "user", "content": prompt}]})
    # TCP 握手 → TLS 握手 → 發請求 → 收回應 → 關連線
    # 光握手就要 1-3 個 RTT（~50-150ms）
```

1000 個請求 = 1000 次握手 = 浪費 50-150 秒在建連線上。

### 有連線池的世界

```python
# 建好連線池，重複使用
client = httpx.Client()  # 內建連線池
for prompt in prompts:
    response = client.post(url, json=payload)
    # 複用已有連線 → 直接發請求 → 省掉握手
```

連線池的本質：**建好的 TCP 連線不要關，下次再用**。

### 連線池要解決的問題

| 問題 | 說明 |
|------|------|
| 連線建立成本 | TCP + TLS 握手慢 |
| 連線數上限 | 作業系統 fd 有限、伺服器也有限制 |
| 連線失效 | idle 太久被對方關掉、網路斷線 |
| 併發控制 | 太多請求同時搶連線 |
| 公平分配 | 多個 host 之間如何分配有限的連線 |
| 記憶體管理 | 連線佔用 buffer，不能無限持有 |

---

## Part 3：從零設計連線池

### 第一版：最簡單的池

```python
import queue
import socket
import ssl

class SimpleConnectionPool:
    def __init__(self, host, port, max_size=10):
        self.host = host
        self.port = port
        self.max_size = max_size
        self._pool = queue.Queue(maxsize=max_size)
        self._created = 0

    def _create_connection(self):
        """建立新的 TCP + TLS 連線"""
        sock = socket.create_connection((self.host, self.port), timeout=5)
        context = ssl.create_default_context()
        return context.wrap_socket(sock, server_hostname=self.host)

    def acquire(self):
        """從池中取一條連線"""
        try:
            conn = self._pool.get_nowait()
            if self._is_alive(conn):
                return conn
            # 連線壞了，丟掉重建
            conn.close()
            self._created -= 1
        except queue.Empty:
            pass

        if self._created < self.max_size:
            self._created += 1
            return self._create_connection()

        # 池滿了，阻塞等待
        return self._pool.get(timeout=30)

    def release(self, conn):
        """用完放回池裡"""
        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            conn.close()
            self._created -= 1

    def _is_alive(self, conn):
        """檢查連線是否還活著"""
        try:
            conn.settimeout(0)
            data = conn.recv(1, socket.MSG_PEEK)
            return True  # 有資料可讀（不太正常，但連線活著）
        except BlockingIOError:
            return True   # 沒資料但連線正常
        except Exception:
            return False  # 連線已斷
        finally:
            conn.settimeout(None)
```

問題：沒有 TTL、沒有 per-host 管理、沒有 async 支援。

### 第二版：加入 TTL 和健康檢查

```python
import time
from dataclasses import dataclass
from typing import Optional

@dataclass
class PooledConnection:
    conn: ssl.SSLSocket
    created_at: float
    last_used_at: float
    host: str

class TTLConnectionPool:
    def __init__(self, max_size=10, max_idle_time=120, max_lifetime=3600):
        self.max_size = max_size
        self.max_idle_time = max_idle_time    # 閒置多久關掉
        self.max_lifetime = max_lifetime      # 最長存活時間
        self._pool: list[PooledConnection] = []
        self._in_use = 0
        self._lock = threading.Lock()

    def acquire(self, host, port) -> ssl.SSLSocket:
        with self._lock:
            # 先清理過期連線
            self._evict_expired()

            # 找同 host 的可用連線
            for i, pooled in enumerate(self._pool):
                if pooled.host == f"{host}:{port}":
                    self._pool.pop(i)
                    self._in_use += 1
                    pooled.last_used_at = time.time()
                    return pooled.conn

            # 沒有可用的，建新的
            if self._in_use + len(self._pool) < self.max_size:
                self._in_use += 1
                conn = self._create_connection(host, port)
                return conn

            # 都滿了
            raise PoolExhaustedError("連線池已滿，請稍後重試")

    def release(self, conn, host, port):
        with self._lock:
            self._in_use -= 1
            pooled = PooledConnection(
                conn=conn,
                created_at=time.time(),
                last_used_at=time.time(),
                host=f"{host}:{port}",
            )
            self._pool.append(pooled)

    def _evict_expired(self):
        """清除過期連線"""
        now = time.time()
        self._pool = [
            p for p in self._pool
            if (now - p.last_used_at < self.max_idle_time
                and now - p.created_at < self.max_lifetime
                and self._is_alive(p.conn))
        ]
```

### 第三版：Async 連線池（生產級設計）

```python
import asyncio
from collections import defaultdict

@dataclass
class AsyncPooledConnection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    created_at: float
    last_used_at: float
    in_use: bool = False

class AsyncConnectionPool:
    def __init__(
        self,
        max_connections: int = 100,
        max_connections_per_host: int = 10,
        max_idle_time: float = 120,
        max_lifetime: float = 3600,
        connect_timeout: float = 5,
    ):
        self.max_connections = max_connections
        self.max_per_host = max_connections_per_host
        self.max_idle_time = max_idle_time
        self.max_lifetime = max_lifetime
        self.connect_timeout = connect_timeout

        # per-host 管理
        self._pools: dict[str, list[AsyncPooledConnection]] = defaultdict(list)
        self._total_count = 0
        self._lock = asyncio.Lock()

        # 等待佇列：當池滿時，請求在這裡排隊
        self._waiters: dict[str, asyncio.Queue] = defaultdict(
            lambda: asyncio.Queue()
        )

    async def acquire(self, host: str, port: int) -> AsyncPooledConnection:
        key = f"{host}:{port}"

        async with self._lock:
            # 1. 找現有閒置連線
            conn = self._get_idle(key)
            if conn:
                conn.in_use = True
                conn.last_used_at = asyncio.get_event_loop().time()
                return conn

            # 2. 可以建新連線嗎？
            if self._can_create(key):
                conn = await self._create(host, port)
                self._pools[key].append(conn)
                self._total_count += 1
                return conn

        # 3. 池滿了，等待釋放（帶 timeout）
        try:
            conn = await asyncio.wait_for(
                self._waiters[key].get(),
                timeout=30,
            )
            conn.in_use = True
            return conn
        except asyncio.TimeoutError:
            raise PoolExhaustedError(f"等待連線超時: {key}")

    async def release(self, conn: AsyncPooledConnection, host: str, port: int):
        key = f"{host}:{port}"
        conn.in_use = False
        conn.last_used_at = asyncio.get_event_loop().time()

        # 如果有人在等，直接給他
        if not self._waiters[key].empty():
            # 不太對——Queue 是放東西讓別人取
            pass

        # 正確做法：通知等待者
        try:
            self._waiters[key].put_nowait(conn)
        except asyncio.QueueFull:
            # 沒人在等，放回池裡（已經在池裡了）
            pass

    def _get_idle(self, key: str) -> Optional[AsyncPooledConnection]:
        """找一條閒置且健康的連線"""
        now = asyncio.get_event_loop().time()
        for conn in self._pools[key]:
            if conn.in_use:
                continue
            if now - conn.last_used_at > self.max_idle_time:
                continue  # 閒置太久，跳過（稍後清理）
            if now - conn.created_at > self.max_lifetime:
                continue  # 太老了
            return conn
        return None

    def _can_create(self, key: str) -> bool:
        """檢查是否可以建新連線"""
        host_count = len(self._pools[key])
        return (self._total_count < self.max_connections
                and host_count < self.max_per_host)

    async def _create(self, host: str, port: int) -> AsyncPooledConnection:
        """建立新連線"""
        ssl_context = ssl.create_default_context()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_context),
            timeout=self.connect_timeout,
        )
        now = asyncio.get_event_loop().time()
        return AsyncPooledConnection(
            reader=reader,
            writer=writer,
            created_at=now,
            last_used_at=now,
            in_use=True,
        )

    async def _cleanup_loop(self):
        """背景任務：定期清理過期連線"""
        while True:
            await asyncio.sleep(30)
            async with self._lock:
                now = asyncio.get_event_loop().time()
                for key, conns in self._pools.items():
                    to_remove = []
                    for conn in conns:
                        if conn.in_use:
                            continue
                        expired = (
                            now - conn.last_used_at > self.max_idle_time
                            or now - conn.created_at > self.max_lifetime
                        )
                        if expired:
                            conn.writer.close()
                            to_remove.append(conn)
                    for conn in to_remove:
                        conns.remove(conn)
                        self._total_count -= 1
```

---

## Part 4：生產級連線池的設計考量

### 4.1 連線狀態機

```
         建立中
          │
     ┌────▼────┐
     │  IDLE   │←──────────┐
     └────┬────┘           │
          │ acquire()      │ release()
     ┌────▼────┐           │
     │ IN_USE  │───────────┘
     └────┬────┘
          │ 連線錯誤 / TTL 到期
     ┌────▼────┐
     │ CLOSED  │
     └─────────┘
```

### 4.2 核心參數

| 參數 | 典型值 | 說明 |
|------|--------|------|
| `max_connections` | 100 | 所有 host 合計上限 |
| `max_per_host` | 10-20 | 單一 host 上限 |
| `max_idle_time` | 60-120s | 閒置多久回收 |
| `max_lifetime` | 3600s | 連線最長壽命（防止長連線累積問題） |
| `connect_timeout` | 5s | TCP + TLS 握手超時 |
| `pool_timeout` | 30s | 等待空閒連線的超時 |
| `keepalive_interval` | 30s | TCP keepalive 探測間隔 |

### 4.3 為什麼需要 max_lifetime？

即使連線看起來正常，長時間存在的連線可能：
- 經過的 NAT 設備靜默丟棄（中間路由重啟）
- 累積記憶體洩漏（buffer 碎片化）
- SSL session 過期需要重新協商
- 後端 load balancer 重新分配不均（所有連線都黏在一台機器）

定期重建 = 強制重新平衡 + 避免隱藏問題。

### 4.4 預熱（Warm-up）

冷啟動時所有連線都要現建，第一批請求很慢：

```python
async def warmup(self, host: str, port: int, count: int = 5):
    """啟動時預建連線"""
    tasks = [self._create(host, port) for _ in range(count)]
    conns = await asyncio.gather(*tasks, return_exceptions=True)
    for conn in conns:
        if isinstance(conn, AsyncPooledConnection):
            conn.in_use = False
            self._pools[f"{host}:{port}"].append(conn)
            self._total_count += 1
```

### 4.5 背壓（Backpressure）

當池滿時怎麼辦？幾種策略：

| 策略 | 做法 | 適用場景 |
|------|------|---------|
| 阻塞等待 | `await queue.get()` | 一般場景 |
| 快速失敗 | 立即拋異常 | 高可用系統 |
| 溢出建立 | 超過上限仍建立，用完即關 | 突發流量 |
| 排隊 + 超時 | 等 N 秒，超時失敗 | 最常見 |

### 4.6 健康檢查策略

```python
# 策略 1：用之前檢查（acquire 時）
def _is_healthy(self, conn) -> bool:
    # TCP level：用 peek 檢查
    # HTTP level：看上次回應是否正常
    ...

# 策略 2：背景定期探測
async def _health_check_loop(self):
    while True:
        await asyncio.sleep(10)
        for conn in idle_connections:
            if not await self._ping(conn):
                self._remove(conn)

# 策略 3：用失敗當訊號（最常見）
async def request(self, ...):
    conn = await self.acquire(host, port)
    try:
        return await conn.send(request)
    except ConnectionError:
        self._remove(conn)     # 壞了就丟
        conn = await self.acquire(host, port)  # 拿新的重試
        return await conn.send(request)
```

LiteLLM 用的是策略 3：

```python
except (httpx.RemoteProtocolError, httpx.ConnectError):
    new_client = self.create_client(timeout=timeout)
    return await new_client.post(url, ...)
```

### 4.7 HTTP/2 連線池的不同之處

HTTP/1.1 連線池：**一條連線 = 一個並行請求**

```
Pool (max=10):
  conn1 → [Request A]
  conn2 → [Request B]
  conn3 → [idle]
  ...
```

HTTP/2 連線池：**一條連線 = 多個並行 Stream**

```
Pool (max=2):
  conn1 → [Stream 1: Req A] [Stream 3: Req B] [Stream 5: Req C]
  conn2 → [Stream 1: Req D] [Stream 3: Req E]
```

HTTP/2 連線池需要追蹤的額外狀態：
- 每條連線的**活躍 Stream 數**
- 伺服器的 `MAX_CONCURRENT_STREAMS` 設定（通常 100-256）
- 流量控制窗口大小

```python
@dataclass
class HTTP2Connection:
    transport: Any
    active_streams: int = 0
    max_streams: int = 100  # 由伺服器 SETTINGS frame 決定

    @property
    def available_capacity(self) -> int:
        return self.max_streams - self.active_streams

    @property
    def is_full(self) -> bool:
        return self.active_streams >= self.max_streams

class HTTP2Pool:
    def acquire_stream(self, host):
        # 找一條有空餘 stream 的連線
        for conn in self._pools[host]:
            if not conn.is_full:
                conn.active_streams += 1
                return conn
        # 所有連線都滿了，建新的
        ...

    def release_stream(self, conn):
        conn.active_streams -= 1
```

---

## Part 5：真實世界的實作對比

### aiohttp 的 TCPConnector

```python
connector = aiohttp.TCPConnector(
    limit=100,                 # 總連線數
    limit_per_host=10,         # per-host 上限
    keepalive_timeout=120,     # idle 超時
    ttl_dns_cache=300,         # DNS 結果快取
    enable_cleanup_closed=True,
    force_close=False,         # 回應結束後是否強制關連線
)
session = aiohttp.ClientSession(connector=connector)
```

### httpx 的 Limits

```python
limits = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=120,
)
client = httpx.AsyncClient(limits=limits)
```

### urllib3 的 PoolManager（requests 底層）

```python
import urllib3

pool = urllib3.PoolManager(
    num_pools=10,          # 快取多少 host 的池
    maxsize=10,            # 每個 host 池的大小
    block=True,            # 滿了是否阻塞等待
    retries=3,             # 自動重試次數
)
```

### LiteLLM 的三層設計

```
httpx.AsyncClient (介面)
    └── LiteLLMAiohttpTransport (橋接)
            └── aiohttp.TCPConnector (實際連線池)
                    limit=1000
                    limit_per_host=500
                    keepalive_timeout=120
                    ttl_dns_cache=300
```

---

## Part 6：常見問題與除錯

### 連線洩漏（Connection Leak）

症狀：連線數只增不減，最終 fd 耗盡。

原因：`acquire()` 了但沒有 `release()`。

解法：Context Manager 模式
```python
class ConnectionPool:
    @asynccontextmanager
    async def connection(self, host, port):
        conn = await self.acquire(host, port)
        try:
            yield conn
        finally:
            await self.release(conn, host, port)

# 使用
async with pool.connection("api.openai.com", 443) as conn:
    response = await conn.send(request)
    # 即使拋異常，finally 也會 release
```

### 連線風暴（Connection Storm）

症狀：冷啟動或高峰時，大量連線同時建立，打爆後端。

解法：用 Semaphore 限制同時建立數
```python
class ConnectionPool:
    def __init__(self):
        self._create_semaphore = asyncio.Semaphore(5)  # 同時最多建 5 條

    async def _create(self, host, port):
        async with self._create_semaphore:
            return await asyncio.open_connection(host, port, ssl=...)
```

### DNS 快取過期

症狀：後端 IP 換了，但連線池還在用舊 IP。

解法：
1. `max_lifetime` 強制定期重建
2. DNS TTL cache（aiohttp 的 `ttl_dns_cache=300`）
3. 連線失敗時強制刷新 DNS

---

## 總結：設計檢查清單

設計連線池時，對照這張表確認都考慮到了：

| 維度 | 必須 | 進階 |
|------|------|------|
| 容量控制 | ✅ max_connections | per-host 限制 |
| 複用 | ✅ idle 連線重用 | HTTP/2 stream 複用 |
| 淘汰 | ✅ idle timeout | max lifetime |
| 健康檢查 | ✅ 用失敗當訊號 | 背景探測 |
| 併發安全 | ✅ Lock / Semaphore | 無鎖設計 |
| 背壓 | ✅ 等待 + 超時 | 溢出策略 |
| 生命週期 | ✅ close / cleanup | atexit 清理 |
| 可觀測 | — | metrics（活躍/閒置/等待數） |
| 預熱 | — | 啟動時預建連線 |
| DNS | — | TTL cache + 故障刷新 |

---

## 快速複習

| 重點 | 一句話 |
|------|--------|
| HTTP/2 核心 | 一條 TCP 連線上多個 stream 並行（多路複用），解決 HTTP/1.1 隊頭阻塞 |
| Frame 結構 | 9 byte header + payload，每個 frame 帶 stream ID |
| 連線池 V1 | Queue + Semaphore，基礎容量控制 |
| 連線池 V2 | + idle timeout + max lifetime + 健康檢查 |
| 連線池 V3（生產級） | + per-host 限制 + 背壓 + warmup + HTTP/2 stream 追蹤 |
| 設計檢查清單 | 容量控制、複用、淘汰、健康檢查、併發安全、背壓、生命週期 |

---

[← 上一章：httpx vs requests 深度比較](./17-httpx-vs-requests-深度比較.md) | [下一章：Async Streaming 非同步串流 →](./19-Async-Streaming-非同步串流.md)

*最後更新：2026-06-22*
