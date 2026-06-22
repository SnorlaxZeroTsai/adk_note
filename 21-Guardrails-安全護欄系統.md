# 第二十一章：Guardrails 安全護欄系統

> **本章目標**：理解 LiteLLM 的三階段安全攔截架構（pre/during/post），掌握 Prompt Injection 偵測、PII 遮罩、Tool 權限控制的實作方式。
>
> **預計閱讀時間**：30 分鐘
>
> **前置閱讀**：[第十章：Proxy Gateway 伺服器](./10-Proxy-Gateway伺服器.md)、[第十一章：可觀測性與日誌](./11-可觀測性與日誌.md)
>
> **你會學到**：CustomGuardrail 基礎類別、Lakera/Presidio/Tool Permission/Bedrock 四種實作、自定義護欄寫法

> 使用者可能發送惡意 prompt、洩漏個資、或讓 AI 呼叫危險工具。Guardrails 就是在 LLM 請求生命週期中攔截、審查、改寫的安全層。

---

## 一句話總結

Guardrails 是可插拔的安全 Hook——在請求**前中後**三個時機點執行，可以阻擋、改寫、或記錄請求/回應。

---

## 生命週期：什麼時候執行？

```
Client Request
    │
    ▼
┌─────────────────────────────────┐
│  PRE_CALL（請求前）              │ ← Prompt Injection 偵測、PII 遮罩
│  可以：修改 request / 阻擋請求   │
└─────────────┬───────────────────┘
              │
              ▼
┌─────────────────────────────────┐
│  LLM Provider 處理中             │
│                                  │
│  DURING_CALL（平行執行）         │ ← 非同步審查（不阻塞回應）
│  可以：記錄 / 事後阻擋           │
└─────────────┬───────────────────┘
              │
              ▼
┌─────────────────────────────────┐
│  POST_CALL（回應後）             │ ← 輸出 PII 遮罩、Tool 權限檢查
│  可以：修改 response / 阻擋回應  │
└─────────────┬───────────────────┘
              │
              ▼
Client Response
```

| Hook | 時機 | 能做什麼 | 典型用途 |
|------|------|---------|---------|
| `pre_call` | LLM 呼叫前 | 修改/阻擋 request | Prompt injection、輸入 PII 遮罩 |
| `during_call` | 與 LLM 平行 | 非同步審查 | 背景 moderation |
| `post_call` | 收到回應後 | 修改/阻擋 response | 輸出 PII 遮罩、Tool 權限 |
| `logging_only` | 任何時候 | 只記錄不阻擋 | 合規日誌 |

---

## 基礎類別：CustomGuardrail

所有 Guardrail 都繼承自 `CustomGuardrail`：

```python
class CustomGuardrail(CustomLogger):
    def __init__(
        self,
        guardrail_name: str,
        event_hook: Union[str, List[str]],  # "pre_call", "post_call", etc.
        default_on: bool = False,           # 是否對所有請求生效
        on_violation: Optional[str] = None, # "warn" or "end_session"
        on_sensitive_data: Optional[str] = None,  # "block" or "route"
        sensitive_data_route_to_model: Optional[str] = None,
        ...
    ):

    # 必須實作的方法
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """請求前攔截"""
        ...

    async def async_moderation_hook(self, data, user_api_key_dict, call_type):
        """平行審查"""
        ...

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        """回應後處理"""
        ...

    # 判斷是否該執行
    def should_run_guardrail(self, data, event_type) -> bool:
        """根據 default_on、request metadata、opt-out 決定是否執行"""
        ...
```

### 控制是否執行

```python
# 全域預設開啟
default_on: true  # 所有請求都跑這個 guardrail

# 請求層級開啟特定 guardrail
data["metadata"]["guardrails"] = ["presidio-pii", "tool-permission"]

# 請求層級關閉全域 guardrail
data["metadata"]["disable_global_guardrails"] = True

# 排除特定 guardrail
data["metadata"]["opted_out_global_guardrails"] = ["lakera"]
```

---

## 內建實作

### 1. Lakera AI — Prompt Injection 偵測

**用途**：偵測使用者是否試圖注入惡意指令或繞過系統 prompt。

