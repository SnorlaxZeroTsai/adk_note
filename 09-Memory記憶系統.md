# 09 - Memory 記憶系統

## Session vs Memory

| 維度 | Session | Memory |
|------|---------|--------|
| 範圍 | 一次對話 | 跨多次對話 |
| 生命週期 | 對話開始到結束 | 永久 |
| 內容 | 完整事件歷史 | 精選的重要資訊 |
| 用途 | 維持對話上下文 | 長期學習/回憶 |

**比喻：**
- Session = 你正在進行的會議筆記
- Memory = 你從過去所有會議中學到的知識

## Memory 的架構

**檔案位置：** `src/google/adk/memory/`

```python
class BaseMemoryService(ABC):
    # 寫入記憶
    async def add_session_to_memory(self, session: Session):
        """把整個 Session 的重要資訊存入記憶"""
        ...
    
    async def add_events_to_memory(self, events: list[Event], ...):
        """增量式：只加入新的事件"""
        ...
    
    async def add_memory(self, app_name, user_id, content: str, ...):
        """直接寫入一條記憶"""
        ...
    
    # 讀取記憶
    async def search_memory(self, app_name, user_id, query: str) -> list[MemoryEntry]:
        """根據查詢搜尋相關記憶"""
        ...
```

## MemoryEntry：記憶的基本單位

```python
class MemoryEntry:
    content: str           # 記憶內容
    timestamp: float       # 何時產生
    metadata: dict         # 額外資訊
    source_session_id: str # 來自哪個 Session
```

## Memory 的實作

### 1. InMemoryMemoryService（開發用）

```python
# 純記憶體實作
# - 支援簡單的關鍵字搜尋
# - 重啟就消失
# - 適合開發和測試
```

### 2. VertexAIMemoryBankService（生產用）

```python
# 使用 Google Vertex AI 的 Memory Bank
# - 自動摘要和索引
# - 語意搜尋（不只是關鍵字）
# - 自動管理記憶的生命週期
# - 適合生產環境
```

### 3. VertexAIRagMemoryService（進階）

```python
# 使用 Vertex AI RAG（Retrieval-Augmented Generation）
# - 向量檢索
# - 大規模記憶（數百萬條）
# - 適合需要大量知識的 Agent
```

## Memory 的使用流程

```
對話結束
    │
    ▼
Runner 呼叫 memory_service.add_session_to_memory(session)
    │  (從 Session 中提取重要資訊)
    ▼
記憶被索引和儲存
    │
    ▼
下一次對話開始
    │
    ▼
Agent 透過工具搜尋記憶
    │  memory_service.search_memory(query="用戶偏好")
    ▼
相關記憶注入到 Agent 的上下文中
```

## 在 Agent 中使用 Memory

### 方式 1：透過工具手動搜尋

```python
from google.adk.tools import LoadMemoryTool

agent = Agent(
    name="assistant",
    tools=[LoadMemoryTool()],  # Agent 可以主動搜尋記憶
    instruction="如果需要回憶之前的對話，使用 load_memory 工具搜尋。",
)
```

### 方式 2：預載入記憶

```python
from google.adk.tools import PreloadMemoryTool

agent = Agent(
    name="assistant",
    tools=[PreloadMemoryTool()],  # 每次對話開始前自動載入相關記憶
)
```

### 方式 3：在工具中存取

```python
async def personalized_response(query: str, tool_context: ToolContext):
    # 搜尋相關記憶
    memories = await tool_context.search_memory(query)
    
    # 根據記憶客製化回應
    context = "\n".join([m.content for m in memories])
    return f"根據你之前的偏好：{context}\n\n回答：..."
```

## Memory 的設計模式

### 模式 1：漸進式學習

```python
# 每次對話結束，Agent 學到新知識
# 第 1 次對話：用戶喜歡簡短回答
# 第 2 次對話：用戶是 Python 工程師
# 第 3 次對話：用戶在做電商專案

# 之後的對話中，Agent 可以綜合這些記憶
# → 用簡短的 Python 範例回答電商問題
```

### 模式 2：知識積累

```python
# Agent 把研究結果存入記憶
# 下次遇到類似問題不需要重新搜尋

async def research(topic: str, tool_context: ToolContext):
    # 先搜尋記憶
    existing = await tool_context.search_memory(topic)
    if existing:
        return f"之前研究過：{existing[0].content}"
    
    # 沒有記憶，做新研究
    result = await do_research(topic)
    
    # 存入記憶供未來使用
    # (會在 Session 結束時自動存入)
    tool_context.state[f"research:{topic}"] = result
    return result
```

### 模式 3：用戶 Profile 建構

```
對話 1: "我在台北工作"
    → Memory: user_location=台北

對話 2: "幫我找附近餐廳"
    → 搜尋 Memory → 知道用戶在台北
    → 搜尋台北的餐廳（不用再問在哪裡）
```

## 設計洞察

### 1. 為什麼 Memory 是獨立於 Session 的？

**不同的生命週期、不同的儲存需求：**
- Session：完整保留每一條訊息（可能很大）
- Memory：只保留 **重要的、可搜尋的** 資訊

如果把所有 Session 歷史直接當 Memory，搜尋效果會很差（太多噪音）。

### 2. 為什麼用語意搜尋而不是精確匹配？

```python
# 用戶之前說："我不喜歡太長的回答"
# 這次搜尋："用戶的回答偏好"

# 精確匹配：找不到（沒有「偏好」這個字）
# 語意搜尋：找到！（意思相近）
```

### 3. 為什麼 add_session_to_memory 不是自動的？

因為不是所有對話都值得記住。你可以控制：
- 哪些 Session 要存入記憶
- 存入前要做什麼處理（摘要？過濾？）
- 存的頻率（每次對話？每天彙整？）

### 4. Memory 的挑戰

| 挑戰 | 解決方向 |
|------|----------|
| 記憶太多，搜尋慢 | 向量索引 + 語意搜尋 |
| 記憶過時 | TTL + 時間衰減 |
| 記憶衝突 | 時間戳排序，新覆蓋舊 |
| 隱私 | 用戶級隔離 + 刪除 API |
| 幻覺記憶 | 記憶來源追蹤（source_session_id） |

## Session + State + Memory 的完整圖景

```
┌─────────────────────────────────────────────────┐
│                    Memory                        │
│  (跨所有 Session 的長期知識)                      │
│  ┌──────────────────────────────────────────┐   │
│  │              Session A                    │   │
│  │  ┌────────────────────────────────────┐  │   │
│  │  │  State (對話中的即時狀態)            │  │   │
│  │  └────────────────────────────────────┘  │   │
│  │  Events: [msg1, msg2, tool_call, msg3]   │   │
│  └──────────────────────────────────────────┘   │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │              Session B                    │   │
│  │  ┌────────────────────────────────────┐  │   │
│  │  │  State (另一次對話的狀態)            │  │   │
│  │  └────────────────────────────────────┘  │   │
│  │  Events: [msg1, msg2, msg3]              │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

## 下一步

接下來看整合與部署——MCP、OpenAPI、CLI 和雲端部署的全貌。
