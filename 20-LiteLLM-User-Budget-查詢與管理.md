# 第二十章：LiteLLM User Budget 查詢與管理

> **本章目標**：掌握 LiteLLM 的預算管理 API，理解多層級預算強制（Key→User→Team→Org）的設計與互動。
>
> **預計閱讀時間**：20 分鐘
>
> **前置閱讀**：[第十章：Proxy Gateway 伺服器](./10-Proxy-Gateway伺服器.md)
>
> **你會學到**：預算查詢/設定 API、花費追蹤機制、多層預算階層的衝突解決策略

> 怎麼知道某個使用者還剩多少預算？怎麼設定每日/每月限額？這篇整理 LiteLLM 的 Budget 相關 API 和資料結構。

---

## 快速回答：怎麼查使用者剩餘預算？

```bash
# 查詢使用者預算資訊
curl -X GET 'http://localhost:4000/v2/user/info?user_id=user-123' \
  --header 'Authorization: Bearer sk-your-master-key'
```

回應：
```json
{
  "user_id": "user-123",
  "spend": 10.50,
  "max_budget": 100.0,
  "budget_duration": "30d",
  "budget_reset_at": "2024-12-31T23:59:59Z"
}
```

**剩餘預算 = `max_budget` - `spend` = 100.0 - 10.50 = $89.50**

---

## 資料模型

### LiteLLM_UserTable（使用者表）

```python
class LiteLLM_UserTable:
    user_id: str
    max_budget: Optional[float]        # 預算上限（USD）
    spend: float = 0.0                 # 目前已花費
    budget_duration: Optional[str]     # 重置週期："1h", "24h", "7d", "30d", "1mo"
    budget_reset_at: Optional[datetime]  # 下次重置時間
    model_spend: Optional[Dict]        # 各模型花費明細
    model_max_budget: Optional[Dict]   # 各模型獨立預算
    tpm_limit: Optional[int]           # Tokens Per Minute 上限
    rpm_limit: Optional[int]           # Requests Per Minute 上限
```

### LiteLLM_BudgetTable（預算設定表）

可重用的預算設定，可以掛到 user / team / key 上：

```python
class LiteLLM_BudgetTable:
    budget_id: str
    max_budget: Optional[float]            # 硬性上限（超過就擋）
    soft_budget: Optional[float]           # 軟性上限（超過發警告，不擋）
    budget_duration: Optional[str]         # 重置週期
    tpm_limit: Optional[int]
    rpm_limit: Optional[int]
    max_parallel_requests: Optional[int]   # 最大同時請求數
    model_max_budget: Optional[dict]       # 各模型獨立預算
```

### 層級關係

```
Organization
  └── max_budget（組織預算）
      │
      Team
        └── max_budget（團隊預算）
            │
            User（team member）
              └── max_budget_in_team（使用者在團隊中的預算）
                  │
                  API Key
                    └── max_budget（單一 key 的預算）
```

**預算檢查順序**：Key → User → Team → Organization（任一層超過就擋）

---

## API 端點完整列表

### 查詢類

| 端點 | 方法 | 用途 |
|------|------|------|
| `/v2/user/info?user_id=xxx` | GET | 查使用者預算（輕量版） |
| `/user/info?user_id=xxx` | GET | 查使用者完整資訊（含 keys、teams） |
| `/spend/users?user_id=xxx` | GET | 查使用者花費 |
| `/spend/logs?user_id=xxx` | GET | 查使用者花費日誌 |
| `/budget/info` | POST | 查特定 budget 設定 |
| `/budget/list` | GET | 列出所有 budget 設定 |
| `/budget/settings?budget_id=xxx` | GET | 查 budget 詳細設定 |

### 管理類

| 端點 | 方法 | 用途 |
|------|------|------|
| `/user/new` | POST | 建立使用者（附帶預算） |
| `/user/update` | POST | 更新使用者預算 |
| `/user/bulk_update` | POST | 批次更新預算 |
| `/budget/new` | POST | 建立可重用的預算設定 |
| `/budget/update` | POST | 更新預算設定 |

---

## 常見操作範例

### 1. 建立帶預算的使用者

```bash
curl -X POST 'http://localhost:4000/user/new' \
  --header 'Authorization: Bearer sk-master-key' \
  --header 'Content-Type: application/json' \
  --data '{
    "user_id": "user-123",
    "user_email": "user@example.com",
    "max_budget": 100.0,
    "budget_duration": "30d",
    "tpm_limit": 10000,
    "rpm_limit": 100
  }'
```

