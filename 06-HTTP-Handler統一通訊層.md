# 第六章：HTTP Handler 統一通訊層

> **本章目標**：理解 LiteLLM 如何統一管理所有 HTTP 請求的發送、連接池、超時和錯誤處理。
>
> **預計閱讀時間**：20 分鐘
>
> **前置知識**：HTTP 基礎（POST 請求、Header、Status Code）。如果你用過 `requests` 或 `fetch`，就夠了。
>
> **本章的核心 insight**：Provider Config 負責「做什麼」（資料怎麼轉換），HTTP Handler 負責「怎麼做」（網路怎麼通訊）。這種分離讓新增供應商不需要寫任何 HTTP 程式碼。

## 為什麼需要統一通訊層？

所有 LLM 供應商最終都是透過 HTTP API 通訊的。與其讓每個供應商自行處理 HTTP 細節，LiteLLM 提供了 `BaseLLMHTTPHandler` 作為統一的通訊層。

```
Provider Config (轉換邏輯)    BaseLLMHTTPHandler (通訊邏輯)
─────────────────────────    ──────────────────────────────
"怎麼轉換資料格式"            "怎麼發送和接收 HTTP 請求"
```

## BaseLLMHTTPHandler 的核心流程

```python
# litellm/llms/custom_httpx/llm_http_handler.py

class BaseLLMHTTPHandler:
    def completion(
        self,
        model: str,
        messages: list,
        api_base: Optional[str],
        model_response: ModelResponse,
        logging_obj: LiteLLMLoggingObj,
        optional_params: dict,
        timeout: Union[float, httpx.Timeout],
        litellm_params: dict,
        acompletion: bool,
        stream: Optional[bool] = False,
        api_key: Optional[str] = None,
        headers: Optional[dict] = None,
        provider_config: Optional[BaseConfig] = None,
    ):
        # === 步驟 1：取得 Provider Config ===
        provider_config = provider_config or ProviderConfigManager.get_provider_chat_config(
            model=model, provider=custom_llm_provider
        )
        
        # === 步驟 2：驗證環境並取得認證 headers ===
        headers = provider_config.validate_environment(
            api_key=api_key,
            headers=headers or {},
            model=model,
            messages=messages,
            optional_params=optional_params,
        )
        
        # === 步驟 3：組合完整的 API URL ===
        api_base = provider_config.get_complete_url(
            api_base=api_base,
            model=model,
            optional_params=optional_params,
            stream=stream,
        )
        
        # === 步驟 4：轉換請求資料 ===
        data = provider_config.transform_request(
            model=model,
            messages=messages,
            optional_params=optional_params,
            litellm_params=litellm_params,
            headers=headers,
        )
        
        # === 步驟 5：簽名（如 AWS Bedrock 需要 SigV4） ===
        headers, signed_body = provider_config.sign_request(
            headers=headers,
            request_data=data,
            api_base=api_base,
        )
        
        # === 步驟 6：記錄 pre-call 日誌 ===
        logging_obj.pre_call(
            input=messages,
            api_key=api_key,
            additional_args={"complete_input_dict": data, "api_base": api_base},
        )
        
        # === 步驟 7：分流 — 同步/非同步 × 串流/非串流 ===
        if acompletion:
            if stream:
                return self.acompletion_stream_function(...)
            else:
                return self.async_completion(...)
        else:
            if stream:
                return self.completion_stream_function(...)
            else:
                return self.sync_completion(...)
```

## 四種呼叫模式

```
                    同步 (Sync)              非同步 (Async)
                 ────────────────         ──────────────────
  非串流         sync_completion()         async_completion()
  (Non-stream)   回傳 ModelResponse        回傳 coroutine
                                           
  串流           completion_stream()       acompletion_stream()
  (Stream)       回傳 Iterator             回傳 AsyncIterator
```

### 非同步非串流呼叫

```python
async def async_completion(self, ...):
    # 發送 HTTP POST 請求
    response = await self._make_common_async_call(
        async_httpx_client=client,
        provider_config=provider_config,
        api_base=api_base,
        headers=headers,
        data=data,
        timeout=timeout,
    )
    
    # 轉換回應
    return provider_config.transform_response(
        model=model,
        raw_response=response,
        model_response=model_response,
        logging_obj=logging_obj,
    )
```

