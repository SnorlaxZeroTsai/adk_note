# 第十二章：實戰 — 新增一個 Provider

> **本章目標**：跟著步驟實際為 LiteLLM 新增一個供應商。這是你向 LiteLLM 提交 PR 的入門路徑。
>
> **預計閱讀時間**：40 分鐘（含動手練習）
>
> **前置閱讀**：[第四章：Provider 抽象層](./04-Provider抽象層.md)（必讀）、[第六章：HTTP Handler](./06-HTTP-Handler統一通訊層.md)（建議）
>
> **你會獲得的技能**：讀完並跟著做一遍後，你就具備向 LiteLLM 提交真實 PR 的能力。LiteLLM 的 contributor 指南中，「新增供應商」是最常見的貢獻類型之一。

## 目標

本章將手把手帶你為 LiteLLM 新增一個假想的供應商「MagicAI」。完成後你將理解：
- 新增供應商需要哪些檔案
- 每個方法的具體實作
- 如何測試你的實作

> **重要提示**：真實的 PR 需要更多工作（文件、CI 測試、edge case 處理），但核心流程和本章展示的一模一樣。先跑通這個流程，再去看 LiteLLM 的 [CONTRIBUTING.md](https://github.com/BerriAI/litellm/blob/main/CONTRIBUTING.md) 了解完整的 PR 要求。

## 假設

MagicAI 的 API 規格：

```
POST https://api.magic-ai.com/v1/generate
Headers:
  X-Magic-Key: <api_key>
  Content-Type: application/json

Body:
{
    "model_id": "magic-large",
    "prompt_messages": [
        {"speaker": "human", "text": "Hello"}
    ],
    "max_output_length": 1024,
    "randomness": 0.7
}

Response:
{
    "request_id": "req-123",
    "output": {
        "speaker": "ai",
        "text": "Hello! How can I help?"
    },
    "stop_cause": "natural",
    "token_stats": {
        "input_count": 5,
        "output_count": 8
    }
}
```

## 步驟 1：建立目錄結構

```
litellm/llms/magic_ai/
├── __init__.py
├── chat/
│   ├── __init__.py
│   └── transformation.py    # 核心：BaseConfig 實作
└── common_utils.py          # 共用工具（可選）
```

## 步驟 2：實作 Transformation Config

```python
# litellm/llms/magic_ai/chat/transformation.py

from typing import Any, Dict, List, Optional, Tuple, Type, Union
import httpx
from litellm.llms.base_llm.chat.transformation import BaseConfig, BaseLLMException
from litellm.types.utils import ModelResponse, Choices, Message, Usage


class MagicAIConfig(BaseConfig):
    """MagicAI Chat Completion 配置"""
    
    # ═══════════════════════════════════════════════
    # 1. 支援的 OpenAI 參數
    # ═══════════════════════════════════════════════
    
    def get_supported_openai_params(self, model: str) -> list:
        """宣告此供應商支援哪些 OpenAI 標準參數"""
        return [
            "max_tokens",
            "temperature",
            "stream",
            "stop",
            "top_p",
        ]
    
    # ═══════════════════════════════════════════════
    # 2. 參數映射
    # ═══════════════════════════════════════════════
    
    def map_openai_params(
        self,
        non_default_params: dict,
        optional_params: dict,
        model: str,
        drop_params: bool,
    ) -> dict:
        """將 OpenAI 參數名稱映射到 MagicAI 的參數名稱"""
        for key, value in non_default_params.items():
            if key == "max_tokens":
                optional_params["max_output_length"] = value
            elif key == "temperature":
                optional_params["randomness"] = value
            elif key == "stop":
                optional_params["stop_sequences"] = value
            elif key == "top_p":
                optional_params["nucleus_sampling"] = value
        
        return optional_params
    
    # ═══════════════════════════════════════════════
    # 3. 請求轉換
    # ═══════════════════════════════════════════════
    
    def transform_request(
        self,
        model: str,
        messages: List[Dict],
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> dict:
        """將 OpenAI 格式的請求轉換為 MagicAI 格式"""
        
        # 轉換 messages 格式
        magic_messages = []
        for msg in messages:
            speaker_map = {
                "system": "system",
                "user": "human",
                "assistant": "ai",
            }
            magic_messages.append({
                "speaker": speaker_map.get(msg["role"], "human"),
                "text": msg["content"],
            })
        
        # 組裝請求體
        request_body = {
            "model_id": model,
            "prompt_messages": magic_messages,
        }
        
        # 加入可選參數
        if "max_output_length" in optional_params:
            request_body["max_output_length"] = optional_params["max_output_length"]
        if "randomness" in optional_params:
            request_body["randomness"] = optional_params["randomness"]
        if "stop_sequences" in optional_params:
            request_body["stop_sequences"] = optional_params["stop_sequences"]
        
        return request_body
    
    # ═══════════════════════════════════════════════
    # 4. 回應轉換
    # ═══════════════════════════════════════════════
    
    def transform_response(
        self,
        model: str,
        raw_response: httpx.Response,
        model_response: ModelResponse,
        logging_obj,
        request_data: dict,
        messages: List,
        optional_params: dict,
        litellm_params: dict,
        encoding,
        api_key: Optional[str] = None,
        json_mode: bool = False,
    ) -> ModelResponse:
        """將 MagicAI 的回應轉換為 OpenAI 格式"""
        
        response_json = raw_response.json()
        
        # 映射 stop_cause → finish_reason
        finish_reason_map = {
            "natural": "stop",
            "length_limit": "length",
            "tool_request": "tool_calls",
        }
        
        # 建構統一的 ModelResponse
        model_response.choices = [
            Choices(
                message=Message(
                    role="assistant",
                    content=response_json["output"]["text"],
                ),
                finish_reason=finish_reason_map.get(
                    response_json.get("stop_cause", "natural"), "stop"
                ),
                index=0,
            )
        ]
        
        # 設定 usage
        token_stats = response_json.get("token_stats", {})
        model_response.usage = Usage(
            prompt_tokens=token_stats.get("input_count", 0),
            completion_tokens=token_stats.get("output_count", 0),
            total_tokens=(
                token_stats.get("input_count", 0) 
                + token_stats.get("output_count", 0)
            ),
        )
        
        # 設定 ID
        model_response.id = response_json.get("request_id", "")
        model_response.model = model
        
        return model_response
    
    # ═══════════════════════════════════════════════
    # 5. URL 組合
    # ═══════════════════════════════════════════════
    
    def get_complete_url(
        self,
        api_base: Optional[str],
        model: str,
        optional_params: dict,
        stream: Optional[bool] = None,
        litellm_params: Optional[dict] = None,
    ) -> str:
        """組合完整的 API URL"""
        if api_base:
            return f"{api_base}/v1/generate"
        return "https://api.magic-ai.com/v1/generate"
    
    # ═══════════════════════════════════════════════
    # 6. 環境驗證（認證 headers）
    # ═══════════════════════════════════════════════
    
    def validate_environment(
        self,
        headers: dict,
        model: str,
        messages: List,
        optional_params: dict,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        litellm_params: Optional[dict] = None,
    ) -> dict:
        """設定認證 headers"""
        if api_key:
            headers["X-Magic-Key"] = api_key
        headers["Content-Type"] = "application/json"
        return headers
    
    # ═══════════════════════════════════════════════
    # 7. 錯誤處理
    # ═══════════════════════════════════════════════
    
    def get_error_class(
        self,
        error_message: str,
        status_code: int,
        headers: Union[dict, httpx.Headers],
    ) -> BaseLLMException:
        """將 HTTP 錯誤轉換為 LiteLLM 異常"""
        return BaseLLMException(
            status_code=status_code,
            message=error_message,
            headers=headers,
        )
```

## 步驟 3：註冊供應商

在 `litellm/constants.py` 中加入供應商名稱：

```python
# litellm/constants.py
LITELLM_CHAT_PROVIDERS = [
    "openai",
    "anthropic",
    # ... 其他供應商
    "magic_ai",  # ← 新增
]
```

在 Provider 列舉中加入：

```python
# litellm/types/utils.py
class LlmProviders(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    # ...
    MAGIC_AI = "magic_ai"  # ← 新增
```

## 步驟 4：連接到 ProviderConfigManager

```python
# litellm/utils.py（在 ProviderConfigManager 中）

class ProviderConfigManager:
    @staticmethod
    def get_provider_chat_config(model: str, provider: LlmProviders):
        if provider == LlmProviders.MAGIC_AI:
            from litellm.llms.magic_ai.chat.transformation import MagicAIConfig
            return MagicAIConfig()
        # ... 其他供應商
```

## 步驟 5：在 main.py 中加入路由

```python
# litellm/main.py completion() 函式中

elif custom_llm_provider == "magic_ai":
    response = base_llm_http_handler.completion(
        model=model,
        messages=messages,
        api_base=api_base,
        custom_llm_provider=custom_llm_provider,
        model_response=model_response,
        encoding=encoding,
        logging_obj=logging,
        optional_params=optional_params,
        timeout=timeout,
        litellm_params=litellm_params,
        acompletion=acompletion,
        stream=stream,
        api_key=api_key,
        headers=headers,
        provider_config=ProviderConfigManager.get_provider_chat_config(
            model=model, provider=LlmProviders.MAGIC_AI
        ),
    )
```

## 步驟 6：使用

```python
import litellm
import os

os.environ["MAGIC_AI_API_KEY"] = "your-key-here"

response = litellm.completion(
    model="magic_ai/magic-large",
    messages=[{"role": "user", "content": "Hello!"}],
    temperature=0.7,
    max_tokens=1024,
)

print(response.choices[0].message.content)
# → "Hello! How can I help?"
```

## 步驟 7：撰寫測試

```python
# tests/test_litellm/llms/test_magic_ai/test_magic_ai_chat_transformation.py

import httpx
import pytest
from litellm.llms.magic_ai.chat.transformation import MagicAIConfig


class TestMagicAITransformRequest:
    def setup_method(self):
        self.config = MagicAIConfig()
    
    def test_basic_message_transform(self):
        """測試基本的 message 格式轉換"""
        messages = [
            {"role": "system", "content": "Be helpful"},
            {"role": "user", "content": "Hello"},
        ]
        
        result = self.config.transform_request(
            model="magic-large",
            messages=messages,
            optional_params={},
            litellm_params={},
            headers={},
        )
        
        assert result["model_id"] == "magic-large"
        assert result["prompt_messages"] == [
            {"speaker": "system", "text": "Be helpful"},
            {"speaker": "human", "text": "Hello"},
        ]
    
    def test_optional_params_mapping(self):
        """測試參數映射"""
        result = self.config.transform_request(
            model="magic-large",
            messages=[{"role": "user", "content": "Hi"}],
            optional_params={"max_output_length": 512, "randomness": 0.5},
            litellm_params={},
            headers={},
        )
        
        assert result["max_output_length"] == 512
        assert result["randomness"] == 0.5


class TestMagicAITransformResponse:
    def setup_method(self):
        self.config = MagicAIConfig()
    
    def test_basic_response_transform(self):
        """測試基本的回應格式轉換"""
        from litellm.types.utils import ModelResponse
        from unittest.mock import MagicMock
        
        # Mock httpx response
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.json.return_value = {
            "request_id": "req-123",
            "output": {"speaker": "ai", "text": "Hello!"},
            "stop_cause": "natural",
            "token_stats": {"input_count": 5, "output_count": 3},
        }
        
        model_response = ModelResponse()
        result = self.config.transform_response(
            model="magic-large",
            raw_response=mock_response,
            model_response=model_response,
            logging_obj=MagicMock(),
            request_data={},
            messages=[],
            optional_params={},
            litellm_params={},
            encoding=None,
        )
        
        assert result.choices[0].message.content == "Hello!"
        assert result.choices[0].finish_reason == "stop"
        assert result.usage.prompt_tokens == 5
        assert result.usage.completion_tokens == 3
```

## 核心心法

新增供應商的本質就是回答四個問題：

| 問題 | 對應方法 |
|------|---------|
| 這個供應商的 URL 是什麼？ | `get_complete_url()` |
| 怎麼認證？ | `validate_environment()` |
| 怎麼把 OpenAI 格式轉成它的格式？ | `transform_request()` |
| 怎麼把它的回應轉成 OpenAI 格式？ | `transform_response()` |

其他一切（HTTP 發送、錯誤處理、串流、日誌）都由 `BaseLLMHTTPHandler` 自動處理。

## 學習重點

1. 新增供應商**只需實作 BaseConfig 的方法**，不需要寫 HTTP 程式碼
2. 核心是**雙向格式轉換**：request 轉進去，response 轉出來
3. `get_supported_openai_params()` 控制哪些參數會被傳遞
4. 測試應該覆蓋**轉換邏輯**，不需要實際呼叫 API
5. 利用 `base_llm_http_handler` 免費獲得 sync/async/stream 支援

## 常見踩坑點

| 問題 | 症狀 | 解決 |
|------|------|------|
| 忘記處理 system message | Anthropic-like API 的 system 是獨立欄位 | 在 `transform_request` 中特別處理 |
| finish_reason 映射不完整 | 客戶端收到 `None` 的 finish_reason | 加上 fallback default |
| usage 欄位缺失 | 成本計算回傳 $0 | 確保 `transform_response` 設定了 `usage` |
| 忘記在 constants.py 註冊 | `model not found` 錯誤 | 加到 `LITELLM_CHAT_PROVIDERS` |
| API Key header 名稱錯誤 | 401 認證失敗 | 仔細看供應商文件的認證方式 |

## 挑戰練習：進階擴展

如果你已經完成基本實作，嘗試：

1. **支援串流**：實作 `BaseModelResponseIterator` 的子類別來解析供應商的串流格式
2. **支援 Tool Use**：在 `transform_request` 中把 OpenAI 的 tools 格式轉成供應商的格式
3. **加入定價**：在 `model_prices_and_context_window.json` 中加入你的供應商模型定價
4. **寫整合測試**：用 `respx` 或 `responses` 套件 mock HTTP 回應，做端對端測試

---

[← 上一章：可觀測性](./11-可觀測性與日誌.md) | [下一章：AI 工程師進階知識 →](./13-AI工程師進階知識.md)
