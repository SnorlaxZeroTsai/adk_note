# 第五章：Router 路由引擎

> **本章目標**：理解 LiteLLM 如何在多個 API Key / 端點之間做智慧路由、負載均衡和故障轉移。
>
> **預計閱讀時間**：25 分鐘
>
> **前置閱讀**：[第四章：Provider 抽象層](./04-Provider抽象層.md)
>
> **為什麼這章重要**：在生產環境中，你不會只有一個 API Key。你可能有 3 個 Azure 端點、2 個 OpenAI Key、再加上一個 Anthropic 作為備用。Router 負責讓這些資源被高效利用，並在某個掛掉時自動切換——這就是企業級 AI 應用和個人 side project 的核心差距。

## Router 的角色

`Router` 是 LiteLLM 的「交通指揮官」。當你有多個 LLM deployment（例如多個 OpenAI API key、多個區域的 Azure 端點），Router 負責決定每個請求應該發送到哪裡。

> **現實世界類比**：Router 就像 CDN（內容分發網路）。CDN 把使用者的請求路由到最近/最快的伺服器；LiteLLM Router 把 LLM 請求路由到最便宜/最快/最空閒的 deployment。

## 核心概念

### Deployment（部署）

一個 deployment 代表一個可以實際呼叫的 LLM 端點：

```python
# 定義多個 deployment
model_list = [
    {
        "model_name": "gpt-4",           # 邏輯名稱（使用者看到的）
        "litellm_params": {
            "model": "azure/gpt-4-east",  # 實際模型 ID
            "api_base": "https://eastus.openai.azure.com/",
            "api_key": "key-east-us",
            "rpm": 100,                    # 每分鐘請求上限
            "tpm": 40000,                  # 每分鐘 token 上限
        },
    },
    {
        "model_name": "gpt-4",           # 同一個邏輯名稱
        "litellm_params": {
            "model": "azure/gpt-4-west",  # 不同的實際端點
            "api_base": "https://westus.openai.azure.com/",
            "api_key": "key-west-us",
            "rpm": 200,
            "tpm": 80000,
        },
    },
    {
        "model_name": "gpt-4",
        "litellm_params": {
            "model": "gpt-4",             # 原生 OpenAI 作為備用
            "api_key": "sk-openai-key",
        },
    },
]

# 建立 Router
router = Router(model_list=model_list)

# 使用者只需指定邏輯名稱
response = await router.acompletion(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello"}]
)
# Router 自動選擇最佳的 deployment
```

### Model Group（模型群組）

共享同一個 `model_name` 的 deployment 構成一個 Model Group。Router 在同一群組內進行負載均衡。

## 路由策略

Router 支援多種路由策略，透過 `routing_strategy` 參數設定：

### 1. Simple Shuffle（預設）

```python
# router_strategy/simple_shuffle.py
def simple_shuffle(healthy_deployments, model):
    """
    隨機選擇一個健康的 deployment。
    如果設定了 weight/rpm/tpm，則進行加權隨機。
    """
    # 檢查是否有權重設定
    for weight_by in ["weight", "rpm", "tpm"]:
        weights = [d["litellm_params"].get(weight_by, 0) for d in healthy_deployments]
        if any(w > 0 for w in weights):
            # 加權隨機選擇
            return random.choices(healthy_deployments, weights=weights)[0]
    
    # 無權重：均勻隨機
    return random.choice(healthy_deployments)
```

**適用場景**：大多數情況下的簡單均衡分配。

### 2. Lowest Latency（最低延遲）

```python
# router_strategy/lowest_latency.py
class LowestLatencyLoggingHandler(CustomLogger):
    """
    追蹤每個 deployment 的歷史延遲，
    優先選擇回應最快的 deployment。
    """
    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        # 記錄本次呼叫的延遲
        response_ms = end_time - start_time
        # 存入快取供下次路由決策使用
        self.router_cache.set(latency_key, latency_data)
    
    def get_available_deployments(self, healthy_deployments, ...):
        # 取得每個 deployment 的平均延遲
        # 選擇延遲最低的
        sorted_by_latency = sorted(deployments, key=lambda d: d.avg_latency)
        return sorted_by_latency[0]
```

**適用場景**：延遲敏感的即時應用。

### 3. Lowest TPM/RPM（最低使用率）

```python
# 選擇當前 TPM（Token Per Minute）使用率最低的 deployment
# 確保流量均勻分散，避免單一端點過載
```

**適用場景**：避免觸及供應商的速率限制。

### 4. Cost-Based Routing（基於成本）

```python
# 優先選擇成本最低的 deployment
# 例如：同樣是 GPT-4，Azure 可能比 OpenAI 直接呼叫便宜
```

**適用場景**：成本優化為首要目標。

### 策略對比

| 策略 | 優化目標 | 最適合場景 |
|------|---------|-----------|
| `simple-shuffle` | 均勻分配 | 一般用途 |
| `latency-based-routing` | 最低延遲 | 即時聊天 |
| `usage-based-routing` | 避免速率限制 | 高流量場景 |
| `cost-based-routing` | 最低成本 | 預算有限 |
| `least-busy` | 最少併發 | 長請求場景 |

## 故障轉移（Fallback）

Router 的另一個核心功能是故障轉移：

```python
router = Router(
    model_list=model_list,
    fallbacks=[
        {"gpt-4": ["claude-3-opus"]},  # gpt-4 失敗時切換到 claude-3
    ],
    context_window_fallbacks=[
        {"gpt-4": ["gpt-4-32k"]},      # context 不夠時用更大的模型
    ],
    content_policy_fallbacks=[
        {"gpt-4": ["claude-3-opus"]},   # 內容審查被拒時試其他供應商
    ],
)
```

