# 04 - Tool 工具系統

## 為什麼 Tool 重要？

沒有 Tool 的 Agent 只能「說話」。有了 Tool，Agent 才能「做事」：
- 搜尋網路
- 呼叫 API
- 讀寫資料庫
- 執行程式碼
- 與其他 Agent 溝通

Tool 就是 Agent 和真實世界之間的橋樑。

## Tool 的繼承體系

```
BaseTool (抽象基底)
    ├── FunctionTool          ← 最常用：包裝 Python 函式
    ├── LongRunningFunctionTool   ← 長時間運行的工具
    ├── AuthenticatedFunctionTool  ← 需要認證的工具
    ├── AgentTool             ← 委派給另一個 Agent
    ├── TransferToAgentTool   ← 轉移控制權
    ├── GoogleSearchTool      ← Google 搜尋
    ├── MCPTool               ← MCP 協定工具
    └── RestApiTool           ← OpenAPI 自動生成的 REST 工具

BaseToolset (工具集合)
    ├── MCPToolset            ← MCP 伺服器的所有工具
    ├── OpenAPIToolset        ← OpenAPI 規格中的所有端點
    └── SkillToolset          ← 技能管理工具集
```

## BaseTool：工具的核心介面

**檔案位置：** `src/google/adk/tools/base_tool.py`

```python
class BaseTool(ABC):
    name: str              # 工具名稱（LLM 看到的名字）
    description: str       # 工具描述（LLM 用來判斷何時使用）
    is_long_running: bool  # 是否為長時間運行

    @abstractmethod
    async def run_async(self, args: dict, tool_context: ToolContext) -> Any:
        """執行工具邏輯"""
        ...

    def _get_declaration(self) -> FunctionDeclaration:
        """告訴 LLM 這個工具長什麼樣（參數、返回值）"""
        ...

    def process_llm_request(self, tool_context, llm_request):
        """在 LLM 呼叫前修改請求（進階用法）"""
        ...
```

**三個最重要的方法：**
1. `run_async` — 執行工具的核心邏輯
2. `_get_declaration` — 產生 FunctionDeclaration 讓 LLM 知道怎麼呼叫
3. `process_llm_request` — 可以在 LLM 呼叫前做額外處理

## FunctionTool：最簡單也最常用

```python
from google.adk.tools import FunctionTool

# 方法 1：直接寫函式，ADK 自動包裝
def get_weather(city: str) -> str:
    """取得指定城市的天氣資訊"""
    return f"{city} 目前 28°C，晴天"

# 方法 2：明確包裝
weather_tool = FunctionTool(get_weather)

# 方法 3：直接放進 Agent 的 tools 列表（ADK 自動偵測並包裝）
agent = Agent(
    tools=[get_weather]  # 自動變成 FunctionTool
)
```

### FunctionTool 的魔法：自動產生 Declaration

ADK 會自動解析你的函式簽名：

```python
def search_products(
    query: str,           # → 必要的字串參數
    max_results: int = 10, # → 可選的整數參數，預設 10
    category: str = None,  # → 可選的字串參數
) -> list[dict]:
    """搜尋產品資料庫
    
    Args:
        query: 搜尋關鍵字
        max_results: 最多回傳幾筆結果
        category: 產品類別篩選
    """
    ...
```

ADK 會自動產生等價的 FunctionDeclaration：
```json
{
  "name": "search_products",
  "description": "搜尋產品資料庫",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "搜尋關鍵字"},
      "max_results": {"type": "integer", "description": "最多回傳幾筆結果"},
      "category": {"type": "string", "description": "產品類別篩選"}
    },
    "required": ["query"]
  }
}
```

**資深工程師觀點：** 這是 Python 的 type hints + docstring 驅動的元程式設計。寫好型別標注和文件字串，工具定義自動完成。

## ToolContext：工具的執行環境

```python
async def my_tool(query: str, tool_context: ToolContext) -> str:
    # tool_context 提供：
    
    # 1. 存取 Session 狀態
    user_pref = tool_context.state.get("user_preference")
    tool_context.state["last_query"] = query
    
    # 2. 存取 Artifact（檔案）
    await tool_context.save_artifact("report.pdf", pdf_bytes)
    
    # 3. 取得認證資訊
    creds = tool_context.get_auth_response()
    
    # 4. 請求用戶認證
    tool_context.request_credential(auth_config)
    
    # 5. 存取記憶
    memories = await tool_context.search_memory("相關主題")
    
    return "結果"
```

**重點：** 如果你的函式參數中有 `tool_context: ToolContext`，ADK 會自動注入它。這是依賴注入模式。

## BaseToolset：工具的集合

