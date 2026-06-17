# 第十章：Proxy Gateway 伺服器

> **本章目標**：理解 LiteLLM Proxy 的功能和架構，學會何時需要它以及如何配置。
>
> **預計閱讀時間**：25 分鐘
>
> **前置閱讀**：[第五章：Router 路由引擎](./05-Router路由引擎.md)
>
> **何時需要 Proxy？**：只要你的 AI 應用不是一人獨用的 side project，你就需要考慮 Proxy。它解決的是「如何讓一個團隊安全地共用 AI 資源」的問題。

## 什麼是 LiteLLM Proxy？

LiteLLM Proxy 是一個完整的 **AI Gateway**——一個站在你的應用和 LLM 供應商之間的中間伺服器。它提供了企業級功能：

```
┌─────────────────────────────────────────────────┐
│                 你的應用程式                       │
│  (使用 OpenAI SDK 或任何 HTTP 客戶端)            │
└───────────────────────┬─────────────────────────┘
                        │ POST /v1/chat/completions
                        ▼
┌─────────────────────────────────────────────────┐
│            LiteLLM Proxy Gateway                 │
│                                                 │
│  ✓ API Key 認證    ✓ 速率限制    ✓ 預算管理     │
│  ✓ 負載均衡        ✓ 故障轉移    ✓ 日誌追蹤     │
│  ✓ 內容審查        ✓ 成本追蹤    ✓ 管理 UI      │
└───────────────────────┬─────────────────────────┘
                        │
         ┌──────────────┼──────────────┐
         │              │              │
         ▼              ▼              ▼
    OpenAI API    Anthropic API   Azure API ...
```

## 為什麼需要 Proxy？

直接用 SDK 的問題：
1. **API Key 暴露**：每個開發者都能看到 API Key
2. **無法限制使用**：誰用了多少 token？超預算了嗎？
3. **無法集中管理**：100 個服務各自呼叫，無法統一路由
4. **無可觀測性**：誰在什麼時候呼叫了什麼？成功率？

Proxy 解決了所有這些問題，且**對客戶端完全透明**——你的應用只需要把 `api_base` 指向 Proxy。

## Proxy 的核心架構

### 請求生命週期

```python
# proxy/proxy_server.py（概念化）

@app.post("/v1/chat/completions")
async def chat_completion(request: Request):
    # ═══ 階段 1：認證 ═══
    user_api_key_dict = await user_api_key_auth(
        request=request,
        api_key=request.headers.get("Authorization"),
    )
    # → 驗證 API Key，取得 user/team/budget 資訊
    
    # ═══ 階段 2：前置檢查 ═══
    # 檢查預算
    await max_budget_limiter.check(user_api_key_dict)
    # 檢查速率限制
    await parallel_request_limiter.check(user_api_key_dict)
    
    # ═══ 階段 3：路由到 LLM ═══
    response = await router.acompletion(
        model=request_data["model"],
        messages=request_data["messages"],
        **optional_params,
    )
    
    # ═══ 階段 4：後置處理 ═══
    # 記錄成本、觸發 callbacks、更新資料庫
    await logging_obj.async_success_handler(response)
    
    # ═══ 階段 5：回傳 ═══
    return response
```

## API 端點

Proxy 實作了完整的 OpenAI 相容 API：

| 端點 | 功能 |
|------|------|
| `POST /v1/chat/completions` | 聊天完成 |
| `POST /v1/completions` | 文字完成 |
| `POST /v1/embeddings` | 文字嵌入 |
| `POST /v1/images/generations` | 圖片生成 |
| `POST /v1/audio/transcriptions` | 語音轉文字 |
| `POST /v1/audio/speech` | 文字轉語音 |
| `POST /v1/moderations` | 內容審查 |
| `POST /v1/responses` | OpenAI Responses API |
| `GET /v1/models` | 列出可用模型 |
| `GET /health` | 健康檢查 |

客戶端使用範例：

```python
# 客戶端只需改 base_url，其餘程式碼不變
import openai

client = openai.OpenAI(
    api_key="sk-litellm-key-1234",     # LiteLLM 發放的 Key
    base_url="http://localhost:4000/v1" # 指向 Proxy
)

response = client.chat.completions.create(
    model="gpt-4",  # Proxy 會路由到正確的供應商
    messages=[{"role": "user", "content": "Hello"}]
)
```

## 認證系統

### API Key 管理

```yaml
# proxy_config.yaml
model_list:
  - model_name: gpt-4
    litellm_params:
      model: azure/gpt-4
      api_key: os.environ/AZURE_API_KEY
      api_base: https://my-resource.openai.azure.com/

general_settings:
  master_key: sk-master-key-1234  # 管理員 key
  database_url: postgresql://...   # 存儲 key/user/team 資料
```

### Key 層級

```
Master Key (管理員)
    │
    ├── Team Key (團隊)
    │       │
    │       ├── User Key (使用者)
    │       └── User Key
    │
    └── Team Key
            │
            └── User Key
```

每個層級都可以設定：
- **預算上限**：每月最多花費 $100
- **速率限制**：每分鐘最多 100 請求
- **模型存取**：只能使用 gpt-4 和 claude-3
- **有效期**：key 在某日期後過期

### 認證流程

