# 03 - Flow 執行引擎

## Flow 是什麼？

Flow 是 Agent 的「大腦執行邏輯」。當 Agent 收到用戶訊息時，Flow 負責：
1. 準備 LLM 請求（組裝 context、指令、工具）
2. 呼叫 LLM
3. 處理 LLM 回應（執行工具、轉移 Agent）
4. 決定是否需要再次呼叫 LLM

**檔案位置：** `src/google/adk/flows/llm_flows/`

## 兩種 Flow

```
BaseLlmFlow (抽象基底)
    ├── SingleFlow   ← Agent 沒有子 Agent 時使用
    └── AutoFlow     ← Agent 有子 Agent 時使用（支援轉移）
```

## 處理器管線（Processor Pipeline）

Flow 的核心設計是 **處理器管線**。每個請求經過一連串處理器，每個處理器負責一個特定的任務：

```
用戶訊息 → [處理器1] → [處理器2] → ... → [處理器N] → LLM 請求
LLM 回應 → [處理器1] → [處理器2] → ... → 最終回應
```

### SingleFlow 的請求處理器（按順序）

| 順序 | 處理器 | 職責 |
|------|--------|------|
| 1 | `basic` | 基礎請求設定（模型名稱等） |
| 2 | `auth_preprocessor` | 處理認證相關邏輯 |
| 3 | `request_confirmation` | 工具確認提示（需要用戶同意？） |
| 4 | `instructions` | 注入系統指令 |
| 5 | `identity` | 設定用戶/Agent 身份 |
| 6 | `compaction` | 事件壓縮優化（太長的歷史） |
| 7 | `contents` | 從歷史中準備對話內容 |
| 8 | `context_cache` | 快取管理 |
| 9 | `interactions` | 互動 API 狀態追蹤 |
| 10 | `nl_planning` | 自然語言規劃（思考鏈） |
| 11 | `code_execution` | 程式碼執行優化 |
| 12 | `output_schema` | 結構化輸出強制 |

### AutoFlow 額外加上

| 處理器 | 職責 |
|--------|------|
| `agent_transfer` | 處理 Agent 轉移邏輯 |

## 請求處理器的運作方式

```python
class RequestProcessor:
    async def run(self, context, llm_request):
        """修改 LLM 請求"""
        # 例如：instruction 處理器把系統指令加到請求中
        llm_request.config.system_instruction = agent.instruction
```

每個處理器都可以修改 `llm_request`，最終的請求是所有處理器疊加的結果。

### 範例：instructions 處理器做了什麼

```python
# 偽碼
async def run(self, ctx, llm_request):
    # 1. 取得 Agent 的指令（可能是靜態或動態）
    instruction = agent.get_instruction(ctx)

    # 2. 加入全域指令（如果有）
    global_instruction = ctx.run_config.global_instruction

    # 3. 組合後設定到請求中
    llm_request.config.system_instruction = combine(instruction, global_instruction)
```

### 範例：compaction 處理器做了什麼

```python
# 當對話歷史太長時，自動壓縮
async def run(self, ctx, llm_request):
    if token_count(llm_request.contents) > threshold:
        # 把舊的對話歷史摘要化
        summary = await summarize(old_contents)
        llm_request.contents = [summary] + recent_contents
```

**資深工程師觀點：** 這就是 **Chain of Responsibility** 模式。每個處理器只關心自己的領域，互不干擾。新增功能 = 新增處理器，不需要修改現有程式碼。

## 執行迴圈

Flow 不只呼叫一次 LLM。它運行一個迴圈：

```
while True:
    1. 經過所有請求處理器
    2. 呼叫 LLM
    3. 處理回應
       - 如果 LLM 回傳文字 → 結束（回傳給用戶）
       - 如果 LLM 回傳工具呼叫 → 執行工具 → 把結果加入歷史 → 繼續迴圈
       - 如果 LLM 要求轉移 Agent → 執行轉移 → 結束此 Flow
```

### 視覺化

```
用戶: "幫我查台北天氣"

[迴圈第1次]
  處理器管線 → LLM
  LLM 回應: function_call(get_weather, city="台北")
  執行工具 → 結果: "台北 28°C 晴天"

[迴圈第2次]
  處理器管線（包含工具結果）→ LLM
  LLM 回應: "台北目前天氣是 28°C，晴天！"
  是文字回應 → 結束迴圈，回傳給用戶
```

## 串流（Streaming）

ADK 使用 **AsyncGenerator** 來做串流：

```python
async def run_async(self, ctx) -> AsyncGenerator[Event, None]:
    """每產生一個事件就 yield 出去"""
    # LLM 的串流回應也是一個個 Event
    async for chunk in llm.generate_content_async(request, stream=True):
        yield Event(content=chunk, partial=True)   # 部分回應
    yield Event(content=final_response, partial=False)  # 完整回應
```

**資深工程師觀點：** AsyncGenerator 是 Python 中做串流的最佳實踐。它讓 Flow 可以「邊執行邊回傳」，用戶看到的是即時打字效果。

## AutoFlow：Agent 轉移如何運作

```python
# AutoFlow 在 SingleFlow 基礎上加了轉移邏輯

# 當 LLM 回傳 transfer_to_agent("coder") 時：
1. AutoFlow 識別出這是轉移請求
2. 在 Agent 樹中找到 "coder" Agent
3. 發出 Event(actions=EventActions(transfer_to_agent="coder"))
4. Runner 收到此 Event，切換到 "coder" Agent 繼續執行
```

## 即時模式（Live Mode）

除了文字對話，Flow 也支援即時音訊/視訊串流：

```python
async def run_live(self, ctx) -> AsyncGenerator[Event, None]:
    """雙向即時串流（音訊/視訊）"""
    # 用於語音對話、視訊分析等場景
```

## 設計模式總結

| 模式 | 應用位置 | 效果 |
|------|----------|------|
| Chain of Responsibility | 處理器管線 | 可擴展的請求/回應處理 |
| Strategy Pattern | SingleFlow vs AutoFlow | 根據配置選擇演算法 |
| AsyncGenerator | 串流事件 | 非同步、即時的事件流 |
| Template Method | BaseLlmFlow | 定義骨架，子類填充細節 |

## 為什麼這樣設計？

1. **處理器管線** 讓你可以在任何環節插入邏輯（快取、壓縮、認證），而不破壞核心流程
2. **迴圈執行** 讓 Agent 能做多步驟推理（想 → 做 → 觀察 → 再想）
3. **串流架構** 讓用戶體驗更好（不用等整個回應完成）
4. **Flow 與 Agent 分離** 讓同一個 Agent 可以在不同 Flow 模式下運行

## 下一步

接下來看 Tool 工具系統——Agent 如何與外部世界互動。
