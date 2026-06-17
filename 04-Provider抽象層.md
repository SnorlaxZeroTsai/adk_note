# 第四章：Provider 抽象層

> **本章目標**：理解 LiteLLM 如何用一個抽象基類（BaseConfig）管理 100+ 供應商的差異。這是整個專案最核心的設計模式。
>
> **預計閱讀時間**：25 分鐘
>
> **前置閱讀**：[第三章：核心入口 main.py](./03-核心入口-main-py解析.md)
>
> **你會學到的設計模式**：策略模式（Strategy Pattern）—— 工作面試和實際專案中都非常常用。

## 核心設計：BaseConfig 抽象類別

每一個 LLM 供應商在 LiteLLM 中都由一個「配置類別」來代表。所有配置類別都繼承自 `BaseConfig`，這是經典的**策略模式（Strategy Pattern）**。

> **給初學者的類比**：想像你開了一家國際快遞公司。每個國家的寄件規則不同（格式、重量限制、報關方式），但客戶只需要填一張統一的託運單。`BaseConfig` 就是那張「託運單的模板」——每個國家的代理商（Provider）按照模板實作自己的轉換邏輯。

```
                    BaseConfig (ABC)
                         │
         ┌───────────────┼───────────────┐
         │               │               │
   OpenAIConfig   AnthropicConfig   GeminiConfig
         │               │               │
   處理 OpenAI    處理 Anthropic    處理 Google
   的格式轉換     的格式轉換        的格式轉換
```

## BaseConfig 的核心抽象方法

```python
# litellm/llms/base_llm/chat/transformation.py

class BaseConfig(ABC):
    """所有供應商 Chat Completion 配置的基類"""
    
    @abstractmethod
    def get_supported_openai_params(self, model: str) -> list:
        """回傳此供應商支援哪些 OpenAI 標準參數"""
        pass
    
    @abstractmethod
    def map_openai_params(
        self, 
        non_default_params: dict, 
        optional_params: dict,
        model: str,
        ...
    ) -> dict:
        """將 OpenAI 格式的參數映射到供應商特定格式"""
        pass
    
    @abstractmethod
    def transform_request(
        self,
        model: str,
        messages: List,
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> dict:
        """將完整請求轉換為供應商的 API 格式"""
        pass
    
    @abstractmethod
    def transform_response(
        self,
        model: str,
        raw_response: httpx.Response,
        model_response: ModelResponse,
        logging_obj: LiteLLMLoggingObj,
        ...
    ) -> ModelResponse:
        """將供應商的原生回應轉換為 OpenAI 格式"""
        pass
    
    @abstractmethod
    def get_complete_url(
        self,
        api_base: Optional[str],
        model: str,
        ...
    ) -> str:
        """組合完整的 API endpoint URL"""
        pass
    
    @abstractmethod
    def validate_environment(
        self,
        headers: dict,
        model: str,
        api_key: Optional[str],
        ...
    ) -> dict:
        """驗證環境變數並回傳認證 headers"""
        pass
```

## 實例解析：Anthropic 的實作

讓我們看看 Anthropic 如何實作這些方法：

### get_supported_openai_params

```python
# litellm/llms/anthropic/chat/transformation.py（概念化）

class AnthropicChatConfig(BaseConfig):
    def get_supported_openai_params(self, model: str) -> list:
        return [
            "max_tokens",
            "stream",
            "stop",
            "temperature",
            "top_p",
            "tools",
            "tool_choice",
            "response_format",    # 支援 JSON mode
            "thinking",           # Anthropic 獨有
            # 注意：不包含 frequency_penalty、logit_bias 等
            # 因為 Anthropic API 不支援這些
        ]
```

### transform_request