```python
# proxy/auth/user_api_key_auth.py（概念化）

async def user_api_key_auth(request, api_key):
    # 1. 解析 Bearer token
    token = api_key.replace("Bearer ", "")
    
    # 2. 查快取（避免每次查 DB）
    key_info = await internal_cache.get_key_info(hash(token))
    
    if key_info is None:
        # 3. 快取未命中 → 查 PostgreSQL
        key_info = await prisma.litellm_verificationtoken.find_unique(
            where={"token": hash(token)}
        )
        if key_info is None:
            raise HTTPException(401, "Invalid API Key")
        
        # 4. 回填快取
        await internal_cache.set_key_info(hash(token), key_info)
    
    # 5. 檢查 key 是否過期
    if key_info.expires and key_info.expires < now():
        raise HTTPException(401, "API Key expired")
    
    # 6. 檢查 key 是否超預算
    if key_info.max_budget and key_info.spend >= key_info.max_budget:
        raise HTTPException(403, "Budget exceeded")
    
    return key_info
```

## 預算與限流

### 預算管理

```python
# proxy/hooks/max_budget_limiter.py

class MaxBudgetLimiter:
    async def check(self, user_api_key_dict):
        # 三層預算檢查
        # 1. Key 層級預算
        if key_spend >= key_max_budget:
            raise BudgetExceededError("API Key budget exceeded")
        
        # 2. User 層級預算
        if user_spend >= user_max_budget:
            raise BudgetExceededError("User budget exceeded")
        
        # 3. Team 層級預算
        if team_spend >= team_max_budget:
            raise BudgetExceededError("Team budget exceeded")
```

### 速率限制

```python
# proxy/hooks/parallel_request_limiter_v3.py

class ParallelRequestLimiter:
    async def check(self, user_api_key_dict):
        key = user_api_key_dict.token
        
        # 檢查 RPM (Requests Per Minute)
        current_rpm = await redis.get(f"rpm:{key}:{current_minute}")
        if current_rpm >= max_rpm:
            raise RateLimitError("RPM limit exceeded")
        
        # 檢查 TPM (Tokens Per Minute)
        current_tpm = await redis.get(f"tpm:{key}:{current_minute}")
        if current_tpm >= max_tpm:
            raise RateLimitError("TPM limit exceeded")
        
        # 通過 → 遞增計數器
        await redis.incr(f"rpm:{key}:{current_minute}")
```

## Proxy 配置

Proxy 通過 YAML 檔案配置：

```yaml
# proxy_config.yaml 完整範例

model_list:
  # 定義可用的模型和部署
  - model_name: gpt-4
    litellm_params:
      model: azure/gpt-4-turbo
      api_key: os.environ/AZURE_KEY
      api_base: https://eastus.openai.azure.com/
      rpm: 100
      tpm: 40000
  
  - model_name: gpt-4
    litellm_params:
      model: openai/gpt-4
      api_key: os.environ/OPENAI_KEY
      rpm: 500

  - model_name: claude-3
    litellm_params:
      model: anthropic/claude-3-opus-20240229
      api_key: os.environ/ANTHROPIC_KEY

router_settings:
  routing_strategy: latency-based-routing
  num_retries: 3
  timeout: 60
  fallbacks:
    - gpt-4: [claude-3]

general_settings:
  master_key: sk-master-key
  database_url: postgresql://user:pass@localhost/litellm
  
litellm_settings:
  drop_params: true
  success_callback: ["langfuse"]
```

## 啟動 Proxy

```bash
# 基本啟動
litellm --config proxy_config.yaml --port 4000

# 開發模式（熱重載）
litellm --config proxy_config.yaml --port 4000 --reload --detailed_debug

# Docker 部署
docker run -p 4000:4000 \
  -v ./proxy_config.yaml:/app/config.yaml \
  ghcr.io/berriai/litellm:latest \
  --config /app/config.yaml
```

## 管理 UI

Proxy 內建 Web UI（`/ui` 路徑），提供：
- API Key 管理介面
- 使用量儀表板
- 模型管理
- 日誌查看
- 團隊/使用者管理

## 學習重點

1. Proxy 是一個**完全相容 OpenAI API 的中間伺服器**
2. 客戶端只需改 `base_url`，不需要改任何邏輯
3. **三層認證**：Master Key → Team → User
4. **預算管理**也是三層：Key / User / Team
5. **速率限制**使用 Redis 計數器，支援 RPM 和 TPM
6. 所有配置集中在一個 YAML 檔案中

## SDK vs Proxy：我該用哪個？

| 考量因素 | 用 SDK 就好 | 需要 Proxy |
|---------|------------|-----------|
| 使用者數 | 就你一個人 | 團隊/多人 |
| API Key 管理 | 你自己管 | 需要集中發放和撤銷 |
| 預算控制 | 自己注意就好 | 需要強制限制、自動告警 |
| 可觀測性 | print 就夠 | 需要 Langfuse/Prometheus 追蹤 |
| 部署環境 | 本地 / Notebook | 生產伺服器 |
| 客戶端多樣性 | 只有 Python | 多語言（JS, Go, curl...） |

**經驗法則**：如果有超過 1 個人會用到 LLM API，就該考慮 Proxy。

## 五分鐘快速啟動

```bash
# 1. 安裝
pip install litellm[proxy]

# 2. 最小配置（建立 config.yaml）
cat > config.yaml << 'EOF'
model_list:
  - model_name: gpt-4o-mini
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY

general_settings:
  master_key: sk-my-master-key-1234
EOF

# 3. 啟動
export OPENAI_API_KEY="sk-your-key"
litellm --config config.yaml --port 4000

# 4. 測試（另一個終端）
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-my-master-key-1234" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hi"}]}'
```

---

[← 上一章：快取系統](./09-快取系統.md) | [下一章：可觀測性與日誌 →](./11-可觀測性與日誌.md)
