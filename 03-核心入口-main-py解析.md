# 第三章：核心入口 main.py 解析

> **本章目標**：理解 LiteLLM 最核心的函式 `completion()` 是如何工作的。讀完後你能看懂它的源碼結構。
>
> **預計閱讀時間**：25 分鐘
>
> **前置閱讀**：[第二章：架構全景圖](./02-架構全景圖.md)
>
> **核心概念**：本章涉及「函式如何接收參數並分派給不同處理器」的模式。如果你寫過 API 路由（如 Flask/FastAPI 的 endpoint），這個概念會很熟悉。

## 概述

`litellm/main.py` 是整個 LiteLLM 的心臟。它定義了使用者直接呼叫的核心函式：`completion()` 和 `acompletion()`。

> **給初學者的定位**：如果 LiteLLM 是一家餐廳，`main.py` 就是「大廳經理」——所有客人的需求都先經過它，由它決定分派給哪個廚房（供應商）處理。

## completion() 函式的結構

`completion()` 是一個約 3000+ 行的巨型函式（這是歷史遺留的設計，不是最佳實踐）。它的邏輯可以分為五個階段：

> **初學者注意**：3000 行的函式在正常開發中是「反模式」（anti-pattern）。LiteLLM 這樣做是因為歷史演進——最初很小，隨著供應商增加逐漸膨脹。他們正在慢慢重構到新架構。這是真實開源專案的常態：理想與現實的妥協。

```
┌─────────────────────────────────────────────┐
│ 階段 1：參數驗證與解析                       │
│ - 驗證 messages 格式                         │
│ - 驗證 tools、tool_choice                    │
│ - 解析 kwargs 中的各種配置                   │
├─────────────────────────────────────────────┤
│ 階段 2：供應商識別                           │
│ - get_llm_provider(model) → provider 名稱   │
│ - 根據 model 字串前綴判斷供應商              │
├─────────────────────────────────────────────┤
│ 階段 3：參數轉換                             │
│ - get_optional_params() → 供應商特定參數     │
│ - 移除該供應商不支援的參數                   │
├─────────────────────────────────────────────┤
│ 階段 4：路由到具體供應商處理器                │
│ - 大量的 if/elif 分支                        │
│ - 呼叫對應的 Handler 或 HTTPHandler          │
├─────────────────────────────────────────────┤
│ 階段 5：回應處理與日誌                       │
│ - 統一回應格式為 ModelResponse               │
│ - 觸發 success/failure callbacks             │
│ - 計算成本                                   │
└─────────────────────────────────────────────┘
```

## 階段 1：參數驗證

```python
# main.py:1188-1199（簡化版）
def completion(model: str, messages: List = [], **kwargs):
    # 驗證 model 不為空
    if model is None:
        raise ValueError("model param not passed in.")
    
    # 驗證並修正 messages 格式
    messages = validate_and_fix_openai_messages(messages=messages)
    
    # 驗證 tools 格式
    tools = validate_and_fix_openai_tools(tools=tools)
    
    # 驗證 tool_choice
    tool_choice = validate_chat_completion_tool_choice(tool_choice=tool_choice)
```

**設計意圖**：LiteLLM 採取「寬容接收」策略——即使使用者傳入格式稍有偏差的資料，也會嘗試自動修正，而非直接報錯。

## 階段 2：供應商識別

LiteLLM 使用 model 字串的前綴來識別供應商：

```python
# 模型命名慣例
"gpt-4"                    → OpenAI（預設）
"claude-3-opus-20240229"   → Anthropic
"anthropic/claude-3-opus"  → Anthropic（明確指定）
"bedrock/anthropic.claude" → AWS Bedrock
"vertex_ai/gemini-pro"     → Google Vertex AI
"ollama/llama2"            → Ollama（本地）
```

核心函式 `get_llm_provider()` 負責這個解析：

```python
# utils.py 中的關鍵邏輯（概念化）
def get_llm_provider(model: str, custom_llm_provider=None, api_base=None):
    """
    回傳: (model_name, provider, api_base, api_key)
    
    例如:
      "anthropic/claude-3" → ("claude-3", "anthropic", None, <key>)
      "bedrock/anthropic.claude-v2" → ("anthropic.claude-v2", "bedrock", None, <key>)
    """
    # 1. 如果明確指定 custom_llm_provider，直接使用
    if custom_llm_provider:
        return model, custom_llm_provider, api_base, api_key
    
    # 2. 檢查 model 字串是否有 "/" 前綴
    if "/" in model:
        provider = model.split("/")[0]
        model_name = "/".join(model.split("/")[1:])
        return model_name, provider, api_base, api_key
    
    # 3. 根據已知模型名稱推斷供應商
    if model in litellm.openai_compatible_models:
        return model, "openai", api_base, api_key
    # ... 更多推斷邏輯
```