```python
    def transform_request(self, model, messages, optional_params, ...):
        """
        OpenAI 格式:
        {
            "model": "claude-3-opus",
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hello"}
            ],
            "max_tokens": 1024
        }
        
        轉換為 Anthropic 格式:
        {
            "model": "claude-3-opus-20240229",
            "system": "You are helpful",
            "messages": [
                {"role": "user", "content": "Hello"}
            ],
            "max_tokens": 1024
        }
        """
        # Anthropic 的 system message 是獨立欄位，不在 messages 內
        system_prompt = None
        transformed_messages = []
        
        for msg in messages:
            if msg["role"] == "system":
                system_prompt = msg["content"]
            else:
                transformed_messages.append(msg)
        
        request_body = {
            "model": model,
            "messages": transformed_messages,
            "max_tokens": optional_params.get("max_tokens", 4096),
        }
        
        if system_prompt:
            request_body["system"] = system_prompt
        
        return request_body
```

### transform_response

```python
    def transform_response(self, model, raw_response, model_response, ...):
        """
        Anthropic 回應:
        {
            "id": "msg_abc123",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5}
        }
        
        轉換為 OpenAI 格式:
        {
            "id": "msg_abc123",
            "choices": [{"message": {"role": "assistant", "content": "Hello!"}, ...}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        }
        """
        response_json = raw_response.json()
        
        # 提取文字內容
        content = ""
        for block in response_json["content"]:
            if block["type"] == "text":
                content += block["text"]
        
        # 映射 stop_reason
        finish_reason_map = {
            "end_turn": "stop",
            "max_tokens": "length",
            "tool_use": "tool_calls",
        }
        
        model_response.choices = [
            Choices(
                message=Message(role="assistant", content=content),
                finish_reason=finish_reason_map.get(
                    response_json["stop_reason"], "stop"
                ),
                index=0,
            )
        ]
        
        model_response.usage = Usage(
            prompt_tokens=response_json["usage"]["input_tokens"],
            completion_tokens=response_json["usage"]["output_tokens"],
            total_tokens=(
                response_json["usage"]["input_tokens"] 
                + response_json["usage"]["output_tokens"]
            ),
        )
        
        return model_response
```

## ProviderConfigManager — 配置管理器

`ProviderConfigManager` 是查找正確 Config 實例的中央註冊表：

```python
# 概念化的使用方式
config = ProviderConfigManager.get_provider_chat_config(
    model="claude-3-opus",
    provider=LlmProviders.ANTHROPIC,
)
# → 回傳 AnthropicChatConfig 的實例
```

每種供應商和每種 API 類型（chat、embedding、image 等）都有對應的 Config：

```
BaseConfig (chat)
BaseEmbeddingConfig (embedding)
BaseImageGenerationConfig (image generation)
BaseAudioTranscriptionConfig (audio)
BaseRerankConfig (rerank)
BaseResponsesAPIConfig (responses)
```

## 供應商目錄結構

每個供應商在 `litellm/llms/` 下都有標準化的目錄結構：

```
litellm/llms/anthropic/
├── __init__.py
├── chat/
│   ├── __init__.py
│   ├── handler.py           # 舊式：獨立的請求處理器
│   └── transformation.py    # 核心：BaseConfig 實作
├── completion/
│   └── transformation.py    # Text completion 轉換
├── cost_calculation.py      # 成本計算邏輯
├── common_utils.py          # 共用工具
└── experimental/            # 實驗性功能
```

## 參數映射的實際案例

### OpenAI → Anthropic 的 tools 轉換

OpenAI 的 tool 定義：
```json
{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get weather info",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string"}
            }
        }
    }
}
```

Anthropic 的 tool 定義（格式不同）：
```json
{
    "name": "get_weather",
    "description": "Get weather info",
    "input_schema": {
        "type": "object",
        "properties": {
            "location": {"type": "string"}
        }
    }
}
```

`transform_request` 會自動處理這種差異。

### stop_reason / finish_reason 映射