### 2. 查詢剩餘預算

```bash
curl -X GET 'http://localhost:4000/v2/user/info?user_id=user-123' \
  --header 'Authorization: Bearer sk-master-key'
```

### 3. 更新使用者預算

```bash
curl -X POST 'http://localhost:4000/user/update' \
  --header 'Authorization: Bearer sk-master-key' \
  --header 'Content-Type: application/json' \
  --data '{
    "user_id": "user-123",
    "max_budget": 200.0
  }'
```

### 4. 設定每模型獨立預算

```bash
curl -X POST 'http://localhost:4000/budget/new' \
  --header 'Authorization: Bearer sk-master-key' \
  --header 'Content-Type: application/json' \
  --data '{
    "budget_id": "premium-user-budget",
    "max_budget": 200.0,
    "soft_budget": 150.0,
    "budget_duration": "30d",
    "model_max_budget": {
      "gpt-4o": {
        "max_budget": 100.0,
        "budget_duration": "1d",
        "tpm_limit": 50000
      },
      "claude-sonnet-4-20250514": {
        "max_budget": 80.0,
        "budget_duration": "1d"
      }
    }
  }'
```

### 5. 查看花費明細

```bash
# 查看使用者的花費日誌
curl -X GET 'http://localhost:4000/spend/logs?user_id=user-123&start_date=2024-06-01&end_date=2024-06-30' \
  --header 'Authorization: Bearer sk-master-key'
```

---

## 程式化查詢（Python SDK）

### 用 httpx 直接呼叫

```python
import httpx

LITELLM_URL = "http://localhost:4000"
MASTER_KEY = "sk-your-master-key"

async def get_user_budget(user_id: str) -> dict:
    """查詢使用者預算資訊"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{LITELLM_URL}/v2/user/info",
            params={"user_id": user_id},
            headers={"Authorization": f"Bearer {MASTER_KEY}"},
        )
        resp.raise_for_status()
        return resp.json()

async def get_remaining_budget(user_id: str) -> float:
    """計算剩餘預算"""
    info = await get_user_budget(user_id)
    max_budget = info.get("max_budget")
    spend = info.get("spend", 0.0)

    if max_budget is None:
        return float("inf")  # 沒有設預算 = 無限
    return max_budget - spend

async def check_before_request(user_id: str, estimated_cost: float) -> bool:
    """請求前預檢：預算夠不夠"""
    remaining = await get_remaining_budget(user_id)
    return remaining >= estimated_cost
```

### 在 ADK Agent 中使用

```python
from google.adk import Agent

async def budget_aware_completion(user_id: str, messages: list):
    """有預算意識的 completion 呼叫"""
    # 1. 檢查預算
    remaining = await get_remaining_budget(user_id)
    if remaining <= 0:
        return {"error": "Budget exceeded", "remaining": remaining}

    # 2. 根據剩餘預算選擇模型
    if remaining < 1.0:
        model = "gpt-4o-mini"       # 便宜模型
    elif remaining < 10.0:
        model = "gpt-4o"            # 中等模型
    else:
        model = "claude-sonnet-4-20250514"  # 高品質模型

    # 3. 發送請求
    response = await litellm.acompletion(
        model=model,
        messages=messages,
        user=user_id,  # LiteLLM 會自動追蹤花費
    )
    return response
```

---

## 預算強制執行的原理

### 檢查位置

```
請求進來
  → auth_checks.py
    → _virtual_key_max_budget_check()   # 檢查 Key 預算
    → _check_user_budget()              # 檢查 User 預算
    → team budget check                 # 檢查 Team 預算
    → org budget check                  # 檢查 Org 預算
  → 任一層超過 → 拋出 BudgetExceededError (HTTP 400)
  → 全部通過 → 轉發到 LLM Provider
```

### 核心檢查邏輯

```python
# litellm/proxy/auth/auth_checks.py
def _virtual_key_max_budget_check(valid_token):
    if (math.isfinite(valid_token.max_budget)
        and valid_token.spend >= valid_token.max_budget):
        raise litellm.BudgetExceededError(
            current_cost=valid_token.spend,
            max_budget=valid_token.max_budget,
        )
```

