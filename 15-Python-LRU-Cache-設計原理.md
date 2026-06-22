# 第十五章：Python LRU Cache 設計原理

> **本章目標**：理解 LRU 淘汰策略的核心資料結構，能自己從零實作一個 O(1) 的 LRU Cache，並掌握 Python `@lru_cache` 的正確用法與陷阱。
>
> **預計閱讀時間**：25 分鐘
>
> **前置閱讀**：[第九章：快取系統](./09-快取系統.md)
>
> **你會學到**：Hash Map + 雙向鏈結串列設計、CPython 源碼的實作技巧、`@lru_cache` 的使用時機與地雷

> LRU（Least Recently Used）是最常見的快取淘汰策略——當快取滿了，丟掉最久沒被存取的那一筆。這篇從零設計一個 LRU Cache，再對照 Python 標準庫的實作。

---

## 核心需求

一個 LRU Cache 必須支援兩個操作，且都要 **O(1)**：

| 操作 | 說明 |
|------|------|
| `get(key)` | 取值，並標記為「最近使用」 |
| `put(key, value)` | 寫入，滿了就淘汰最久沒用的 |

---

## 資料結構選擇

### 為什麼 dict 不夠？

Python dict 是 hash map，`get` / `put` 都是 O(1)，但**沒有順序資訊**（Python 3.7+ 的 dict 保留插入順序，但不追蹤「存取順序」）。

### 為什麼 list 不夠？

list 可以追蹤順序，但把某個元素移到最前面是 O(n)。

### 正解：Hash Map + Doubly Linked List

```
┌────────────────────────────────────────────┐
│  Hash Map (dict)                           │
│  key → Node pointer                        │
│                                            │
│  "a" → [Node A]                            │
│  "b" → [Node B]                            │
│  "c" → [Node C]                            │
└────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────┐
│  Doubly Linked List（最近使用 → 最久沒用）              │
│                                                      │
│  HEAD ↔ [Node C] ↔ [Node A] ↔ [Node B] ↔ TAIL      │
│  (dummy)  最近用     ↑中間      最久沒用   (dummy)    │
└──────────────────────────────────────────────────────┘
```

| 操作 | 實作 | 複雜度 |
|------|------|--------|
| get | dict 查 node → 移到 head 後面 | O(1) |
| put | dict 寫入 + 插入 head 後面 | O(1) |
| 淘汰 | 刪除 tail 前面的 node + dict 刪 key | O(1) |

---

## 從零實作

### 第一步：定義 Node

```python
class Node:
    __slots__ = ('key', 'value', 'prev', 'next')

    def __init__(self, key=None, value=None):
        self.key = key
        self.value = value
        self.prev = None
        self.next = None
```

`__slots__` 省記憶體，LRU cache 可能存大量節點。

### 第二步：LRU Cache 本體

```python
class LRUCache:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.cache = {}  # key → Node

        # dummy head & tail，簡化邊界處理
        self.head = Node()
        self.tail = Node()
        self.head.next = self.tail
        self.tail.prev = self.head

    def get(self, key):
        if key not in self.cache:
            return None
        node = self.cache[key]
        self._move_to_front(node)
        return node.value

    def put(self, key, value):
        if key in self.cache:
            node = self.cache[key]
            node.value = value
            self._move_to_front(node)
        else:
            if len(self.cache) >= self.capacity:
                self._evict()
            node = Node(key, value)
            self.cache[key] = node
            self._add_to_front(node)

    def _add_to_front(self, node):
        """插入到 head 後面（最近使用的位置）"""
        node.prev = self.head
        node.next = self.head.next
        self.head.next.prev = node
        self.head.next = node

    def _remove(self, node):
        """從鏈結串列中移除"""
        node.prev.next = node.next
        node.next.prev = node.prev

    def _move_to_front(self, node):
        """標記為最近使用"""
        self._remove(node)
        self._add_to_front(node)

    def _evict(self):
        """淘汰最久沒用的（tail 前面那個）"""
        lru_node = self.tail.prev
        self._remove(lru_node)
        del self.cache[lru_node.key]
```

### 使用範例

```python
cache = LRUCache(capacity=3)

cache.put("a", 1)
cache.put("b", 2)
cache.put("c", 3)
# 鏈結串列: c ↔ b ↔ a

cache.get("a")
# 鏈結串列: a ↔ c ↔ b（a 被存取，移到最前面）

cache.put("d", 4)
# 容量滿了，淘汰 b（最久沒用的）
# 鏈結串列: d ↔ a ↔ c
```

---

