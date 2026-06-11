# 08 - Model 模型層

## 核心問題

ADK 不只支援 Gemini，它支援多個 LLM 供應商：
- Google Gemini（原生）
- Anthropic Claude
- OpenAI GPT
- LiteLLM（統一多家）
- 本地模型（Gemma 等）

**設計挑戰：** 怎麼讓 Agent 程式碼不依賴具體的模型供應商？

## 解法：Registry 模式

**檔案位置：** `src/google/adk/models/`

```
BaseLlm (抽象介面)
    ├── Gemini (Google)
    ├── Claude (Anthropic)
    ├── OpenAI (GPT)
    ├── LiteLlm (統一適配器)
    └── 你的自訂模型

LLMRegistry (工廠 + 註冊表)
    └── 根據模型名稱自動找到對應的實作
```

## BaseLlm：統一的模型介面

```python
class BaseLlm(ABC):
    model: str  # 模型名稱

    @abstractmethod
    async def generate_content_async(
        self, 
        llm_request: LlmRequest, 
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        """產生回應（串流模式）"""
        ...
```

**關鍵設計：** 只有一個方法 `generate_content_async`。不管什麼模型，介面就是：
- 輸入：`LlmRequest`（問題）
- 輸出：`AsyncGenerator[LlmResponse]`（回答，可能分多次串流）

## LlmRequest：送給模型的請求

```python
class LlmRequest:
    model: str                    # 模型名稱
    contents: list[Content]       # 對話歷史
    config: GenerateContentConfig # 模型參數
    tools_dict: dict[str, BaseTool]  # 可用工具
    cache_config: CacheConfig     # 快取設定
    cache_metadata: dict          # 之前的快取資料
```

**contents 的結構：**
```python
contents = [
    Content(role="user", parts=[Part(text="你好")]),
    Content(role="model", parts=[Part(text="你好！有什麼需要幫助的嗎？")]),
    Content(role="user", parts=[Part(text="幫我查天氣")]),
]
```

## LlmResponse：模型的回應

```python
class LlmResponse:
    content: Content           # 回應內容
    partial: bool              # 是否為串流片段
    finish_reason: str         # 結束原因（stop、max_tokens、tool_call）
    error_code: int            # 錯誤碼
    error_message: str         # 錯誤訊息
    grounding_metadata: dict   # 引用/Grounding 資料
    model_version: str         # 實際使用的模型版本
```

### finish_reason 的重要性

| finish_reason | 意義 | Flow 的處理 |
|---------------|------|------------|
| `STOP` | 正常結束 | 回傳給用戶 |
| `MAX_TOKENS` | 超過長度限制 | 可能需要繼續 |
| `TOOL_CALL` | 要呼叫工具 | 執行工具，再呼叫 LLM |
| `SAFETY` | 安全過濾 | 回報錯誤 |

## LLMRegistry：模型的工廠

```python
class LLMRegistry:
    """根據模型名稱自動找到對應的實作"""
    
    # 內部維護一個 regex → 建構函式 的映射
    _registry = {
        r"gemini-.*": GeminiLlm,
        r"claude-.*": ClaudeLlm,
        r"gpt-.*": OpenAILlm,
        r"litellm/.*": LiteLlmLlm,
    }
    
    @classmethod
    def get_llm(cls, model_name: str) -> BaseLlm:
        """根據名稱建立對應的 LLM 實例"""
        for pattern, constructor in cls._registry.items():
            if re.match(pattern, model_name):
                return constructor(model=model_name)
        raise ValueError(f"Unknown model: {model_name}")
```

### 使用方式

```python
# Agent 只需要指定模型名稱
agent = Agent(model="gemini-2.5-flash")      # → 自動用 GeminiLlm
agent = Agent(model="claude-sonnet-4-5-20250514")  # → 自動用 ClaudeLlm
agent = Agent(model="gpt-4o")                # → 自動用 OpenAILlm
```

**資深工程師觀點：** Registry 模式 + 惰性載入。模型的實作只在真正使用時才載入，避免啟動時載入所有供應商的 SDK。

## 模型解析流程