### 非同步串流呼叫

```python
async def acompletion_stream_function(self, ...):
    # 發送串流請求
    response = await self._make_common_async_call(
        ..., stream=True  # 關鍵：stream=True
    )
    
    # 包裝為串流迭代器
    streaming_response = CustomStreamWrapper(
        completion_stream=response.aiter_lines(),
        model=model,
        custom_llm_provider=custom_llm_provider,
        logging_obj=logging_obj,
    )
    
    return streaming_response
```

## HTTP 錯誤處理

`_make_common_async_call` 包含統一的錯誤處理邏輯：

```python
async def _make_common_async_call(self, ...):
    max_retry = provider_config.max_retry_on_unprocessable_entity_error
    
    for i in range(max(max_retry, 1)):
        try:
            response = await async_httpx_client.post(
                url=api_base,
                headers=headers,
                data=json.dumps(data),
                timeout=timeout,
                stream=stream,
            )
            return response
            
        except httpx.HTTPStatusError as e:
            # 某些錯誤可以重試（例如 422 可能是參數問題）
            should_retry = provider_config.should_retry_llm_api_inside_llm_translation_on_http_error(e)
            
            if should_retry and i < max_retry - 1:
                # 讓 provider_config 修改請求後重試
                data = provider_config.transform_request_on_unprocessable_entity_error(
                    e=e, request_data=data
                )
                continue
            else:
                # 轉換為 LiteLLM 統一錯誤格式
                raise self._handle_error(e=e, provider_config=provider_config)
```

### 錯誤映射

HTTP 狀態碼會被映射為 LiteLLM 的標準異常：

| HTTP Status | LiteLLM Exception | 含義 |
|-------------|-------------------|------|
| 400 | `BadRequestError` | 請求格式錯誤 |
| 401 | `AuthenticationError` | API Key 無效 |
| 403 | `PermissionDeniedError` | 無權限 |
| 404 | `NotFoundError` | 模型或端點不存在 |
| 408 | `Timeout` | 請求超時 |
| 429 | `RateLimitError` | 超過速率限制 |
| 500 | `InternalServerError` | 供應商內部錯誤 |
| 503 | `ServiceUnavailableError` | 服務暫時不可用 |

## HTTPHandler 和 AsyncHTTPHandler

底層的 HTTP 客戶端封裝：

```python
# litellm/llms/custom_httpx/http_handler.py

class HTTPHandler:
    """同步 HTTP 客戶端，基於 httpx.Client"""
    
    def __init__(self, timeout=600, concurrent_limit=1000):
        self.client = httpx.Client(
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(
                max_connections=concurrent_limit,
                max_keepalive_connections=100,
            ),
        )
    
    def post(self, url, headers, data, timeout, stream=False):
        response = self.client.post(
            url=url,
            headers=headers,
            content=data,
            timeout=timeout,
        )
        response.raise_for_status()
        return response


class AsyncHTTPHandler:
    """非同步 HTTP 客戶端，基於 httpx.AsyncClient"""
    
    def __init__(self, timeout=600, concurrent_limit=1000):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(
                max_connections=concurrent_limit,
                max_keepalive_connections=100,
            ),
        )
    
    async def post(self, url, headers, data, timeout, stream=False):
        response = await self.client.post(
            url=url,
            headers=headers,
            content=data,
            timeout=timeout,
        )
        response.raise_for_status()
        return response
```

### 連接池管理

`httpx` 內建了 HTTP/2 支援和連接池，LiteLLM 通過共享 client 實例來複用連接：

```python
# 全域共享的 HTTP client（避免每次請求都建立新連接）
def _get_httpx_client(params):
    """取得或建立共享的 httpx client"""
    # 使用 LRU cache 確保同參數配置共用同一個 client
    return HTTPHandler(...)

def get_async_httpx_client(params):
    """取得或建立共享的 async httpx client"""
    return AsyncHTTPHandler(...)
```

## 簽名機制

某些供應商（如 AWS Bedrock）需要請求簽名：