## Python 標準庫的做法：`OrderedDict`

Python 的 `collections.OrderedDict` 內部就是 hash map + doubly linked list，而且提供 `move_to_end()` 方法：

```python
from collections import OrderedDict

class LRUCache:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.cache = OrderedDict()

    def get(self, key):
        if key not in self.cache:
            return None
        self.cache.move_to_end(key)  # 標記為最近使用
        return self.cache[key]

    def put(self, key, value):
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = value
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)  # 淘汰最久沒用的（最前面）
```

更簡潔，但原理相同。

---

## `functools.lru_cache` 的內部實作

CPython 的 `@lru_cache` 用的是**循環雙向鏈結串列**（circular doubly linked list），每個節點是一個長度為 4 的 list：

```python
# CPython 源碼中每個 cache entry 的結構
# [PREV, NEXT, KEY, RESULT]
#   0     1     2     3
```

### 為什麼用 circular list？

```
     ┌──────────────────────────┐
     ↓                          │
  [root] ↔ [newest] ↔ ... ↔ [oldest]
     ↑                          │
     └──────────────────────────┘
```

- 不需要 dummy head/tail，root 節點本身就是哨兵
- `root.next` = 最新的（MRU）
- `root.prev` = 最舊的（LRU，要淘汰的）
- 淘汰時直接 `root.prev` 就是目標，不用遍歷

### CPython 源碼簡化版

```python
# 參考 CPython Lib/functools.py 的核心邏輯
def _lru_cache_wrapper(user_function, maxsize):
    cache = {}
    root = []            # 循環鏈結串列的 root
    root[:] = [root, root, None, None]  # 自己指向自己

    PREV, NEXT, KEY, RESULT = 0, 1, 2, 3
    hits = misses = 0

    def wrapper(*args):
        nonlocal hits, misses
        key = args  # 簡化，實際會用 _make_key()

        link = cache.get(key)
        if link is not None:
            # Cache hit：把 link 移到最前面
            link_prev, link_next, _key, result = link
            link_prev[NEXT] = link_next
            link_next[PREV] = link_prev
            last = root[PREV]
            last[NEXT] = root[PREV] = link
            link[PREV] = last
            link[NEXT] = root
            hits += 1
            return result

        # Cache miss：呼叫函式
        result = user_function(*args)
        misses += 1

        if len(cache) >= maxsize:
            # 淘汰最舊的（root[NEXT]）
            oldest = root[NEXT]
            oldest_prev, oldest_next = oldest[PREV], oldest[NEXT]
            oldest_prev[NEXT] = oldest_next
            oldest_next[PREV] = oldest_prev
            del cache[oldest[KEY]]

        # 插入新節點到最前面（root[PREV] 位置）
        last = root[PREV]
        link = [last, root, key, result]
        last[NEXT] = root[PREV] = link
        cache[key] = link

        return result

    return wrapper
```

### 關鍵設計決策

| 決策 | 原因 |
|------|------|
| 用 list 而非 class 當節點 | C 層面的 list 存取比 attribute lookup 快 |
| 用 index（0,1,2,3）不用 `.prev` | 省去 `__getattr__` 開銷 |
| 循環鏈結串列 | 少一個判斷分支，code path 更短 |
| `_make_key` 把 args/kwargs 轉 tuple | 確保所有參數組合都 hashable |
| Thread lock（`_lru_cache_wrapper` 完整版） | 多線程安全 |

---

## `@lru_cache` 使用技巧

### 基本用法

```python
from functools import lru_cache

@lru_cache(maxsize=128)
def expensive_compute(x, y):
    return x ** y
```

### 查看快取統計

```python
expensive_compute.cache_info()
# CacheInfo(hits=10, misses=3, maxsize=128, currsize=3)
```

### 手動清除

```python
expensive_compute.cache_clear()
```

### `maxsize=None`：無限快取

```python
@lru_cache(maxsize=None)
def fibonacci(n):
    if n < 2:
        return n
    return fibonacci(n-1) + fibonacci(n-2)
```

不淘汰任何 entry，等同於 memoization。內部用純 dict（沒有鏈結串列開銷）。

### 常見陷阱

```python
# ❌ 參數是 unhashable 的 → TypeError
@lru_cache
def bad(data: list):
    return sum(data)

# ✅ 轉成 tuple
@lru_cache
def good(data: tuple):
    return sum(data)

# ❌ 用在有副作用的函式 → 第二次呼叫不會執行函式體
@lru_cache
def bad_side_effect(x):
    print(f"computing {x}")  # 只會印一次
    return x * 2

# ❌ 用在方法上 → self 會被當成 key 的一部份，導致 instance 無法被 GC
class Bad:
    @lru_cache
    def method(self, x):  # self 被快取住，記憶體洩漏
        return x

# ✅ 方法上用 __hash__ 或改用外部函式
```

