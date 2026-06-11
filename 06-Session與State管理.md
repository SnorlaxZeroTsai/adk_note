# 06 - Session 與 State 管理

## 核心問題

Agent 需要記住對話歷史。但「記住」有很多層次：
- 這次對話說了什麼？ → **Session**
- 這個用戶的偏好是什麼？ → **State**
- 上週的對話內容？ → **Memory**（下一章）

本章專注 Session 和 State。

## Session：一次對話

**檔案位置：** `src/google/adk/sessions/session.py`

```python
class Session:
    id: str            # Session 唯一 ID
    app_name: str      # 應用名稱
    user_id: str       # 用戶 ID
    state: State       # 可變狀態字典
    events: list[Event]  # 對話歷史（所有事件）
    last_update_time: float  # 最後更新時間
```

### Session 的生命週期

```
建立 Session
    │  create_session(app_name, user_id)
    ▼
用戶發送訊息
    │  Runner 把用戶訊息加入 events
    ▼
Agent 回應
    │  Agent 的回應也加入 events
    │  如果有 state 變更，記錄在 event.actions.state_delta
    ▼
Session 持久化
    │  upsert_session() 把變更存到後端
    ▼
下次用戶來
    │  get_session() 取回之前的狀態
    ▼
對話結束
    │  session 保留在儲存中
    └── 可選：add_session_to_memory() 存入長期記憶
```

## State：結構化的狀態

**檔案位置：** `src/google/adk/sessions/state.py`

State 不只是一個 dict，它有特殊的能力：

```python
class State:
    # 像 dict 一樣使用
    state["user_name"] = "小明"
    name = state.get("user_name")
    
    # 但有 delta 追蹤！
    state._delta  # 記錄所有未儲存的變更
```

### State 的三個作用域

```python
# 1. 一般 state：跟 Session 綁定
state["preference"] = "dark_mode"

# 2. app: 前綴：整個 App 共享（跨 Session）
state["app:global_counter"] = 42

# 3. user: 前綴：跟用戶綁定（跨 Session）
state["user:language"] = "zh-TW"

# 4. temp: 前綴：暫時的（不持久化）
state["temp:current_step"] = 3
```

**資深工程師觀點：** 這個作用域設計解決了「狀態該存在哪裡」的問題：
- 對話級 → 一般 key
- 應用級 → `app:` 前綴
- 用戶級 → `user:` 前綴
- 暫時的 → `temp:` 前綴

### Delta 追蹤

```python
# State 追蹤所有變更
state["score"] = 100      # delta: {"score": 100}
state["name"] = "Alice"   # delta: {"score": 100, "name": "Alice"}

# 當 Event 產生時，delta 被寫入 event.actions.state_delta
# 然後 delta 被清空

# 這讓 ADK 知道「這次 Agent 執行改了什麼」
# 用於：
# - 增量持久化（只存改變的部分）
# - Event Sourcing（重播事件恢復狀態）
# - 除錯（查看每步改了什麼）
```

### Schema 驗證

```python
from pydantic import BaseModel

class UserProfile(BaseModel):
    name: str
    age: int
    preferences: list[str]

# 建立有 schema 的 Session
session = Session(
    state_schema=UserProfile,
    state={"name": "Alice", "age": 30, "preferences": ["coding"]}
)

# 現在 state 會驗證寫入的值
state["age"] = "不是數字"  # 驗證錯誤！
state["unknown_field"] = 1  # 驗證錯誤！

# 但 app:, user:, temp: 前綴的 key 繞過驗證
state["temp:debug"] = "anything"  # OK
```

## SessionService：Session 的持久化

**檔案位置：** `src/google/adk/sessions/base_session_service.py`

```python
class BaseSessionService(ABC):
    async def create_session(self, app_name, user_id, ...) -> Session
    async def get_session(self, app_name, user_id, session_id) -> Session
    async def list_sessions(self, app_name, user_id) -> list[Session]
    async def upsert_session(self, session) -> Session
    async def delete_session(self, app_name, user_id, session_id)
```

### 四種實作

