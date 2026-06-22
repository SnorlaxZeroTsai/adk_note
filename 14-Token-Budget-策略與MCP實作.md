# 第十四章：Token Budget 策略與 MCP 實作

> **本章目標**：學會如何規劃和管理 AI 應用的 Token 預算，以及如何用 MCP 建立自動化的預算顧問工具。
>
> **預計閱讀時間**：30 分鐘
>
> **前置閱讀**：[第八章：成本計算引擎](./08-成本計算引擎.md)
>
> **本章特色**：這是最貼近「日常工作」的章節——無論你是獨立開發者還是企業工程師，Token 預算管理都是你上線 AI 產品後第一個要面對的問題。本章附帶一個完整可運行的 MCP Server。
>
> **什麼是 MCP？**：Model Context Protocol（模型上下文協議）是一個讓 AI 助手（如 Claude）調用外部工具的標準。類似於瀏覽器的插件機制——MCP Server 提供「工具」，AI 助手在需要時調用這些工具來獲取即時資訊。

## 為什麼需要 Token Budget 管理？

在 AI 應用中，Token 是你的「燃料費」。每次 API 呼叫都消耗 Token，而不同模型的定價差距可達 **100 倍以上**：

| 模型 | Input ($/1M tokens) | Output ($/1M tokens) | Context Window |
|------|---------------------|----------------------|----------------|
| GPT-4o | $2.50 | $10.00 | 128K |
| GPT-4o-mini | $0.15 | $0.60 | 128K |
| Claude Opus 4 | $15.00 | $75.00 | 200K |
| Claude Sonnet 4 | $3.00 | $15.00 | 200K |
| Gemini 2.0 Flash | $0.10 | $0.40 | 1M |
| DeepSeek Chat | $0.28 | $0.42 | 131K |

> 數據來源：LiteLLM `model_prices_and_context_window.json`（2843+ 模型定價）

## Token Budget 核心概念

### 1. Token 消耗公式

```
單次呼叫成本 = (input_tokens × input_price) + (output_tokens × output_price)
月度成本 = 單次成本 × 每日呼叫次數 × 30
```

### 2. 預算分配維度

```
┌─────────────────────────────────────────────────┐
│              Token Budget 分配框架               │
├─────────────────────────────────────────────────┤
│                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐     │
│  │ 模型分層  │  │ 快取策略  │  │ 流量控制  │     │
│  │          │  │          │  │          │     │
│  │ Premium  │  │ System   │  │ Rate     │     │
│  │ Standard │  │ Prompt   │  │ Limiting │     │
│  │ Economy  │  │ Caching  │  │          │     │
│  └──────────┘  └──────────┘  └──────────┘     │
│        │              │              │          │
│        ▼              ▼              ▼          │
│  ┌─────────────────────────────────────────┐   │
│  │         LiteLLM Router + Budget          │   │
│  └─────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

## 策略一：模型分層路由

將請求按複雜度路由到不同成本的模型：

```python
import litellm
from litellm import Router

# 定義多層模型部署
model_list = [
    {
        "model_name": "premium",  # 複雜推理、創作
        "litellm_params": {
            "model": "claude-opus-4-20250514",
            "api_key": "sk-..."
        }
    },
    {
        "model_name": "standard",  # 一般對話、摘要
        "litellm_params": {
            "model": "gpt-4o",
            "api_key": "sk-..."
        }
    },
    {
        "model_name": "economy",  # 分類、提取、簡單問答
        "litellm_params": {
            "model": "gpt-4o-mini",
            "api_key": "sk-..."
        }
    },
]

router = Router(model_list=model_list)
```

### 路由決策邏輯

```python
def route_request(user_message: str, complexity: str) -> str:
    """根據任務複雜度選擇模型層級"""
    tier_map = {
        "high": "premium",      # 多步推理、程式生成、長文創作
        "medium": "standard",   # 摘要、翻譯、一般問答
        "low": "economy",       # 分類、關鍵字提取、格式轉換
    }
    model_name = tier_map.get(complexity, "standard")
    
    response = router.completion(
        model=model_name,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.choices[0].message.content
```

### 成本對比範例

假設每月 10,000 次呼叫，平均 2000 input + 1000 output tokens：

| 策略 | 月成本 | 說明 |
|------|--------|------|
| 全用 Claude Opus 4 | $1,050 | 最高品質 |
| 全用 GPT-4o | $150 | 平衡選擇 |
| 分層路由 (20/30/50) | $78 | 20% premium + 30% standard + 50% economy |
| 全用 GPT-4o-mini | $9 | 最省但品質有限 |

**分層路由可節省 48-92% 的成本**，同時保持對關鍵請求的品質。

## 策略二：Prompt Caching（提示快取）

對於重複使用的 System Prompt 或 Few-shot Examples，快取能節省 80-90% 的輸入成本：

```python
# Anthropic 的 Prompt Caching
response = litellm.completion(
    model="claude-sonnet-4-20250514",
    messages=[
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": "你是一個程式碼審查助手...(很長的系統提示)...",
                    "cache_control": {"type": "ephemeral"}  # 標記為可快取
                }
            ]
        },
        {"role": "user", "content": "請審查這段程式碼..."}
    ]
)