```yaml
guardrails:
  - guardrail_name: "lakera-guard"
    litellm_params:
      guardrail: "lakera"
      mode: "pre_call"
      api_key: "os.environ/LAKERA_API_KEY"
      category_thresholds:
        prompt_injection: 0.8
        jailbreak: 0.7
```

**流程**：

```
使用者訊息
  → 提取所有 message content
  → POST https://api.lakera.ai/v1/prompt_injection
  → 回應: { flagged: bool, category_scores: {...} }
  → 分數 > threshold → raise HTTPException(400)
  → 分數 < threshold → 放行
```

**為什麼需要外部 API？**

Prompt injection 偵測需要理解語意（不是簡單的 regex），Lakera 用專門訓練的模型做偵測，準確率遠高於規則式。

---

### 2. Presidio — PII 偵測與遮罩

**用途**：偵測並遮罩個人識別資訊（姓名、信用卡號、Email 等）。

```yaml
guardrails:
  - guardrail_name: "presidio-pii"
    litellm_params:
      guardrail: "presidio"
      mode: ["pre_call", "post_call"]
      pii_entities_config:
        EMAIL_ADDRESS: "MASK"     # 遮罩：john@example.com → <EMAIL_ADDRESS>
        CREDIT_CARD: "BLOCK"      # 阻擋：直接拒絕請求
        PERSON: "MASK"
      presidio_score_thresholds:
        CREDIT_CARD: 0.8          # 信心分數門檻
      presidio_analyzer_api_base: "http://presidio-analyzer:8000"
      presidio_anonymizer_api_base: "http://presidio-anonymizer:8001"
      output_parse_pii: true      # 回應中還原 PII（給合法使用者看）
```

**流程（pre_call）**：

```
"請幫我查 john@example.com 的訂單"
  → POST /analyze → 偵測到 EMAIL_ADDRESS (score: 0.95)
  → 動作 = MASK → POST /anonymize
  → "請幫我查 <EMAIL_ADDRESS_1> 的訂單"
  → 儲存 mapping: {"<EMAIL_ADDRESS_1>": "john@example.com"} 到 metadata
  → 送給 LLM

LLM 回應: "已查到 <EMAIL_ADDRESS_1> 的訂單..."
  → POST_CALL: 用 mapping 還原
  → "已查到 john@example.com 的訂單..."
  → 回傳給使用者
```

**兩種動作**：

| 動作 | 行為 | 適用 |
|------|------|------|
| `MASK` | 替換為 token，事後可還原 | Email、姓名（LLM 不需要知道真實值） |
| `BLOCK` | 直接拒絕整個請求 | 信用卡號（絕對不該出現） |

**串流處理**：

Presidio 需要完整文字才能偵測 PII，但串流是逐 chunk 來的。解法：

```python
# 策略：buffer 所有 chunk → 重組 → PII 處理 → 重新串流
async def async_post_call_streaming_iterator_hook(self, response, ...):
    chunks = []
    async for chunk in response:
        chunks.append(chunk)

    assembled = stream_chunk_builder(chunks)      # 重組完整回應
    await self._unmask_pii(assembled)             # PII 還原
    async for chunk in re_stream(assembled):      # 重新串流
        yield chunk
```

---

### 3. Tool Permission — 工具權限控制

**用途**：限制 LLM 能呼叫哪些工具、用什麼參數。

```yaml
guardrails:
  - guardrail_name: "tool-permission"
    litellm_params:
      guardrail: "tool_permission"
      mode: ["pre_call", "post_call"]
      default_action: "deny"        # 預設拒絕所有工具
      on_disallowed_action: "block"  # 或 "rewrite"（移除工具但不報錯）
      rules:
        - id: "allow-read"
          tool_name: "^(read|get)_.*"    # regex
          decision: "allow"
        - id: "deny-delete"
          tool_name: "^delete_.*"
          decision: "deny"
        - id: "restrict-params"
          tool_name: "update_user"
          decision: "allow"
          allowed_param_patterns:
            user_id: "^[0-9]+$"          # 參數值必須是數字
```

