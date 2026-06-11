# 05 - Workflow 工作流

## 為什麼需要 Workflow？

LLM Agent 的行為是 **非確定性** 的——你給它同樣的輸入，它可能走不同的路徑。

但有些場景你需要 **確定性**：
- 「一定要先驗證身份，才能查詢資料」
- 「搜尋結果一定要經過摘要，再經過品質檢查」
- 「三個 Agent 必須同時執行，全部完成後才彙整」

**Workflow = 確定性的圖結構執行引擎**

## Workflow vs Agent 的差別

| 特性 | LLM Agent | Workflow |
|------|-----------|---------|
| 控制流 | LLM 決定 | 你定義 |
| 確定性 | 不確定 | 確定 |
| 適用場景 | 開放式對話 | 結構化流程 |
| 路由 | LLM 判斷 | 條件/規則 |
| 可預測性 | 低 | 高 |

## 基本用法

```python
from google.adk import Agent, Workflow

# 定義兩個專業 Agent
search_agent = Agent(
    name="search",
    instruction="搜尋相關資訊，只回傳搜尋結果",
)

summarize_agent = Agent(
    name="summarize", 
    instruction="把搜尋結果整理成簡潔的摘要",
)

# 用 Workflow 串接：搜尋 → 摘要
root_agent = Workflow(
    name="research_flow",
    edges=[("START", search_agent, summarize_agent)],
)
```

## 核心概念

### Node（節點）

Workflow 中每個步驟就是一個 Node：

```python
# Agent 可以當 Node
agent_node = Agent(name="step1", ...)

# 函式也可以當 Node
@function_node
async def validate(ctx, input):
    if input.score < 0.8:
        return "retry"
    return "done"
```

### Edge（邊）

邊定義了節點之間的連接：

```python
edges = [
    # 簡單序列：START → A → B → C
    ("START", agent_a, agent_b, agent_c),
    
    # 分支：B 可以去 C 或 D
    (agent_b, agent_c),  # B → C
    (agent_b, agent_d),  # B → D（根據 B 的輸出路由）
]
```

### Route（路由）

節點可以決定下一步去哪裡：

```python
# Agent 透過 output 決定路由
quality_checker = Agent(
    name="quality_check",
    instruction="評估品質。如果品質好回傳 'pass'，不好回傳 'retry'",
    output_schema={"route": "pass | retry"},
)

# 根據 output 路由
edges = [
    ("START", search_agent, quality_checker),
    (quality_checker, "pass", final_agent),    # 品質好 → 結束
    (quality_checker, "retry", search_agent),  # 品質差 → 重新搜尋
]
```

## Workflow 的架構

**檔案位置：** `src/google/adk/workflow/`

```
Workflow (_workflow.py)
├── 管理 Node 集合
├── 管理 Edge 連接
├── 追蹤執行狀態
└── 協調 NodeRunner 執行

NodeRunner (_node_runner.py)
├── 執行單一 Node
├── 處理 Retry 邏輯
├── 處理 Timeout
└── 管理 Node 狀態

BaseNode (_base_node.py)
├── name: 節點名稱
├── input_schema: 輸入驗證
├── output_schema: 輸出驗證
├── retry_config: 重試策略
├── timeout: 超時設定
└── _run_impl(): 執行邏輯
```

## 進階功能

### 1. Fan-out / Fan-in（扇出/扇入）

```python
# 三個 Agent 同時執行，全部完成後彙整
analyst_1 = Agent(name="market_analyst", ...)
analyst_2 = Agent(name="tech_analyst", ...)
analyst_3 = Agent(name="risk_analyst", ...)
synthesizer = Agent(name="synthesizer", ...)

workflow = Workflow(
    name="parallel_analysis",
    edges=[
        ("START", analyst_1),
        ("START", analyst_2),
        ("START", analyst_3),
        (analyst_1, synthesizer),
        (analyst_2, synthesizer),
        (analyst_3, synthesizer),  # synthesizer 等所有分析完成
    ],
)
```

### 2. 迴圈（Loop）