當你有一整組相關工具時，用 Toolset 管理：

```python
class BaseToolset(ABC):
    tool_filter: ToolPredicate | list[str]  # 過濾哪些工具可用
    tool_name_prefix: str                    # 工具名稱前綴

    @abstractmethod
    async def get_tools(self, readonly_context) -> list[BaseTool]:
        """返回這個集合中的所有工具"""
        ...
```

### 為什麼需要 Toolset？

```python
# 情境：你有一個 OpenAPI 規格，裡面有 50 個端點
# 不需要手動定義 50 個工具

openapi_toolset = OpenAPIToolset(
    spec=load_spec("api.yaml"),
    tool_filter=["get_user", "create_order", "search_products"],  # 只暴露 3 個
    tool_name_prefix="shop_",  # 加前綴避免名稱衝突
)

agent = Agent(tools=[openapi_toolset])
# Agent 看到的工具: shop_get_user, shop_create_order, shop_search_products
```

### 動態工具過濾（ToolPredicate）

```python
# 根據 context 動態決定哪些工具可用
def admin_only_filter(tool, readonly_context):
    """只有管理員可以使用刪除工具"""
    if "delete" in tool.name:
        return readonly_context.state.get("is_admin", False)
    return True

toolset = MyToolset(tool_filter=admin_only_filter)
```

**資深工程師觀點：** 動態工具過濾是做 RBAC（角色存取控制）的關鍵技術。同一個 Agent 可以根據用戶身份暴露不同的能力。

## 特殊工具類型

### LongRunningFunctionTool

```python
# 適用於：需要幾分鐘甚至幾小時才能完成的操作
long_tool = LongRunningFunctionTool(start_training_job)

# 工作流程：
# 1. 工具返回一個 resource_id（任務 ID）
# 2. Agent 回報「任務已啟動」給用戶
# 3. 用戶之後可以查詢任務狀態
```

### AgentTool：Agent 即工具

```python
# 把一個 Agent 當作工具使用
# 適合：需要一個專業 Agent 完成子任務，但不需要轉移控制權

specialist = Agent(name="data_analyst", ...)

# 主 Agent 可以「呼叫」 specialist 就像呼叫工具一樣
main_agent = Agent(
    tools=[AgentTool(agent=specialist)]
)
```

**Agent 轉移 vs AgentTool 的差別：**

| 特性 | Agent 轉移 | AgentTool |
|------|-----------|-----------|
| 控制權 | 完全交出 | 保留在主 Agent |
| Session 隔離 | 共享 | 可隔離 |
| 適用場景 | 長對話切換 | 單次子任務 |

### TransferToAgentTool

```python
# ADK 內部使用：實現 Agent 轉移的機制
# 當 LLM 決定轉移時，實際上是在呼叫這個工具
transfer_tool = TransferToAgentTool(agent=target_agent)
```

## 工具的完整生命週期

```
1. Agent 定義時註冊工具
   └── canonical_tools() 解析所有工具

2. Flow 準備 LLM 請求
   └── 每個工具的 _get_declaration() 產生函式定義
   └── 工具的 process_llm_request() 可以修改請求

3. LLM 決定呼叫工具
   └── 回傳 FunctionCall(name="...", args={...})

4. 回呼層
   └── before_tool_callback (可以攔截)

5. 工具執行
   └── tool.run_async(args, tool_context)

6. 回呼層
   └── after_tool_callback (可以修改結果)

7. 結果加入對話歷史
   └── 下一次 LLM 呼叫會看到工具結果
```

## 設計洞察

### 1. 為什麼 Tool 用 async？

因為大部分工具都涉及 I/O（網路請求、資料庫查詢）。用 async 可以在工具等待 I/O 時不阻塞其他工作。

### 2. 為什麼有 process_llm_request？

有些工具需要修改 LLM 的行為。例如：
- Google Search 工具需要啟用 LLM 的 grounding 功能
- Code Execution 工具需要設定特殊的模型參數

### 3. 為什麼 Toolset 有快取？

```python
# get_tools_with_prefix() 有快取機制
# 因為同一次 invocation 中，工具列表不會改變
# 避免每次 LLM 呼叫都重新計算工具列表
```

### 4. 為什麼支援混合工具來源？

```python
agent = Agent(tools=[
    get_weather,              # 普通函式
    FunctionTool(calculate),  # 明確包裝的工具
    MCPToolset(...),          # MCP 伺服器的工具
    OpenAPIToolset(...),      # OpenAPI 規格的工具
])
```

因為在真實系統中，工具來自不同地方。統一的介面讓你不需要關心工具的來源。

## 下一步

接下來看 Workflow 工作流——當你需要確定性的流程控制時怎麼辦。