## 階段 3：參數轉換

不同供應商支援不同的參數。例如：
- OpenAI 支援 `frequency_penalty`，但 Anthropic 不支援
- Anthropic 有 `thinking` 參數，OpenAI 沒有

`get_optional_params()` 負責過濾和轉換：

```python
# utils.py（概念化）
def get_optional_params(
    model: str,
    custom_llm_provider: str,
    temperature=None,
    max_tokens=None,
    stream=None,
    # ... 更多參數
):
    """
    只保留目標供應商支援的參數。
    如果 litellm.drop_params=True，靜默丟棄不支援的參數。
    否則，對不支援的參數拋出異常。
    """
    optional_params = {}
    
    # 根據供應商過濾參數
    supported = get_supported_openai_params(model, custom_llm_provider)
    
    for param_name, param_value in provided_params.items():
        if param_value is not None:
            if param_name in supported:
                optional_params[param_name] = param_value
            elif litellm.drop_params:
                pass  # 靜默丟棄
            else:
                raise UnsupportedParamsError(...)
    
    return optional_params
```

## 階段 4：路由到供應商處理器

這是 `completion()` 中最長的部分——一個巨大的 if/elif 鏈：

```python
# main.py（簡化版，展示核心邏輯模式）
try:
    # 根據供應商分派
    if custom_llm_provider == "openai":
        # 新式供應商：使用統一的 BaseLLMHTTPHandler
        response = base_llm_http_handler.completion(
            model=model,
            messages=messages,
            model_response=model_response,
            optional_params=optional_params,
            litellm_params=litellm_params,
            logging_obj=logging,
            provider_config=ProviderConfigManager.get_provider_chat_config(
                model=model, provider=LlmProviders.OPENAI
            ),
        )
    
    elif custom_llm_provider == "anthropic":
        # 也使用統一的 handler
        response = base_llm_http_handler.completion(
            model=model,
            messages=messages,
            provider_config=ProviderConfigManager.get_provider_chat_config(
                model=model, provider=LlmProviders.ANTHROPIC
            ),
            # ...
        )
    
    elif custom_llm_provider == "bedrock":
        # Bedrock 有自己的特殊 handler（歷史原因）
        response = bedrock_converse_chat_completion.completion(
            model=model,
            messages=messages,
            # ...
        )
    
    # ... 更多 elif 分支（100+ 個供應商）
```

### 新式 vs 舊式供應商

LiteLLM 正在遷移到統一架構：

| 模式 | 呼叫方式 | 代表供應商 |
|------|---------|-----------|
| 新式 | `base_llm_http_handler.completion(provider_config=...)` | OpenAI, Anthropic, Gemini |
| 舊式 | `specific_handler.completion(...)` | Bedrock, SageMaker |

新式架構的優勢：所有供應商共用同一個 HTTP 處理流程，差異只在 `provider_config`（即 `BaseConfig` 的實作）。

## 階段 5：回應處理

```python
    # completion() 的最後部分
    
    # 串流回應：包裝為 CustomStreamWrapper
    if stream and isinstance(response, CustomStreamWrapper):
        return response
    
    # 非串流回應：確保格式統一
    if isinstance(response, ModelResponse):
        # 已經是正確格式
        return response
    
    # 字典回應：轉換為 ModelResponse
    response = convert_to_model_response_object(response)
    return response

except Exception as e:
    # 統一錯誤映射
    raise exception_type(
        model=model,
        custom_llm_provider=custom_llm_provider,
        original_exception=e,
    )
```

## acompletion() — 非同步版本

`acompletion()` 是 `completion()` 的非同步包裝：

```python
async def acompletion(model: str, messages: List = [], **kwargs):
    # 1. 設定 acompletion=True 標記
    completion_kwargs["acompletion"] = True
    
    # 2. 處理 fallbacks
    if fallbacks:
        return await async_completion_with_fallbacks(**kwargs)
    
    # 3. 在 executor 中呼叫同步的 completion()
    func = partial(completion, **completion_kwargs, **kwargs)
    ctx = contextvars.copy_context()
    func_with_context = partial(ctx.run, func)
    init_response = await loop.run_in_executor(None, func_with_context)
    
    # 4. 如果 completion() 回傳了 coroutine，await 它
    if asyncio.iscoroutine(init_response):
        response = await init_response
    else:
        response = init_response
    
    return response
```

