# 第十六章：Cooldown Cache 設計解析

> **本章目標**：理解 LiteLLM Router 的自動健康管理機制——如何偵測故障、暫停不健康部署、並自動恢復。
>
> **預計閱讀時間**：20 分鐘
>
> **前置閱讀**：[第十五章：Python LRU Cache 設計原理](./15-Python-LRU-Cache-設計原理.md)、[第五章：Router 路由引擎](./05-Router路由引擎.md)
>
> **你會學到**：TTL 驅動的自動恢復設計、失敗率統計、DualCache 在容錯中的應用

> 當某個 LLM 部署（deployment）連續失敗時，Router 如何自動「冷卻」它，避免繼續浪費請求？這就是 Cooldown Cache 的核心職責。

## 一句話總結

Cooldown Cache 是 LiteLLM Router 的**健康管理機制**——當某個部署的失敗率超過閾值，自動將它標記為不可用，等 TTL 過期後自動恢復。

---

## 核心資料結構

### CooldownCacheValue

每個被冷卻的部署，在 cache 中存一筆這樣的資料：

```python
class CooldownCacheValue(TypedDict):
    exception_received: str    # 遮罩後的錯誤訊息（前 50 字元 + 星號）
    status_code: str           # HTTP 狀態碼（字串）
    timestamp: float           # 進入冷卻的 Unix 時間戳
    cooldown_time: float       # 冷卻持續秒數
```

### Cache Key 格式

```
deployment:{model_id}:cooldown
```

使用 `@lru_cache(maxsize=1024)` 避免重複拼接字串。

---

## `@lru_cache` 的角色與用法

### 它快取什麼？

`@lru_cache` 在這裡快取的是 **cache key 字串的生成結果**，不是冷卻狀態本身：

```python
@staticmethod
@lru_cache(maxsize=1024)
def get_cooldown_cache_key(model_id: str) -> str:
    return f"deployment:{model_id}:cooldown"
```

同一個 `model_id` 在每次路由、每次查詢冷卻狀態時都會被呼叫。如果不快取，每次都要做 f-string 格式化——雖然單次很快，但在高流量下（每秒數千次路由決策）累積起來是不必要的 CPU 開銷。

### 為什麼不用 Redis？

三者職責完全不同，不是替代關係：

| 機制 | 快取什麼 | 為什麼用它 |
|------|---------|-----------|
| `@lru_cache` | key 字串（`"deployment:xxx:cooldown"`） | 省 CPU，避免重複字串拼接 |
| InMemoryCache | 冷卻狀態（CooldownCacheValue） | 本機低延遲查詢 |
| Redis | 冷卻狀態（CooldownCacheValue） | 跨 Gateway 節點同步 |

### `@lru_cache` 的特性

- **Python 標準庫**：`from functools import lru_cache`
- **進程內存**：存在 function 物件上，不涉及網路
- **LRU 淘汰策略**：超過 maxsize 時淘汰最久沒用的
- **適用場景**：純函式（相同輸入 → 相同輸出），無副作用
- **限制**：參數必須是 hashable 的（str、int、tuple 可以，dict、list 不行）

### 什麼時候該用 `@lru_cache`？

| 適合 | 不適合 |
|------|--------|
| 字串格式化結果 | 會隨時間變化的值 |
| 正則表達式編譯 | 有副作用的函式 |
| 配置解析（不變的） | 需要跨進程共享的資料 |
| 遞迴計算（如 fibonacci） | 參數是 unhashable 的型別 |

### 實際效果

```python
# 沒有 lru_cache：每次都做字串拼接
get_cooldown_cache_key("gpt-4-deploy-1")  # 拼接一次
get_cooldown_cache_key("gpt-4-deploy-1")  # 又拼接一次（浪費）

# 有 lru_cache：第一次拼接，之後直接從 dict 查
get_cooldown_cache_key("gpt-4-deploy-1")  # 拼接一次，存入 cache
get_cooldown_cache_key("gpt-4-deploy-1")  # 直接回傳，O(1) dict lookup
```