| OpenAI | Anthropic | Google | 含義 |
|--------|-----------|--------|------|
| `stop` | `end_turn` | `STOP` | 正常結束 |
| `length` | `max_tokens` | `MAX_TOKENS` | 達到 token 上限 |
| `tool_calls` | `tool_use` | `TOOL_CALL` | 模型要求使用工具 |
| `content_filter` | - | `SAFETY` | 內容被過濾 |

## 設計模式分析

### 為什麼用策略模式而非繼承樹？

```python
# ❌ 不好的設計：深層繼承
class LLM:
    def call(self): ...

class OpenAILLM(LLM):
    def call(self): ...

class AzureOpenAILLM(OpenAILLM):  # 繼承 OpenAI
    def call(self): ...
```

```python
# ✅ LiteLLM 的設計：扁平的策略模式
class BaseConfig(ABC):  # 介面定義
    @abstractmethod
    def transform_request(self): ...

class OpenAIConfig(BaseConfig):     # 獨立實作
    def transform_request(self): ...

class AzureOpenAIConfig(BaseConfig): # 獨立實作
    def transform_request(self): ...
```

優勢：
1. **低耦合**：修改 OpenAI 不會影響 Azure
2. **易測試**：每個 Config 可以獨立單元測試
3. **易擴展**：新增供應商只需新增一個 Config 類別

### OpenAI-Like 供應商

許多供應商（如 Groq、Together AI、Fireworks）使用與 OpenAI 相容的 API。它們繼承 `OpenAIConfig` 並只覆蓋差異部分：

```python
class GroqConfig(OpenAIConfig):
    """Groq 與 OpenAI 格式相容，只需覆蓋 URL 和認證"""
    
    def get_complete_url(self, api_base, model, ...):
        return "https://api.groq.com/openai/v1/chat/completions"
    
    def validate_environment(self, headers, model, api_key, ...):
        headers["Authorization"] = f"Bearer {api_key}"
        return headers
    
    # transform_request 和 transform_response 直接繼承 OpenAI 的
```

## 學習重點

1. **策略模式**是 LiteLLM 處理 100+ 供應商的核心架構選擇
2. **BaseConfig** 定義了統一的轉換介面
3. **每個供應商**只需實作「輸入轉換」和「輸出轉換」
4. **OpenAI-compatible** 供應商可以直接繼承，大幅減少重複程式碼
5. **關注點分離**：Config 只管轉換邏輯，不管 HTTP 通訊

## 這在你的工作中如何應用

策略模式不只用在 LLM 整合。當你遇到以下場景時，都可以用同樣的設計：

| 場景 | BaseClass | Strategies |
|------|-----------|-----------|
| 支付系統整合 | `BasePaymentGateway` | `StripeGateway`, `PayPalGateway` |
| 通知系統 | `BaseNotifier` | `EmailNotifier`, `SlackNotifier`, `SMSNotifier` |
| 資料匯出 | `BaseExporter` | `CSVExporter`, `JSONExporter`, `ExcelExporter` |
| 認證方式 | `BaseAuthProvider` | `OAuth2Provider`, `SAMLProvider`, `APIKeyProvider` |

核心思路都一樣：**定義統一介面，讓每個實作專注處理自己的差異**。

## 動手練習

1. 打開 `litellm/llms/anthropic/chat/transformation.py`，找到 `transform_request` 方法，看看它如何處理 `system` message（Anthropic 把 system 放在獨立欄位而非 messages 陣列中）。
2. 打開 `litellm/llms/openai/chat/gpt_transformation.py`，對比它的 `transform_request`——你會發現 OpenAI 幾乎不需要轉換，因為 LiteLLM 本身就用 OpenAI 格式。
3. 如果你想挑戰自己，試著為一個假想的供應商寫一個 Config（第 12 章有完整教學）。

---

[← 上一章：核心入口 main.py](./03-核心入口-main-py解析.md) | [下一章：Router 路由引擎 →](./05-Router路由引擎.md)