# 快取命中時，input 成本降低 90%
# Claude: cache_read = $0.30/1M vs normal = $3.00/1M (Sonnet)
```

### 快取策略適用場景

| 場景 | 可快取內容 | 預估節省 |
|------|-----------|---------|
| 客服機器人 | System prompt + FAQ 知識庫 | 85-90% input cost |
| 程式碼審查 | 規則文件 + 風格指南 | 80-85% input cost |
| RAG 應用 | 常用文件片段 | 60-70% input cost |
| 多輪對話 | 早期對話歷史 | 40-60% input cost |

## 策略三：Token 上限控制

設定 `max_tokens` 防止輸出失控：

```python
response = litellm.completion(
    model="gpt-4o",
    messages=messages,
    max_tokens=500,  # 限制輸出 tokens
)

# 搭配 LiteLLM Router 的預算控制
router = Router(
    model_list=model_list,
    routing_strategy="cost-based-routing",  # 自動選最便宜的可用模型
    set_verbose=True,
)
```

## 策略四：批次處理與非同步

```python
import asyncio
import litellm

async def batch_process(prompts: list[str], model: str = "gpt-4o-mini"):
    """批次處理降低延遲開銷，搭配便宜模型"""
    tasks = [
        litellm.acompletion(
            model=model,
            messages=[{"role": "user", "content": p}],
            max_tokens=200,
        )
        for p in prompts
    ]
    return await asyncio.gather(*tasks)
```

## 預算監控與告警

```python
from litellm import BudgetManager

budget_manager = BudgetManager(project_name="my-ai-app")

# 設定每位使用者的預算
budget_manager.create_budget(
    total_budget=10.0,       # $10 美元
    user="user_123",
    duration="monthly",
)

# 每次呼叫前檢查
if budget_manager.get_current_cost(user="user_123") < budget_manager.get_total_budget(user="user_123"):
    response = litellm.completion(...)
    budget_manager.update_cost(
        user="user_123",
        completion_obj=response,
    )
else:
    raise Exception("Budget exceeded!")
```

## MCP Server 實作：Token Budget Advisor

我們用 MCP（Model Context Protocol）建立一個 Token Budget 顧問服務，讓 AI 助手能即時查詢模型定價並提供預算建議。

### 架構設計

```
┌─────────────────────────────────────────┐
│           AI Assistant (Client)          │
│  (Claude Code / Cursor / Custom App)    │
└───────────────────┬─────────────────────┘
                    │ MCP Protocol (stdio/SSE)
                    ▼
┌─────────────────────────────────────────┐
│        Token Budget Advisor MCP         │
│                                         │
│  Tools:                                 │
│  ├─ get_model_info()                    │
│  ├─ estimate_cost()                     │
│  ├─ budget_advisor()                    │
│  ├─ compare_models()                    │
│  ├─ token_budget_strategies()           │
│  ├─ find_cheapest_models()              │
│  └─ list_providers()                    │
└───────────────────┬─────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│  LiteLLM model_prices_and_context_      │
│  window.json (2843+ models)             │
└─────────────────────────────────────────┘
```

### MCP Server 核心程式碼

```python
from mcp.server.fastmcp import FastMCP
import json

mcp = FastMCP("token-budget-advisor")

@mcp.tool()
def budget_advisor(
    monthly_budget_usd: float,
    avg_input_tokens_per_call: int,
    avg_output_tokens_per_call: int,
    expected_calls_per_day: int,
    use_case: str = "general",
) -> str:
    """
    根據月預算、平均 token 用量、每日呼叫量，
    推薦符合預算的模型及成本分析。
    """
    chat_models = get_chat_models()  # 從 LiteLLM JSON 載入
    monthly_calls = expected_calls_per_day * 30
    
    recommendations = []
    for name, info in chat_models.items():
        cost_per_call = calculate_cost(info, avg_input_tokens, avg_output_tokens)
        monthly_cost = cost_per_call * monthly_calls
        
        if monthly_cost <= monthly_budget_usd:
            recommendations.append({
                "model": name,
                "monthly_cost": monthly_cost,
                "budget_utilization": monthly_cost / monthly_budget_usd * 100,
            })
    
    return json.dumps(recommendations)
