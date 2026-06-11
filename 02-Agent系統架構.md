# 02 - Agent 系統架構

## Agent 的繼承體系

```
BaseNode (工作流節點介面)
    └── BaseAgent (所有 Agent 的基底類別)
            ├── LlmAgent (LLM 驅動的 Agent) ← 最常用
            ├── SequentialAgent (順序執行子 Agent)
            ├── ParallelAgent (平行執行子 Agent)
            └── LoopAgent (迴圈執行子 Agent)
```

**重要觀察：** `BaseAgent` 繼承自 `BaseNode`，這代表每個 Agent 天生就能當作 Workflow 中的節點使用。這是一個精妙的設計決策——Agent 和 Workflow 共享同一套介面。

## BaseAgent：所有 Agent 的基底

**檔案位置：** `src/google/adk/agents/base_agent.py`

### 核心職責

```python
class BaseAgent(BaseNode):
    # 1. 身份
    name: str           # Agent 名稱（唯一識別）
    description: str    # Agent 的描述（用於 Agent 之間的轉移決策）

    # 2. 階層管理
    sub_agents: list    # 子 Agent 列表
    parent_agent: BaseAgent  # 父 Agent（自動設定）

    # 3. 生命週期 Hook
    before_agent_callback: Callable  # 執行前回呼
    after_agent_callback: Callable   # 執行後回呼
```

### 關鍵設計：階層式 Agent 樹

```
      Root Agent
      /    |    \
  Agent A  Agent B  Agent C
              |
          Agent B1
```

每個 Agent 知道自己的父親和孩子。這使得：
- **Agent 轉移**（Transfer）：把控制權交給另一個 Agent
- **狀態繼承**：子 Agent 可以存取父 Agent 的模型設定
- **搜尋**：可以在樹中找到任何 Agent

### Agent 發現機制

```python
def find_agent(self, name: str) -> Optional[BaseAgent]:
    """在整個 Agent 樹中搜尋指定名稱的 Agent"""
    # 先搜尋自己的子 Agent
    # 再往上搜尋（透過父 Agent）
    # 支援跨層級搜尋
```

**資深工程師觀點：** 這就像 DOM 樹的節點搜尋，或微服務架構中的服務發現。

## LlmAgent：最常使用的 Agent 類型

**檔案位置：** `src/google/adk/agents/llm_agent.py`

### 建立一個 LlmAgent

```python
from google.adk import Agent  # Agent 就是 LlmAgent 的別名

agent = Agent(
    name="research_agent",
    model="gemini-2.5-flash",
    instruction="你是一個研究助理，幫助用戶找到準確的資訊。",
    tools=[search_tool, summarize_tool],
    sub_agents=[detail_agent, citation_agent],
)
```

### LlmAgent 的關鍵屬性

| 屬性 | 用途 | 說明 |
|------|------|------|
| `model` | 使用哪個 LLM | 支援 Gemini、Claude、OpenAI 等 |
| `instruction` | 系統提示詞 | 可以是字串或動態函式 |
| `tools` | 可用工具 | 函式、Toolset、MCP 等 |
| `sub_agents` | 子 Agent | 可以轉移控制權的對象 |
| `output_schema` | 輸出格式 | 強制 JSON 結構化輸出 |
| `generate_content_config` | LLM 參數 | temperature、top_p 等 |

### 動態指令（Instruction Provider）

```python
# 靜態指令
agent = Agent(instruction="你是助理")

# 動態指令：根據 context 改變行為
def dynamic_instruction(context):
    user_role = context.state.get("user_role", "general")
    return f"你正在服務一位 {user_role}，請調整你的回應深度。"

agent = Agent(instruction=dynamic_instruction)
```

**資深工程師觀點：** 動態指令是建構 context-aware Agent 的關鍵。你可以根據用戶狀態、時間、對話歷史動態調整 Agent 的行為。

### 模型解析機制（Model Resolution）

```python
# 模型解析順序：
# 1. Agent 自己設定的 model
# 2. 往上找父 Agent 的 model
# 3. 使用預設 model

@property
def canonical_model(self):
    """解析最終使用的模型"""
    if self.model:
        return self.model
    if self.parent_agent:
        return self.parent_agent.canonical_model
    return default_model
```

**設計意圖：** 你可以在根 Agent 設定模型，所有子 Agent 自動繼承。需要特殊模型的子 Agent 可以覆寫。

### 工具解析機制