**關鍵洞察**：`acompletion()` 並不是完全獨立的非同步實作。它將同步的 `completion()` 放到 thread executor 中執行。當供應商的 handler 內部偵測到 `acompletion=True` 時，會回傳一個 coroutine 而非直接結果，再由 `acompletion()` 來 await。

## @client 裝飾器

你會注意到 `completion()` 和 `acompletion()` 都有 `@client` 裝飾器：

```python
@client
def completion(...):
    ...

@client
async def acompletion(...):
    ...
```

這個裝飾器來自 `litellm/__init__.py`，負責：
1. **建立 Logging 物件**：記錄請求的開始時間
2. **觸發 pre-call callbacks**：在呼叫前執行 `input_callback`
3. **觸發 post-call callbacks**：在呼叫後執行 `success_callback` 或 `failure_callback`
4. **快取處理**：檢查快取中是否有相同請求的回應
5. **成本計算**：計算本次呼叫的 token 花費

## ModelResponse — 統一回應格式

所有供應商的回應最終都被轉換為 `ModelResponse`：

```python
# types/utils.py
class ModelResponse(BaseModel):
    id: str                    # 回應唯一 ID
    choices: List[Choices]     # 生成的選項
    created: int               # 時間戳
    model: str                 # 模型名稱
    usage: Usage               # Token 用量
    system_fingerprint: Optional[str]
    
class Choices(BaseModel):
    finish_reason: str         # "stop", "length", "tool_calls" 等
    index: int                 # 選項索引
    message: Message           # 生成的訊息
    
class Message(BaseModel):
    role: str                  # "assistant"
    content: Optional[str]     # 文字內容
    tool_calls: Optional[List] # 工具呼叫
    
class Usage(BaseModel):
    prompt_tokens: int         # 輸入 token 數
    completion_tokens: int     # 輸出 token 數
    total_tokens: int          # 總 token 數
```

這個格式完全相容 OpenAI SDK 的 `ChatCompletion` 物件，讓使用者可以無縫切換。

## 學習重點

1. **入口函式的職責**：驗證 → 識別 → 轉換 → 分派 → 格式化
2. **前綴命名慣例**：`provider/model` 是 LiteLLM 的核心約定
3. **寬容接收、嚴格輸出**：接受各種格式的輸入，但輸出始終統一
4. **裝飾器的強大用途**：`@client` 在不修改核心邏輯的情況下加入橫切關注點
5. **遷移中的架構**：新舊模式並存，新供應商應使用 `base_llm_http_handler`

## 常見新手疑問

<details>
<summary>Q：為什麼不把 completion() 拆成多個小函式？</summary>

歷史原因。LiteLLM 最初只支援幾個供應商，一個函式就夠了。隨著增長到 100+，重構的工程量和破壞性太大，所以採取「漸進遷移」策略——新的供應商使用新架構（`base_llm_http_handler`），舊的逐步搬遷。這在真實開源專案中很常見。

</details>

<details>
<summary>Q：acompletion() 為什麼不是完全獨立的非同步實作？</summary>

因為要維護兩份完全相同的邏輯（同步 + 非同步）成本太高。所以 LiteLLM 的策略是：讓 `completion()` 做所有準備工作，到最後發送 HTTP 請求時才分叉——同步的直接發送，非同步的回傳 coroutine 讓 `acompletion()` 去 await。這樣轉換邏輯只寫一次。

</details>

<details>
<summary>Q：@client 裝飾器是什麼？我需要會寫裝飾器嗎？</summary>

裝飾器是 Python 的語法糖，讓你能在不修改函式本體的情況下「包裹」額外功能。`@client` 加入了日誌、快取、成本計算等功能。你不需要會寫裝飾器來理解 LiteLLM，但理解它的概念（「在函式前後做事」）會很有幫助。如果不熟悉，建議搜尋「Python decorator tutorial」花 15 分鐘學習。

</details>

## 動手練習

打開 LiteLLM 的源碼 `litellm/main.py`，嘗試：

1. 搜尋 `def completion(`，找到函式開頭
2. 搜尋 `custom_llm_provider == "anthropic"`，看看 Anthropic 是怎麼被路由的
3. 搜尋 `base_llm_http_handler.completion`，看看新式供應商的呼叫方式

你不需要看懂每一行——目標是**驗證本章描述的五個階段確實存在**。

---

[← 上一章：架構全景圖](./02-架構全景圖.md) | [下一章：Provider 抽象層 →](./04-Provider抽象層.md)
