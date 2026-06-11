# 07 - Event 事件系統

## Event 是什麼？

在 ADK 中，**所有發生的事情都是 Event**。它是系統的「血液」——資訊透過 Event 在各元件之間流動。

```
用戶發訊息 → Event
LLM 回應 → Event
工具被呼叫 → Event
工具回傳結果 → Event
Agent 轉移 → Event
State 改變 → Event
```

**資深工程師觀點：** 這是典型的 Event-Driven Architecture (EDA)。好處是解耦、可追蹤、可重播。

## Event 的結構

**檔案位置：** `src/google/adk/events/event.py`

```python
class Event:
    # === 身份識別 ===
    invocation_id: str     # 哪次呼叫產生的
    author: str            # 誰產生的（"user" 或 Agent 名稱）
    branch: str            # Agent 階層路徑 (root.sub1.sub2)
    
    # === 內容 ===
    content: Content       # 訊息內容（文字、圖片、function call）
    partial: bool          # 是否為串流中的部分回應
    
    # === 行動 ===
    actions: EventActions  # 副作用（state 改變、轉移、路由）
    
    # === Workflow 相關 ===
    output: Any            # 節點輸出值
    node_info: NodeInfo    # 節點路徑和執行 ID
    
    # === 元資料 ===
    long_running_tool_ids: list  # 長時間運行的工具
    isolation_scope: str         # 隔離範圍
```

## EventActions：Event 的副作用

```python
class EventActions:
    # State 變更
    state_delta: dict          # {"key": "new_value"}
    
    # Agent 轉移
    transfer_to_agent: str     # 目標 Agent 名稱
    
    # 流程控制
    escalate: bool             # 向上層回報
    route: str                 # Workflow 路由決策
    
    # Agent 狀態快照
    agent_state: dict          # Agent 的 checkpoint
    
    # 認證
    requested_auth_configs: dict  # 請求用戶認證
    
    # 工具控制
    skip_summarization: bool   # 跳過工具結果摘要
```

## Event 流的視覺化

一次典型的對話：

```
時間 →

[Event 1] author="user"
  content: "幫我查台北天氣"

[Event 2] author="weather_agent", partial=True
  content: function_call(get_weather, city="台北")

[Event 3] author="weather_agent"
  content: function_response(get_weather, result="28°C 晴天")
  actions: state_delta={"last_query": "台北天氣"}

[Event 4] author="weather_agent", partial=True
  content: "台北目前..."  (串流中)

[Event 5] author="weather_agent", partial=False
  content: "台北目前天氣是 28°C，晴天！適合出門。"
```

## Branch：追蹤 Agent 階層

```python
# 當 Agent 形成樹狀結構時：
# Root → SubA → SubA1

# Root 產生的 Event:
event.branch = ""  # 或 "root"

# SubA 產生的 Event:
event.branch = "root.sub_a"

# SubA1 產生的 Event:
event.branch = "root.sub_a.sub_a1"
```

**用途：**
- 除錯時知道哪個 Agent 說了什麼
- Session 恢復時知道要恢復哪個 Agent 的上下文
- 事件過濾：只看特定 Agent 的事件

## NodeInfo：Workflow 中的位置

```python
class NodeInfo:
    path: str          # 節點在圖中的路徑
    run_id: str        # 本次執行的 ID
    output_for: list   # 這個 Event 是哪些節點的輸出
    message_as_output: bool  # 內容本身就是輸出
```

**用途：**
- Workflow 恢復時跳過已完成的節點
- 知道哪個節點產生了什麼結果
- Fan-in 時知道等待哪些節點

## Event 的消費者

```
Event 產生
    │
    ├──→ Session: 加入 events 歷史
    │
    ├──→ Runner: 判斷是否需要暫停（long_running_tool）
    │
    ├──→ Memory Service: 選擇性存入長期記憶
    │
    ├──→ Artifact Service: 處理附件
    │
    ├──→ Telemetry: 追蹤和監控
    │
    ├──→ Web UI: 即時顯示給用戶
    │
    └──→ Workflow: 恢復節點狀態
```

## 串流 Event 的模式

ADK 使用 AsyncGenerator 做串流：

```python
# Runner 層級
async for event in runner.run_async(user_id, session_id, message):
    if event.partial:
        # 部分回應 → 即時顯示（打字效果）
        print(event.content.text, end="", flush=True)
    else:
        # 完整回應 → 最終結果
        print(event.content.text)
```

### 為什麼用 AsyncGenerator？

```python
# 對比：傳統回呼方式（不好）
runner.run(message, on_event=callback)  # 控制權在 runner

# AsyncGenerator 方式（好）
async for event in runner.run_async(message):  # 控制權在呼叫者
    ...
```

**好處：**
- 呼叫者決定怎麼處理事件
- 可以隨時中斷
- 可以用 async for 的所有功能（break、continue）
- 自然的背壓（backpressure）控制

## Event Sourcing 模式

ADK 的 State 管理基於 Event Sourcing：

```
Session 初始 State = {}

Event 1: actions.state_delta = {"name": "Alice"}
→ State = {"name": "Alice"}

Event 2: actions.state_delta = {"score": 100}
→ State = {"name": "Alice", "score": 100}

Event 3: actions.state_delta = {"name": "Bob"}
→ State = {"name": "Bob", "score": 100}
```

### 恢復 State

```python
# 當需要恢復某個時間點的 State：
state = {}
for event in session.events[:target_index]:
    if event.actions and event.actions.state_delta:
        state.update(event.actions.state_delta)
```

## Event 在 Workflow 恢復中的角色

```python
# Workflow 被中斷後恢復：

# 1. 讀取 Session 中的所有 Events
events = session.events

# 2. 找到已完成節點的 output
for event in events:
    if event.node_info and event.output:
        completed_nodes[event.node_info.path] = event.output

# 3. 跳過已完成的節點，從中斷點繼續
for node in workflow.nodes:
    if node.path in completed_nodes:
        continue  # 跳過
    await node.run(...)  # 從這裡開始
```

## 設計洞察

### 1. 為什麼所有東西都是 Event？

**統一的資料模型** 帶來的好處：
- 一套 API 處理所有類型的資料
- 一個列表就是完整的執行歷史
- 簡化序列化和持久化
- 統一的監控和追蹤

### 2. 為什麼 Event 是不可變的？

一旦 Event 產生就不會改變。這保證了：
- 歷史不會被竄改
- 多個消費者看到一致的資料
- Event Sourcing 的可靠性

### 3. 為什麼需要 partial Event？

用戶體驗。如果等 LLM 完整回應完再顯示（可能要 10 秒），用戶會覺得系統卡住了。partial Event 讓用戶看到「Agent 正在思考/打字」。

### 4. Event 和 Log 的差別？

| Event | Log |
|-------|-----|
| 結構化 | 文字為主 |
| 是系統行為的一部分 | 純觀察用 |
| 會影響 State | 不影響系統 |
| 被消費和處理 | 被查看 |

Event 是 first-class citizen，不是除錯工具。

## 下一步

接下來看 Model 模型層——ADK 如何支援多個 LLM 供應商。