在 LiteLLM 的場景中，部署數量通常是有限的（幾十到幾百個），所以 `maxsize=1024` 足以覆蓋所有部署 ID，命中率接近 100%。

---

## 架構層次

```
┌─────────────────────────────────────────┐
│              Router                       │
│  ┌─────────────────────────────────┐    │
│  │       CooldownCache             │    │
│  │  ┌───────────────────────────┐  │    │
│  │  │       DualCache           │  │    │
│  │  │  ┌─────────┐ ┌────────┐  │  │    │
│  │  │  │ Redis   │ │InMemory│  │  │    │
│  │  │  └─────────┘ └────────┘  │  │    │
│  │  └───────────────────────────┘  │    │
│  └─────────────────────────────────┘    │
└─────────────────────────────────────────┘
```

**DualCache（雙層快取）**：
- **Redis**：跨實例共享冷卻狀態（多個 Gateway 節點同步）
- **InMemoryCache**：本機快速查詢，用 min-heap 追蹤 TTL 過期

寫入時同時寫入兩層，確保即使 Redis 延遲也不會漏掉。

---

## 完整流程

### 觸發冷卻

```
請求失敗
  → deployment_callback_on_failure()
    → _should_run_cooldown_logic()     # 前置檢查
      → _should_cooldown_deployment()  # 是否達到冷卻門檻
        → cooldown_cache.add_deployment_to_cooldown()  # 寫入 cache
          → 觸發 Prometheus metrics callback
```

### 路由時過濾

```
新請求進來
  → 取得 model group 所有部署 ID
    → _get_cooldown_deployments()      # 批次查詢哪些在冷卻中
      → _filter_cooldown_deployments() # 從可用池中移除（O(1) set lookup）
        → 若全部冷卻 → 拋出 RouterRateLimitError
        → 否則 → 從剩餘健康部署中路由
```

### 自動恢復

```
TTL 到期
  → InMemoryCache.get_cache() 檢查 ttl_dict
    → 發現已過期 → 刪除 key → 回傳 None
      → 下次路由時，該部署不再出現在冷卻列表
        → 自動回到健康池
```

**沒有手動解除機制**——完全靠 TTL 過期自動恢復。

---

## 冷卻判定邏輯

### 哪些錯誤會觸發冷卻？

| 狀態碼 | 是否冷卻 | 原因 |
|--------|---------|------|
| 429    | ✅ | Rate limit，該部署已滿載 |
| 401    | ✅ | 認證失敗，key 可能已失效 |
| 408    | ✅ | 超時，服務可能不穩定 |
| 404    | ✅ | 模型不存在 |
| 5xx    | ✅ | 伺服器端錯誤 |
| APIConnectionError | ❌ | 可能是暫時網路問題 |
| 其他 4xx | ❌ | 通常是請求端問題 |

### V2 判定策略（預設）

不是一失敗就冷卻，而是根據**失敗率**判斷：

```python
percent_fails = fails / (successes + fails)
```

觸發條件（滿足任一）：
1. **429 且不是單一部署** → 立即冷卻
2. **100% 失敗率** 且流量 ≥ 1000 次/分鐘 → 冷卻（大流量全掛）
3. **失敗率 > 50%** 且總請求 ≥ 5 次 → 冷卻（統計顯著）
4. **不可重試的錯誤**（401、404） → 冷卻

### Legacy 策略（AllowedFailsPolicy）

可按錯誤類型設定允許失敗次數：

```python
class AllowedFailsPolicy(BaseModel):
    BadRequestErrorAllowedFails: Optional[int] = None
    AuthenticationErrorAllowedFails: Optional[int] = None
    TimeoutErrorAllowedFails: Optional[int] = None
    RateLimitErrorAllowedFails: Optional[int] = None
    ContentPolicyViolationErrorAllowedFails: Optional[int] = None
    InternalServerErrorAllowedFails: Optional[int] = None
```

---

## 設定參數

### Router 層級

```python
Router(
    cooldown_time=5.0,           # 冷卻秒數（預設 5 秒）
    allowed_fails=3,             # 允許失敗次數（legacy 模式）
    allowed_fails_policy=...,    # 按錯誤類型設定
    disable_cooldowns=False,     # 設 True 完全關閉冷卻機制
)
```