```
Agent.model = "gemini-2.5-flash"
    │
    ▼
LlmAgent.canonical_model
    │ （如果自己沒設定，往上找父 Agent）
    ▼
LLMRegistry.get_llm("gemini-2.5-flash")
    │ （regex 匹配找到 GeminiLlm）
    ▼
GeminiLlm(model="gemini-2.5-flash")
    │
    ▼
generate_content_async(request)
    │ （呼叫 Gemini API）
    ▼
LlmResponse
```

## 工具呼叫的統一處理

不同模型的 Function Calling 格式不同：

```python
# Gemini 的格式
{"functionCall": {"name": "get_weather", "args": {"city": "台北"}}}

# OpenAI 的格式  
{"tool_calls": [{"function": {"name": "get_weather", "arguments": "{\"city\": \"台北\"}"}}]}

# Claude 的格式
{"type": "tool_use", "name": "get_weather", "input": {"city": "台北"}}
```

**ADK 的解法：** 每個 LLM 實作負責把自家格式轉換成統一的 `Content` 格式。Agent 程式碼永遠不需要處理格式差異。

```python
# 統一格式（ADK 內部）
Content(
    role="model",
    parts=[
        Part(function_call=FunctionCall(name="get_weather", args={"city": "台北"}))
    ]
)
```

## 模型切換的實際案例

```python
# 場景：根任務用強模型，子任務用便宜模型

root = Agent(
    name="orchestrator",
    model="gemini-2.5-pro",      # 貴但聰明，負責規劃
    sub_agents=[
        Agent(
            name="data_fetcher",
            model="gemini-2.5-flash",  # 便宜快速，負責取資料
        ),
        Agent(
            name="analyzer",
            model="claude-sonnet-4-5-20250514",  # 分析能力強
        ),
    ]
)
```

**資深工程師觀點：** 這是「分層模型策略」。用貴的模型做高層決策，用便宜的做簡單任務。和微服務中「不同服務用不同規格的機器」是一樣的思路。

## 串流的處理

```python
# BaseLlm 的串流合約：
async def generate_content_async(self, request, stream=True):
    if stream:
        # yield 多個 partial=True 的回應
        yield LlmResponse(content=chunk1, partial=True)
        yield LlmResponse(content=chunk2, partial=True)
        # 最後 yield 完整回應
        yield LlmResponse(content=full_response, partial=False)
    else:
        # 直接 yield 完整回應
        yield LlmResponse(content=full_response, partial=False)
```

## 快取機制

ADK 支援 Context Caching（減少重複的 token 計費）：

```python
# 場景：系統指令很長（幾千 token），每次對話都要送
# 快取讓你只付一次費

llm_request.cache_config = CacheConfig(
    cache_instructions=True,  # 快取系統指令
    ttl_seconds=3600,         # 快取 1 小時
)
```

## 設計洞察

### 1. 為什麼用 Registry 而不是 if-else？

```python
# 不好的設計
if "gemini" in model_name:
    return GeminiLlm(model_name)
elif "claude" in model_name:
    return ClaudeLlm(model_name)

# 好的設計（Registry）
# - 新增模型只需要註冊，不修改現有程式碼（開放封閉原則）
# - 支援 regex 匹配（更靈活）
# - 惰性載入（不用的模型不載入）
```

### 2. 為什麼 generate_content_async 回傳 AsyncGenerator？

統一了串流和非串流兩種模式：
- 串流：yield 多次
- 非串流：yield 一次

呼叫者不需要知道是哪種模式，統一用 `async for` 處理。

### 3. 為什麼模型用字串名稱而不是類別？

```python
# 用字串：宣告式、可配置
Agent(model="gemini-2.5-flash")

# 用類別：程式碼耦合
Agent(model=GeminiLlm("gemini-2.5-flash"))
```

字串的好處：
- 可以從設定檔讀取
- 可以用環境變數覆蓋
- Agent 定義不依賴具體的 LLM SDK

### 4. 多模型系統的最佳實踐

| 場景 | 模型選擇 | 原因 |
|------|----------|------|
| 路由/分類 | Flash/Haiku | 快速便宜 |
| 複雜推理 | Pro/Opus | 準確度高 |
| 程式碼生成 | Sonnet/Pro | 平衡性能和成本 |
| 大量資料處理 | Flash/Haiku | 成本優先 |
| 安全敏感任務 | Pro/Opus | 可靠性優先 |

## 下一步

接下來看 Memory 記憶系統——Agent 如何跨對話記住事情。