### Fallback 流程

```
請求 model="gpt-4"
    │
    ▼
選擇 deployment A (Azure East)
    │
    ├── 成功 → 回傳結果
    │
    └── 失敗（429 Rate Limit）
         │
         ▼
    重試 deployment B (Azure West)  ← 同群組內重試
         │
         ├── 成功 → 回傳結果
         │
         └── 失敗（所有 gpt-4 都失敗）
              │
              ▼
         Fallback 到 "claude-3-opus"  ← 跨群組 fallback
              │
              └── 成功 → 回傳結果（header 中標記是 fallback）
```

## 冷卻機制（Cooldown）

當一個 deployment 連續失敗時，Router 會暫時將它「冷卻」：

```python
# router_utils/cooldown_handlers.py（概念化）

DEFAULT_COOLDOWN_TIME_SECONDS = 60  # 預設冷卻 60 秒

def _set_cooldown_deployments(deployment_id, cooldown_time):
    """
    將失敗的 deployment 加入冷卻列表。
    冷卻期間不會被選中。
    """
    cooldown_cache.set(
        key=f"cooldown:{deployment_id}",
        value=True,
        ttl=cooldown_time,
    )

def _get_cooldown_deployments():
    """
    取得當前所有在冷卻中的 deployment。
    路由選擇時會排除這些。
    """
    return cooldown_cache.get_all_active()
```

**冷卻邏輯**：
1. deployment 回傳 5xx 或 429 錯誤
2. Router 記錄失敗次數
3. 超過 `allowed_fails` 閾值 → 進入冷卻
4. 冷卻期結束後自動恢復

## Router 的完整請求流程

```python
# router.py::acompletion()（概念化）
async def acompletion(self, model, messages, **kwargs):
    # 1. 取得該 model_group 的所有 deployment
    deployments = self.get_deployments(model=model)
    
    # 2. 過濾掉冷卻中的 deployment
    healthy = self.filter_cooldown(deployments)
    
    # 3. 根據路由策略選擇最佳 deployment
    chosen = self.routing_strategy.pick(healthy, model)
    
    # 4. 執行呼叫
    try:
        response = await litellm.acompletion(
            model=chosen["litellm_params"]["model"],
            messages=messages,
            api_key=chosen["litellm_params"]["api_key"],
            api_base=chosen["litellm_params"].get("api_base"),
            **kwargs,
        )
        # 5. 記錄成功指標（延遲、TPM 等）
        self.log_success(chosen, response)
        return response
        
    except Exception as e:
        # 6. 記錄失敗，可能觸發冷卻
        self.log_failure(chosen, e)
        
        # 7. 重試其他 deployment
        if self.should_retry(e):
            return await self._retry_with_fallback(model, messages, **kwargs)
        
        raise
```

## 健康檢查

Router 可以定期對 deployment 進行健康檢查：

```python
router = Router(
    model_list=model_list,
    # 每 60 秒檢查一次所有 deployment 的健康狀態
    health_check_interval=60,
)
```

健康檢查會發送輕量級的測試請求，確認 deployment 是否可用。

## 使用範例

### 基本使用

```python
from litellm import Router

router = Router(
    model_list=[
        {"model_name": "gpt-4", "litellm_params": {"model": "gpt-4", "api_key": "sk-1"}},
        {"model_name": "gpt-4", "litellm_params": {"model": "gpt-4", "api_key": "sk-2"}},
    ],
    routing_strategy="simple-shuffle",
    num_retries=3,
)

# 自動負載均衡
response = await router.acompletion(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello"}]
)
```

### 進階：帶權重的路由

```python
model_list = [
    {
        "model_name": "default-model",
        "litellm_params": {
            "model": "gpt-4",
            "api_key": "sk-primary",
            "weight": 3,  # 70% 流量
        },
    },
    {
        "model_name": "default-model",
        "litellm_params": {
            "model": "claude-3-opus",
            "api_key": "key-secondary",
            "weight": 1,  # 30% 流量
        },
    },
]
```

## 學習重點

1. **Model Group** 是路由的基本單位——同名 deployment 構成群組
2. **路由策略**是可插拔的——實作 `CustomLogger` 介面即可自定義
3. **冷卻機制**保護系統免受連鎖故障
4. **Fallback** 支援三種層級：同群組重試 → 跨群組 fallback → 特殊情境 fallback
5. **所有指標**（延遲、TPM、成功率）存在 DualCache 中，支援分散式部署

## 初學者常犯的錯誤

| 錯誤 | 為什麼是問題 | 正確做法 |
|------|-------------|---------|
| 只設一個 deployment | 供應商掛了整個服務就掛 | 至少 2 個 deployment + fallback |
| 不設 rpm/tpm 限制 | 所有流量打到同一個 Key，觸發 429 | 設定每個 deployment 的限額 |
| 把 API Key 硬寫在程式碼裡 | 安全風險，且無法輪換 | 用環境變數 `os.environ/KEY_NAME` |
| fallback 用同一個供應商 | 如果是供應商整體故障，fallback 也沒用 | 跨供應商 fallback（如 OpenAI → Anthropic） |

## 決策指南：該選哪種路由策略？

```
你的主要考量是什麼？
│
├─ 成本 → cost-based-routing
│
├─ 速度 → latency-based-routing
│
├─ 避免被限流 → usage-based-routing
│
├─ 不確定 / 剛開始 → simple-shuffle（預設）
│
└─ 高階需求（如 A/B 測試）→ 自定義策略 + weight 配置
```

---

[← 上一章：Provider 抽象層](./04-Provider抽象層.md) | [下一章：HTTP Handler 統一通訊層 →](./06-HTTP-Handler統一通訊層.md)