### 花費追蹤時機

```
請求完成（成功）
  → success_callback
    → 計算 token 用量和費用
    → 更新 LiteLLM_SpendLogs（每筆請求記錄）
    → 更新 LiteLLM_UserTable.spend（累加）
    → 更新 LiteLLM_TeamMembership.spend（如果有 team）
```

費用計算公式：
```python
cost = (input_tokens * input_cost_per_token) + (output_tokens * output_cost_per_token)
```

---

## 預算重置機制

### 支援的重置週期

| `budget_duration` | 說明 |
|-------------------|------|
| `"1h"` | 每小時重置 |
| `"24h"` | 每 24 小時重置 |
| `"7d"` | 每 7 天重置 |
| `"30d"` | 每 30 天重置 |
| `"1mo"` | 每月重置（按日曆月） |

### 重置邏輯

```python
# 簡化版
if now >= budget_reset_at:
    user.spend = 0.0  # 重置花費
    user.budget_reset_at = calculate_next_reset(budget_duration)
```

重置是**自動的**——每次有請求進來時，LiteLLM 會檢查是否該重置。

---

## Soft Budget vs Hard Budget

| 類型 | 超過時 | 用途 |
|------|--------|------|
| `max_budget`（硬性） | 拒絕請求，回 400 | 絕對上限，防止爆預算 |
| `soft_budget`（軟性） | 發送警告通知，不擋請求 | 預警，給管理者反應時間 |

設定軟性預算為硬性的 80%，提前預警：
```json
{
  "max_budget": 100.0,
  "soft_budget": 80.0
}
```

---

## 實用場景

### 場景 1：多租戶 SaaS

```python
# 免費用戶
{"user_id": "free-user", "max_budget": 5.0, "budget_duration": "30d"}

# 付費用戶
{"user_id": "paid-user", "max_budget": 100.0, "budget_duration": "30d"}

# 企業用戶
{"user_id": "enterprise-user", "max_budget": 10000.0, "budget_duration": "30d"}
```

### 場景 2：團隊預算分配

```python
# 團隊總預算 $1000/月
team = {"team_id": "eng-team", "max_budget": 1000.0, "budget_duration": "1mo"}

# 每個成員 $200/月
member = {"user_id": "dev-1", "max_budget_in_team": 200.0}
```

### 場景 3：模型分層限制

```json
{
  "max_budget": 500.0,
  "model_max_budget": {
    "gpt-4o": {"max_budget": 200.0, "budget_duration": "1d"},
    "gpt-4o-mini": {"max_budget": 50.0, "budget_duration": "1d"},
    "claude-sonnet-4-20250514": {"max_budget": 300.0, "budget_duration": "7d"}
  }
}
```

防止某個模型把整體預算吃完。

---

## 關鍵原始碼位置

| 檔案 | 內容 |
|------|------|
| `litellm/models/user.py` | User 資料模型 |
| `litellm/models/budget.py` | Budget 資料模型 |
| `litellm/proxy/management_endpoints/internal_user_endpoints.py` | User API 端點 |
| `litellm/proxy/management_endpoints/budget_management_endpoints.py` | Budget API 端點 |
| `litellm/proxy/spend_tracking/spend_management_endpoints.py` | Spend 查詢端點 |
| `litellm/proxy/auth/auth_checks.py` | 預算強制檢查 |
| `litellm/proxy/hooks/max_budget_limiter.py` | 預算 Hook |
| `litellm/proxy/hooks/model_max_budget_limiter.py` | 模型預算 Hook |

---

## 快速複習

| 重點 | 一句話 |
|------|--------|
| 查預算 | `GET /user/info?user_id=xxx` → `max_budget - spend = 剩餘` |
| 預算階層 | Key → User → Team Member → Team → Org，**最嚴格的生效** |
| budget_duration | `"1d"`, `"7d"`, `"1mo"` — 到期自動 reset spend |
| model_max_budget | 對個別模型設限，防止單一模型吃掉所有預算 |
| 強制時機 | pre-call hook 在呼叫 LLM 前檢查，超過直接 402 |

---

[← 上一章：Async Streaming 非同步串流](./19-Async-Streaming-非同步串流.md) | [下一章：Guardrails 安全護欄系統 →](./21-Guardrails-安全護欄系統.md)

*最後更新：2026-06-22*