### 部署層級（覆蓋 Router 預設值）

```python
{
    "model_name": "gpt-4",
    "litellm_params": {
        "model": "azure/gpt-4",
        "cooldown_time": 10.0    # 這個部署冷卻 10 秒
    }
}
```

### 環境變數

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `DEFAULT_COOLDOWN_TIME_SECONDS` | 5 | 預設冷卻秒數 |
| `DEFAULT_FAILURE_THRESHOLD_PERCENT` | 0.5 | 失敗率門檻（50%） |
| `DEFAULT_FAILURE_THRESHOLD_MINIMUM_REQUESTS` | 5 | 最少請求數才計算失敗率 |
| `SINGLE_DEPLOYMENT_TRAFFIC_FAILURE_THRESHOLD` | 1000 | 大流量全失敗門檻 |

### 優先順序

```
部署層級 cooldown_time > Response Header 指定 > Router 預設值
```

---

## 安全設計：錯誤訊息遮罩

冷卻記錄中的 `exception_received` 會被遮罩處理：

```python
SensitiveDataMasker(
    visible_prefix=50,   # 只顯示前 50 字元
    visible_suffix=0,    # 尾部全部遮蔽
    mask_char="*"        # 用星號替代
)
```

防止 prompt 內容或 API Key 被存入 cache 或暴露在 log 中。

---

## 邊界情況處理

### 所有部署都冷卻了怎麼辦？

拋出 `RouterRateLimitError`，附帶：
- `cooldown_time`：所有冷卻中部署的最短剩餘時間
- `cooldown_list`：正在冷卻的部署 ID 列表

讓呼叫端知道「最快什麼時候可以重試」。

### Health Check 旁路

如果啟用了健康檢查路由 + AllowedFailsPolicy，即使全部冷卻也不會過濾掉——確保健康檢查能繼續監控。

### 單一部署不冷卻 429

如果整個 model group 只有一個部署，收到 429 時**不冷卻**——因為沒有其他部署可用，冷卻了等於完全不能用。

---

## 與其他模組的關係

| 模組 | 互動方式 |
|------|---------|
| Router | 路由前查詢冷卻列表，過濾不健康部署 |
| Failover | 冷卻後剩餘部署接管流量 |
| Retry | 重試時自動跳過冷卻中的部署 |
| DualCache | 儲存冷卻狀態的底層 |
| Prometheus | 冷卻事件觸發 metrics 上報 |
| InMemoryCache | min-heap TTL 實現自動恢復 |

---

## 設計亮點

1. **TTL 自動恢復**：不需要額外的「恢復邏輯」，cache 過期 = 自動恢復
2. **雙層 cache**：Redis 保證跨節點一致，InMemory 保證低延遲
3. **統計驅動**：不是一次失敗就冷卻，而是基於失敗率，避免誤判
4. **最少請求數門檻**：流量太低時不計算失敗率，避免統計偏差
5. **錯誤遮罩**：防止敏感資訊洩漏
6. **可配置粒度**：全域、Router 層、部署層三級配置

---

## 源碼位置

| 元件 | 路徑 |
|------|------|
| CooldownCache 類別 | `litellm/router_utils/cooldown_cache.py` |
| 冷卻判定邏輯 | `litellm/router_utils/cooldown_handlers.py` |
| 冷卻 Metrics Callback | `litellm/router_utils/cooldown_callbacks.py` |
| Router 整合 | `litellm/router.py` |
| DualCache | `litellm/caching/dual_cache.py` |
| InMemoryCache | `litellm/caching/in_memory_cache.py` |
| 型別定義 | `litellm/types/router.py` |
| 常數設定 | `litellm/constants.py` |

---

[← 上一章：Python LRU Cache 設計原理](./15-Python-LRU-Cache-設計原理.md) | [下一章：httpx vs requests 深度比較 →](./17-httpx-vs-requests-深度比較.md)

*最後更新：2026-06-22*