### Python 3.9+：`@cache`

```python
from functools import cache

@cache  # 等同於 @lru_cache(maxsize=None)
def fibonacci(n):
    if n < 2:
        return n
    return fibonacci(n-1) + fibonacci(n-2)
```

---

## 進階：自己設計時的考量

### 要不要支援 TTL（過期時間）？

標準 `@lru_cache` 不支援 TTL。如果需要：

```python
import time
from functools import wraps

def ttl_lru_cache(maxsize=128, ttl=60):
    def decorator(func):
        cache = OrderedDict()

        @wraps(func)
        def wrapper(*args):
            now = time.time()
            if args in cache:
                result, timestamp = cache[args]
                if now - timestamp < ttl:
                    cache.move_to_end(args)
                    return result
                else:
                    del cache[args]  # 過期了

            result = func(*args)
            cache[args] = (result, now)
            if len(cache) > maxsize:
                cache.popitem(last=False)
            return result

        return wrapper
    return decorator
```

### 要不要支援多線程？

CPython 的 `@lru_cache` 用 `threading.RLock` 保護。自己實作時：

```python
import threading

class ThreadSafeLRUCache:
    def __init__(self, capacity):
        self.capacity = capacity
        self.cache = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            if key not in self.cache:
                return None
            self.cache.move_to_end(key)
            return self.cache[key]

    def put(self, key, value):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            self.cache[key] = value
            if len(self.cache) > self.capacity:
                self.cache.popitem(last=False)
```

### 要不要支援 async？

`@lru_cache` 可以裝飾 async 函式，但快取的是 **coroutine 物件**，不是結果：

```python
# ❌ 這樣不行：快取的是 coroutine，不是結果
@lru_cache
async def fetch(url):
    ...

# ✅ 方案一：用第三方庫 aiocache / async-lru
from async_lru import alru_cache

@alru_cache(maxsize=128)
async def fetch(url):
    ...

# ✅ 方案二：自己包一層
def async_lru_cache(maxsize=128):
    def decorator(func):
        cache = OrderedDict()

        @wraps(func)
        async def wrapper(*args):
            if args in cache:
                cache.move_to_end(args)
                return cache[args]
            result = await func(*args)
            cache[args] = result
            if len(cache) > maxsize:
                cache.popitem(last=False)
            return result

        return wrapper
    return decorator
```

---

## 與 LiteLLM Cooldown Cache 的關聯

在 Cooldown Cache 的設計中，三種快取各司其職：

```
@lru_cache          →  快取 key 字串生成（進程內，純 CPU 優化）
InMemoryCache       →  快取冷卻狀態（進程內，min-heap TTL）
Redis               →  快取冷卻狀態（跨節點，分散式一致性）
```

`@lru_cache` 處理的是**不變的計算結果**（相同 model_id → 相同 key string），而 InMemoryCache / Redis 處理的是**會過期的狀態**。選擇哪個取決於：

| 考量 | 用 `@lru_cache` | 用 InMemory/Redis |
|------|-----------------|-------------------|
| 值會變嗎？ | 不會（純函式） | 會（有 TTL 或更新） |
| 需要跨進程？ | 不需要 | Redis 需要 |
| 淘汰策略 | LRU（容量滿了淘汰） | TTL（時間到了過期） |
| 複雜度 | 一行裝飾器 | 需要額外基礎設施 |

---

## 快速複習

| 重點 | 一句話 |
|------|--------|
| LRU 核心結構 | Hash Map（O(1) 查找）+ 雙向鏈結串列（O(1) 移動/刪除） |
| CPython 實作 | 環形雙向鏈結串列 + dict，用 sentinel node 簡化邊界 |
| `@lru_cache` 適用 | 純函式、不可變參數、重複計算多 |
| `@lru_cache` 陷阱 | 不能用 mutable 參數、無 TTL、記憶體可能無限增長（`maxsize=None`） |
| 在 LiteLLM 中的角色 | 快取 key string 生成（純 CPU 優化），不快取會過期的狀態 |

---

[← 上一章：Token Budget 策略與 MCP 實作](./14-Token-Budget-策略與MCP實作.md) | [下一章：Cooldown Cache 設計解析 →](./16-Cooldown-Cache-設計解析.md)

*最後更新：2026-06-22*