```python
def canonical_tools(self) -> list[BaseTool]:
    """解析 Agent 最終可用的工具列表"""
    tools = []
    for tool_union in self.tools:
        if callable(tool_union):
            tools.append(FunctionTool(tool_union))  # 自動包裝函式
        elif isinstance(tool_union, BaseTool):
            tools.append(tool_union)
        elif isinstance(tool_union, BaseToolset):
            tools.extend(tool_union.get_tools(context))
    return tools
```

**重點：** 你可以混用函式、Tool 物件、Toolset，ADK 會自動統一處理。

## 回呼系統（Callback System）

ADK 提供多層回呼，讓你在不修改核心邏輯的情況下介入流程：

```
┌─────────────────────────────────────────────┐
│  before_agent_callback                       │ ← 可以攔截，直接返回結果
│  ┌─────────────────────────────────────────┐ │
│  │  before_model_callback                   │ │ ← 修改送給 LLM 的請求
│  │  ┌───────────────────────────────────┐   │ │
│  │  │  LLM 呼叫                         │   │ │
│  │  └───────────────────────────────────┘   │ │
│  │  after_model_callback                    │ │ ← 修改 LLM 的回應
│  │                                          │ │
│  │  before_tool_callback                    │ │ ← 攔截工具呼叫
│  │  ┌───────────────────────────────────┐   │ │
│  │  │  工具執行                          │   │ │
│  │  └───────────────────────────────────┘   │ │
│  │  after_tool_callback                     │ │ ← 修改工具結果
│  └─────────────────────────────────────────┘ │
│  after_agent_callback                        │ ← 最終攔截
└─────────────────────────────────────────────┘
```

### 實際應用範例

```python
# 使用回呼做權限控制
def check_permission(callback_context, llm_request):
    if not callback_context.state.get("is_admin"):
        # 直接返回，不呼叫 LLM
        return LlmResponse(content="抱歉，你沒有權限執行此操作")
    return None  # None = 繼續正常流程

agent = Agent(
    name="admin_agent",
    before_model_callback=check_permission,
    ...
)
```

## Agent 轉移機制

當你有多個子 Agent 時，LLM 可以決定把控制權「轉移」給另一個 Agent：

```python
root = Agent(
    name="router",
    instruction="根據用戶需求，轉移到適當的專業 Agent",
    sub_agents=[
        Agent(name="coder", instruction="處理程式碼問題"),
        Agent(name="writer", instruction="處理寫作問題"),
    ]
)
```

### 轉移的方向

```
父 Agent ←→ 子 Agent     (上下轉移)
子 Agent ←→ 兄弟 Agent   (平級轉移)
```

### 控制轉移行為

```python
Agent(
    name="specialist",
    disallow_transfer_to_parent=True,   # 不能轉回父 Agent
    disallow_transfer_to_peers=True,    # 不能轉給兄弟 Agent
)
```

## Flow 的自動選擇

LlmAgent 根據配置自動選擇執行模式：

```
有子 Agent → AutoFlow（支援 Agent 轉移）
沒有子 Agent → SingleFlow（單純的 LLM 對話）
```

這是一個 **策略模式**（Strategy Pattern）的應用。

## 組合式 Agent（已棄用但概念重要）

```python
# 順序執行：A → B → C
sequential = SequentialAgent(sub_agents=[agent_a, agent_b, agent_c])

# 平行執行：A、B、C 同時跑
parallel = ParallelAgent(sub_agents=[agent_a, agent_b, agent_c])

# 迴圈執行：重複直到滿足條件
loop = LoopAgent(sub_agents=[agent_a], max_iterations=5)
```

**注意：** ADK 2.0 中這些已被 Workflow 取代，但概念不變。Workflow 提供更強大的圖結構控制。

## 資深工程師的設計洞察

### 1. 為什麼 Agent 繼承 BaseNode？

這是 **開放封閉原則** 的體現。Agent 可以在兩種場景中使用：
- 獨立運行（透過 Runner）
- 作為 Workflow 的一個節點

不需要寫轉接器，因為介面已經統一。

### 2. 為什麼有回呼系統而不是用 Middleware？

回呼更加聲明式。你在定義 Agent 時就聲明了它的行為修改，而不是在某個遠離的地方註冊 middleware。這讓 Agent 的定義自包含。

### 3. 為什麼模型用繼承解析？

因為在多 Agent 系統中，大部分子 Agent 會用相同的模型。繼承避免了重複配置，同時保留了覆寫的彈性。

## 下一步

接下來看 Flow 執行引擎——Agent 的「大腦」是怎麼一步步思考和行動的。