**兩階段檢查**：

```
PRE_CALL（請求前）：
  使用者提供 tools=[read_file, delete_file, update_user]
    → read_file: 匹配 allow-read → 保留
    → delete_file: 匹配 deny-delete → 移除
    → update_user: 允許，但限制參數
  → 修改後的 tools=[read_file, update_user] 送給 LLM

POST_CALL（回應後）：
  LLM 回應 tool_calls=[update_user(user_id="abc")]
    → user_id="abc" 不匹配 "^[0-9]+$" → 阻擋！
    → raise GuardrailRaisedException
```

**`block` vs `rewrite`**：

| 模式 | 行為 |
|------|------|
| `block` | 拒絕請求，回 400 |
| `rewrite` | 靜默移除不允許的工具/tool_call，繼續處理 |

---

### 4. Bedrock Guardrails — AWS 原生安全

**用途**：使用 AWS Bedrock 的內建 guardrail（內容安全、PII、接地性檢查）。

```yaml
guardrails:
  - guardrail_name: "bedrock-safety"
    litellm_params:
      guardrail: "bedrock"
      mode: ["pre_call", "post_call"]
      guardrailIdentifier: "my-guardrail-id"
      guardrailVersion: "1"
```

**特殊功能：Contextual Grounding（接地性檢查）**

檢查 LLM 回應是否忠實於提供的來源資料：

```python
# 標記來源資料
messages = [
    {"role": "user", "content": [
        {"type": "text", "text": "來源文件...", "guardrailConfig": {"tagSuffix": "grounding_source"}},
        {"type": "text", "text": "使用者問題", "guardrailConfig": {"tagSuffix": "query"}},
    ]}
]
# Bedrock 會比對回應是否「接地」於 grounding_source
```

---

## 錯誤處理機制

### 阻擋時拋出什麼？

```python
# 1. 一般阻擋
raise HTTPException(status_code=400, detail={
    "error": "Violated guardrail policy",
    "guardrail_name": "lakera-guard",
    "detection_message": "Prompt injection detected",
})

# 2. PII 被阻擋
raise BlockedPiiEntityError(
    entity_type="CREDIT_CARD",
    guardrail_name="presidio-pii",
)

# 3. 敏感資料重路由（不阻擋，改路線）
raise SensitiveDataRouteException(
    route_to_model="on-premise-llm",  # 轉到本地模型
    guardrail_name="presidio-pii",
    sticky_session_routing=True,       # 後續請求也走這條路
)

# 4. 修改回應（不報錯，改內容）
raise ModifyResponseException(
    message="I cannot help with that request.",
    guardrail_name="tool-permission",
)
```

### 異常分類

```python
guardrail_status = (
    "guardrail_intervened"         # 正常攔截（使用者做了不該做的事）
    if is_guardrail_intervention(e)
    else "guardrail_failed_to_respond"  # Guardrail 自身故障
)
```

故障 ≠ 攔截。Guardrail API 掛了是故障，偵測到 injection 是攔截——兩者要分開追蹤。

---

## 敏感資料路由（Sensitive Data Routing）

不只是阻擋，還能**改路線**：

```
使用者發送含 PII 的請求
  → Presidio 偵測到 PERSON entity
  → on_sensitive_data = "route"
  → raise SensitiveDataRouteException(route_to_model="on-premise-llm")
  → Router 將請求改路到本地部署的模型（資料不出公司網路）
  → sticky_session_routing=True → 同一 session 後續都走本地
```

適用場景：醫療、金融，某些資料不能送到外部 API。

---

## 設定方式

### YAML 設定（靜態）

```yaml
guardrails:
  - guardrail_name: "my-guardrail"
    litellm_params:
      guardrail: "lakera"          # 供應商名稱
      mode: "pre_call"             # 或 ["pre_call", "post_call"]
      api_key: "os.environ/KEY"    # 可引用環境變數
      default_on: true             # 所有請求都執行
      category_thresholds:
        prompt_injection: 0.8
```

### 資料庫設定（動態）

```
GuardrailsRepository (DB)
  - guardrail_id (PK)
  - guardrail_name
  - litellm_params (JSON)
  - status: "active" | "pending_review" | "rejected"
```