```python
# provider_config.sign_request() 的作用
# 大多數供應商：直接回傳原始 headers
# AWS Bedrock：使用 SigV4 簽名
# Google Vertex AI：使用 OAuth2 token

class BedrockConfig(BaseConfig):
    def sign_request(self, headers, request_data, api_base, ...):
        """使用 AWS SigV4 對請求進行簽名"""
        # 計算 SHA256 hash
        # 加入 AWS 認證 headers
        signed_headers = aws_sign_v4(
            method="POST",
            url=api_base,
            headers=headers,
            body=json.dumps(request_data),
            region=self.aws_region,
            service="bedrock-runtime",
        )
        return signed_headers, None
```

## Fake Stream（模擬串流）

某些供應商不原生支援串流，LiteLLM 會「模擬」串流：

```python
def should_fake_stream(self, model, custom_llm_provider, stream):
    """某些模型不支援真正的串流，需要模擬"""
    # 例如某些 Vertex AI 模型
    return model in self.non_streaming_models

# 模擬方式：
# 1. 發送非串流請求，取得完整回應
# 2. 將回應切割成 chunks
# 3. 逐個 yield，模擬串流效果
```

## 設計精髓

### 關注點分離

```
┌────────────────────┐     ┌─────────────────────┐
│   Provider Config   │     │  BaseLLMHTTPHandler  │
│                    │     │                     │
│ • 資料格式轉換      │     │ • HTTP 連接管理       │
│ • URL 組合         │     │ • 超時處理           │
│ • 認證邏輯         │     │ • 錯誤重試           │
│ • 參數映射         │     │ • 串流管理           │
│                    │     │ • 日誌記錄           │
└────────────────────┘     └─────────────────────┘
      "What"                      "How"
   （做什麼轉換）              （怎麼發送請求）
```

這個設計確保：
- 新增供應商時，只需寫轉換邏輯，不需要碰 HTTP 程式碼
- HTTP 層的改進（如連接池優化）自動惠及所有供應商
- 測試可以獨立進行：mock HTTP 層來測試轉換，mock 轉換來測試 HTTP

## 學習重點

1. `BaseLLMHTTPHandler` 是所有新式供應商共用的 HTTP 引擎
2. 它實作了四種呼叫模式的完整處理（sync/async × stream/non-stream）
3. 錯誤處理是統一的，所有供應商的 HTTP 錯誤都映射到 LiteLLM 標準異常
4. 連接池透過共享的 httpx client 實現
5. `sign_request()` 是處理特殊認證（如 AWS SigV4）的擴展點

## 初學者補充：httpx vs requests

你可能更熟悉 `requests` 套件。LiteLLM 選用 `httpx` 是因為：

| 特性 | requests | httpx |
|------|----------|-------|
| 同步請求 | ✅ | ✅ |
| 非同步請求 | ❌（需要 aiohttp） | ✅（內建） |
| HTTP/2 | ❌ | ✅ |
| 串流回應 | 有限 | ✅ 完整支援 |
| 連接池 | 基本 | 進階（可控並發數） |

對 LiteLLM 來說，`httpx` 用同一個 API 同時支援同步和非同步是決定性優勢——不需要維護兩套 HTTP 程式碼。

## 常見 HTTP 錯誤速查

當你在使用 LiteLLM 時遇到錯誤，可以用這張表快速定位問題：

| 錯誤 | 可能原因 | 解決方向 |
|------|---------|---------|
| `AuthenticationError` (401) | API Key 錯誤或過期 | 檢查環境變數 |
| `RateLimitError` (429) | 請求太頻繁 | 降低頻率、加入 retry、用 Router 分流 |
| `Timeout` (408) | 模型回覆太慢 | 增加 timeout 設定、換更快的模型 |
| `BadRequestError` (400) | 請求格式有誤 | 檢查 messages 格式、model 名稱是否正確 |
| `NotFoundError` (404) | 模型名稱錯誤 | 確認 model ID 拼寫，檢查 `litellm/model` 前綴 |

---

[← 上一章：Router 路由引擎](./05-Router路由引擎.md) | [下一章：串流處理機制 →](./07-串流處理機制.md)