```

### 設定 MCP Server

在 Claude Code 的 `.claude/settings.json` 中加入：

```json
{
  "mcpServers": {
    "token-budget-advisor": {
      "command": "python",
      "args": ["path/to/server.py"],
      "env": {
        "LITELLM_MODEL_PRICES_PATH": "/path/to/model_prices_and_context_window.json"
      }
    }
  }
}
```

### 使用範例

向 AI 助手提問：

```
「我每月有 $50 的預算，平均每次呼叫 3000 input tokens + 1500 output tokens，
每天大約 200 次呼叫。主要用於程式碼生成。推薦什麼模型？」
```

MCP 會呼叫 `budget_advisor` 工具並返回：

```json
{
  "budget": {"monthly_usd": 50, "per_call_budget_usd": 0.008333},
  "recommendations": [
    {
      "model": "gpt-4o-mini",
      "monthly_cost_usd": 8.10,
      "budget_utilization_pct": 16.2
    },
    {
      "model": "deepseek/deepseek-chat",
      "monthly_cost_usd": 5.46,
      "budget_utilization_pct": 10.9
    },
    {
      "model": "gemini/gemini-2.0-flash",
      "monthly_cost_usd": 4.20,
      "budget_utilization_pct": 8.4
    }
  ]
}
```

## Token Budget 決策樹

```
你的需求是什麼？
│
├─ 最高品質，不計成本
│   └─ Claude Opus 4 / GPT-4o (with caching)
│
├─ 平衡品質與成本
│   ├─ 長上下文 → Claude Sonnet 4 (200K) / Gemini 2.0 Flash (1M)
│   ├─ 函式呼叫 → GPT-4o / Claude Sonnet 4
│   └─ 一般對話 → GPT-4o / Claude Sonnet 4
│
├─ 成本優先
│   ├─ 大量簡單任務 → GPT-4o-mini / Gemini Flash
│   ├─ 可接受開源 → DeepSeek Chat
│   └─ 需要推理 → DeepSeek Reasoner
│
└─ 混合策略（推薦）
    ├─ 路由器：複雜→Premium, 簡單→Economy
    ├─ 快取：重複 prompt 用 cache 節省 90%
    └─ 限制：設定 max_tokens 防溢出
```

## 實戰公式：估算月度預算

```python
def estimate_monthly_budget(
    daily_users: int,
    avg_messages_per_user: int,
    avg_input_tokens: int = 2000,
    avg_output_tokens: int = 800,
    model: str = "gpt-4o-mini",
):
    """快速估算月度 AI 成本"""
    # 從 LiteLLM 取得定價
    prices = load_model_data()[model]
    input_price = prices["input_cost_per_token"]
    output_price = prices["output_cost_per_token"]
    
    daily_calls = daily_users * avg_messages_per_user
    cost_per_call = input_price * avg_input_tokens + output_price * avg_output_tokens
    
    monthly = cost_per_call * daily_calls * 30
    
    return {
        "monthly_cost": round(monthly, 2),
        "cost_per_user_per_month": round(monthly / daily_users, 4),
        "cost_per_call": round(cost_per_call, 6),
    }

# 範例：1000 DAU, 每人 5 則訊息, 用 GPT-4o-mini
# → 月成本約 $13.50, 每用戶 $0.0135/月
```

## 關鍵建議摘要

| # | 建議 | 預期節省 |
|---|------|---------|
| 1 | 使用模型分層路由，非所有請求都需要頂級模型 | 50-80% |
| 2 | 啟用 Prompt Caching（Anthropic / OpenAI 都支援） | 80-90% input |
| 3 | 設定 max_tokens 限制輸出長度 | 防止 10x 超支 |
| 4 | 用 LiteLLM Router 的 cost-based-routing | 自動選最便宜 |
| 5 | 監控實際用量，每週調整預算分配 | 持續優化 |
| 6 | 批次處理非即時任務，使用更便宜的模型 | 30-50% |
| 7 | 用 MCP 工具即時查詢最新定價做決策 | 避免資訊過時 |

## 進階：用 MCP 打造 AI 成本治理平台

將此 MCP Server 擴展為完整的成本治理：

1. **即時定價查詢**：模型價格隨時變動，MCP 確保使用最新數據
2. **預算告警**：整合 BudgetManager，接近預算時自動切換便宜模型
3. **用量報告**：追蹤每個使用者/專案的消耗趨勢
4. **智慧路由建議**：根據歷史數據推薦最優的模型組合

```python
# 未來擴展方向
@mcp.tool()
def optimize_current_usage(usage_log_path: str) -> str:
    """分析歷史用量日誌，推薦優化方案"""
    ...

@mcp.tool()
def simulate_migration(from_model: str, to_model: str, monthly_calls: int) -> str:
    """模擬模型遷移的成本影響"""
    ...
```

---

## 本章小結

**Token Budget 管理不是「選最便宜的」，而是「讓每一分錢花在對的地方」。** 透過分層路由、快取策略、和 MCP 工具的即時數據支援，你可以在控制成本的同時維持應用品質。

## 初學者行動清單

完成本教學系列後，這些是你可以立即做的事情：

- [ ] 用第 8 章的公式計算你目前 app 的月度 AI 成本
- [ ] 如果成本超過預期，用本章的「決策樹」找到更合適的模型
- [ ] 設定 `max_tokens` 防止意外超支
- [ ] 如果有重複的 system prompt，啟用 prompt caching
- [ ] 跑一下本章附帶的 MCP Server，體驗即時定價查詢
- [ ] 把你的心得寫成一篇文章或內部分享——教別人是最好的學習

---

[← 上一章：AI 工程師進階知識](./13-AI工程師進階知識.md) | [下一章：Python LRU Cache 設計原理 →](./15-Python-LRU-Cache-設計原理.md)