| 實作 | 適用場景 | 說明 |
|------|----------|------|
| `InMemorySessionService` | 開發/測試 | 資料存在記憶體，重啟就沒了 |
| `DatabaseSessionService` | 生產（自管資料庫） | 用 SQLAlchemy 支援各種 DB |
| `VertexAiSessionService` | 生產（GCP） | Google Vertex AI 管理 |
| `FirestoreSessionService` | 生產（GCP） | Firestore NoSQL |

### 選擇指南

```
開發階段 → InMemorySessionService（零設定）
小規模生產 → DatabaseSessionService + SQLite
中等規模 → DatabaseSessionService + PostgreSQL
大規模/GCP → VertexAiSessionService 或 FirestoreSessionService
```

## State 在工具中的使用

```python
async def checkout(items: list[str], tool_context: ToolContext):
    """結帳功能"""
    # 讀取 state
    cart = tool_context.state.get("cart", [])
    user_id = tool_context.state.get("user:id")
    
    # 更新 state
    tool_context.state["cart"] = []  # 清空購物車
    tool_context.state["user:order_count"] = (
        tool_context.state.get("user:order_count", 0) + 1
    )
    
    return f"已結帳 {len(items)} 件商品"
```

## Event 與 State 的關係

每個 Event 可以攜帶 state_delta：

```python
event = Event(
    author="shopping_agent",
    content=Part(text="已加入購物車"),
    actions=EventActions(
        state_delta={"cart": ["item1", "item2"], "temp:step": "added"}
    ),
)
```

**重播 State 的機制：**

```
Event 1: state_delta = {"name": "Alice"}
Event 2: state_delta = {"age": 30}
Event 3: state_delta = {"name": "Bob"}  ← 覆寫

最終 State = {"name": "Bob", "age": 30}
```

這就是 Event Sourcing：State 是所有 Event 的 delta 累積結果。

## 設計洞察

### 1. 為什麼不直接用 dict？

因為你需要：
- **追蹤變更**（delta）用於增量儲存
- **作用域**（app/user/temp）用於不同生命週期的資料
- **驗證**（schema）防止 Agent 寫入垃圾資料
- **隔離**（不同 Agent 的 state 不互相干擾）

### 2. 為什麼 SessionService 是抽象的？

**依賴反轉原則**：Agent 的邏輯不應該依賴具體的儲存方式。
- 開發時用 InMemory，快速迭代
- 測試時用 InMemory，不需要資料庫
- 生產時切換到 Database，不改一行 Agent 程式碼

### 3. 為什麼有 Event Sourcing？

好處：
- **可審計**：知道 State 是怎麼一步步變成現在這樣的
- **可恢復**：重播 Event 就能恢復任何時間點的 State
- **可除錯**：每個 Event 都有 author，知道誰改了什麼
- **Workflow 恢復**：中斷的 Workflow 可以從 Event 中恢復進度

### 4. 多 Agent 的 State 隔離

```
Root Agent 的 State
├── 所有 Agent 共享基礎 state
├── Agent A 透過 temp: 存暫時資料
└── Agent B 透過 temp: 存暫時資料（互不影響）
```

在 Workflow 中，每個 Node 有自己的 scope，避免節點之間意外覆寫。

## 實用模式

### 模式 1：用 State 做對話流程控制

```python
agent = Agent(
    instruction=lambda ctx: (
        "收集用戶資訊。"
        + ("已有姓名。" if ctx.state.get("name") else "請問姓名。")
        + ("已有電話。" if ctx.state.get("phone") else "請問電話。")
        + ("資訊完整，可以建立帳號。" if ctx.state.get("name") and ctx.state.get("phone") else "")
    ),
)
```

### 模式 2：用 State 做進度追蹤

```python
async def process_step(step_name: str, tool_context: ToolContext):
    completed = tool_context.state.get("completed_steps", [])
    completed.append(step_name)
    tool_context.state["completed_steps"] = completed
    tool_context.state["progress"] = f"{len(completed)}/5"
```

## 下一步

接下來看 Event 事件系統——所有資訊流動的載體。