```python
# 反覆改善直到品質達標
writer = Agent(name="writer", instruction="寫文章")
reviewer = Agent(name="reviewer", instruction="審閱，回傳 'approved' 或 'revise'")

workflow = Workflow(
    name="writing_loop",
    edges=[
        ("START", writer, reviewer),
        (reviewer, "revise", writer),     # 需要修改 → 回到 writer
        (reviewer, "approved", "END"),    # 通過 → 結束
    ],
)
```

### 3. 條件路由

```python
# 根據用戶類型走不同流程
router = Agent(
    name="router",
    instruction="判斷用戶需求類型，回傳 'technical' 或 'billing' 或 'general'",
)

workflow = Workflow(
    name="support_flow",
    edges=[
        ("START", router),
        (router, "technical", tech_agent),
        (router, "billing", billing_agent),
        (router, "general", general_agent),
    ],
)
```

### 4. Retry 重試

```python
from google.adk.workflow import RetryConfig

flaky_agent = Agent(
    name="api_caller",
    retry_config=RetryConfig(
        max_retries=3,
        backoff_factor=2.0,  # 指數退避
    ),
)
```

### 5. 動態節點

```python
# 在運行時動態新增節點
async def dynamic_handler(ctx, input):
    # 根據輸入決定要跑哪些子任務
    for item in input.items:
        ctx.schedule_dynamic_node(
            Agent(name=f"process_{item.id}", ...),
            input=item,
        )
```

## Workflow 的執行模型

```
Workflow 開始
    │
    ▼
[START] ─── 觸發初始邊 ───→ [Node A]
                                │
                            NodeRunner 執行：
                            1. 驗證 input_schema
                            2. 執行 _run_impl()
                            3. 處理 timeout/retry
                            4. 提取 output
                                │
                                ▼
                        根據 output 選擇邊 ───→ [Node B] 或 [Node C]
                                                    │
                                                    ▼
                                              ...繼續直到沒有後續邊
```

### 狀態恢復（Replay）

Workflow 支援從中斷處恢復：

```python
# Workflow 會記錄每個 Node 的執行結果到 Session Events
# 當 Session 恢復時：
# 1. 讀取已完成的 Node 結果
# 2. 跳過已完成的 Node
# 3. 從中斷點繼續執行
```

**資深工程師觀點：** 這就是 Event Sourcing 模式。所有狀態變更都是 Event，恢復就是重放 Event。

## Workflow vs 舊版組合 Agent

| ADK 1.x | ADK 2.0 | 差別 |
|---------|---------|------|
| SequentialAgent | Workflow (線性邊) | Workflow 更靈活 |
| ParallelAgent | Workflow (Fan-out) | Workflow 支援 Join |
| LoopAgent | Workflow (循環邊) | Workflow 支援條件退出 |

## 設計洞察

### 1. 為什麼 Agent 可以直接當 Workflow 節點？

因為 `BaseAgent` 繼承 `BaseNode`。不需要轉接器，直接插入 Workflow 圖中。這是介面統一的好處。

### 2. 為什麼需要 NodeRunner？

**單一職責原則**：
- Node 只管「做什麼」
- NodeRunner 管「怎麼執行」（重試、超時、狀態追蹤）

### 3. 什麼時候用 Workflow，什麼時候用 Agent 轉移？

| 場景 | 建議 |
|------|------|
| 固定流程，步驟明確 | Workflow |
| 動態判斷，需要靈活性 | Agent 轉移 |
| 需要重試/超時控制 | Workflow |
| 需要 Fan-out/Fan-in | Workflow |
| 開放式多輪對話 | Agent 轉移 |
| 混合場景 | Workflow + Agent 節點 |

### 4. Workflow 和 Airflow/Temporal 的差別？

ADK Workflow 是 **Agent-native** 的：
- 節點可以是 LLM Agent（非確定性）
- 路由可以基於 LLM 的判斷
- 內建 Session 和 State 管理
- 和 ADK 的事件系統深度整合

傳統工作流引擎是為確定性任務設計的，ADK Workflow 是為 AI Agent 設計的。

## 下一步

接下來看 Session 和 State 管理——Agent 如何記住對話。