Proxy 定期從 DB 同步設定，支援不重啟更新。

### 請求層級控制

```python
# 只對這個請求啟用特定 guardrail
response = litellm.completion(
    model="gpt-4o",
    messages=[...],
    metadata={
        "guardrails": ["presidio-pii", "tool-permission"],
    }
)

# 帶動態參數
metadata={
    "guardrails": [
        {"lakera": {"extra_body": {"success_threshold": 0.9}}}
    ]
}
```

---

## 執行管線內部細節

```python
# proxy/utils.py - pre_call_hook 簡化版
async def pre_call_hook(self, data, user_api_key_dict, call_type):
    for callback in resolved_callbacks:
        if not isinstance(callback, CustomGuardrail):
            continue

        # 1. 是否該執行？
        if not callback.should_run_guardrail(data, "pre_call"):
            continue

        # 2. 執行 guardrail
        try:
            result = await callback.async_pre_call_hook(
                user_api_key_dict=user_api_key_dict,
                cache=cache,
                data=data,
                call_type=call_type,
            )
            if result is not None:
                data = result  # Guardrail 修改了 request

        except SensitiveDataRouteException as e:
            deferred_route = e  # 暫存，等所有 guardrail 跑完再處理
            continue

        except (GuardrailRaisedException, HTTPException):
            raise  # 直接阻擋

        # 3. 標記已執行（防止重複）
        callback.mark_pre_call_hook_ran(data)

    # 所有 guardrail 跑完，處理延遲路由
    if deferred_route:
        raise deferred_route

    return data
```

---

## 自定義 Guardrail 範例

```python
from litellm.integrations.custom_guardrail import CustomGuardrail

class ProfanityFilter(CustomGuardrail):
    def __init__(self, **kwargs):
        super().__init__(
            guardrail_name="profanity-filter",
            supported_event_hooks=["pre_call"],
            **kwargs,
        )
        self.bad_words = {"damn", "hell", "..."}

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        messages = data.get("messages", [])
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                words = set(content.lower().split())
                if words & self.bad_words:
                    raise HTTPException(
                        status_code=400,
                        detail={"error": "Profanity detected"}
                    )
        return data
```

---

## 設計亮點

| 設計 | 效果 |
|------|------|
| Hook 三階段（前/中/後） | 不同安全需求在最佳時機執行 |
| `default_on` + opt-out | 全域保護，個別請求可豁免 |
| `block` vs `route` vs `rewrite` | 不只是阻擋，還能改路線或靜默修改 |
| Guardrail 故障 ≠ 攔截 | 分開追蹤，不因 guardrail 自身問題拒絕合法請求 |
| 串流 buffer+re-stream | 解決 PII 偵測需要完整文字 vs 串流逐 chunk 的矛盾 |
| DB 動態設定 | 不重啟即可更新規則 |
| 請求層級覆寫 | 同一系統不同 API key 有不同安全等級 |

---

## 源碼位置

| 檔案 | 內容 |
|------|------|
| `litellm/integrations/custom_guardrail.py` | 基礎類別 |
| `litellm/proxy/guardrails/guardrail_registry.py` | 註冊與 DB 同步 |
| `litellm/proxy/guardrails/guardrail_initializers.py` | 各供應商初始化 |
| `litellm/proxy/guardrails/guardrail_hooks/lakera_ai.py` | Lakera 實作 |
| `litellm/proxy/guardrails/guardrail_hooks/presidio.py` | Presidio 實作 |
| `litellm/proxy/guardrails/guardrail_hooks/tool_permission.py` | Tool 權限 |
| `litellm/proxy/guardrails/guardrail_hooks/bedrock_guardrails.py` | AWS Bedrock |
| `litellm/proxy/utils.py` | 執行管線（pre/during/post_call_hook） |
| `litellm/types/guardrails.py` | 型別定義 |

---

[← 上一章：User Budget 查詢與管理](./20-LiteLLM-User-Budget-查詢與管理.md) | [下一章：進階路由策略 →](./22-進階路由策略.md)

*最後更新：2026-06-22*
