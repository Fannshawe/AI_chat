# Chat LangGraph: Node / LLM Input-Output

## 表 1：所有 Node 的输入和输出

| Node | 主要输入（来自 state / 外部） | 主要输出（写回 state） |
|---|---|---|
| `context` (`node_load_context`) | `contact_id?` `provider_name?`；外部：`store` | Persona、历史、关系、情绪状态、`critic_attempts`、`critic_analysis` |
| `turn_interpreter` (`node_turn_interpreter`) | `user_request` `history` `recent_emotions` `relationship` | `contact_emotion_analysis` `relationship_analysis` `warnings` `_debug_llm` |
| `persona_habit_retrieval` | `user_request` `persona_habits` | `relevant_habits` |
| `memory_planner` | `user_request` `history` | `memory_plan` `warnings` `_debug_llm` |
| `memory_retrieval` | `contact_id` `memory_plan`；外部：`store.search_memories_by_plan` | `memories` `primary_memories` `background_memories` |
| `self_emotion` | `user_request` `history` `contact_emotion_analysis` `relationship_analysis` `self_emotion_state`；外部：事件库 | `self_emotion_update` `warnings` `_debug_llm` |
| `guidance` | `user_request` `relationship.current_level` `contact_emotion_analysis`；外部：`guidance_retriever` | `guidance_chunks` |
| `response_composer` | Persona、相关 Habit、对方/自身情绪、关系、记忆、技巧文档 | `response_composition` `strategy` `candidates` `warnings` `_debug_llm` |
| `critic` | `user_request` `history` Persona、记忆、`response_composition`、`candidates` | `critic_attempts` `critic_analysis`、必要时替换后的 `candidates` |
| `user_mem_recognition` | 当前 `user_request` 与精确命中的既有记忆；外部：`store.search_memories` | `user_memory_recognition` `warnings` `_debug_llm` |
| `critic_complete` | 最终 Critic 状态 | 空输出，仅作为与用户记忆识别汇合的 barrier |
| `save` | 最终 `candidates` | `candidates`；实际持久化仍在确认发送 API 中执行 |

## 表 2：所有 LLM（Agent）的输入和输出

| Agent | 由哪个节点调用 | LLM 输入（payload 关键字段） | 结构化输出（Schema） |
|---|---|---|---|
| `turn_interpreter` | `node_turn_interpreter` | 最新消息、近期对话/情绪、当前关系 | `TurnInterpreterAnalysis`：独立的 `contact_emotion` 与 `relationship` |
| `self_emotion` | `node_self_emotion_update` | 最新消息、近期对话、情绪、关系、Persona 情绪基线、近期事件 | `SelfEmotionUpdateAnalysis`：`events[]`、`self_emotion_state` |
| `memory_planner` | `node_memory_planner` | 最新消息、近期对话、允许分类 | `MemoryPlan`：`requests[]` |
| `response_composer` | `node_response_composer` | Persona、Habit、情绪、关系、记忆、技巧文档 | `ResponseComposerAnalysis`：`goal/strategy/structure/memory_usage/persona_expression/emotion_expression/topic_plan/length_plan/candidate_directives/constraints` 计划（按此顺序生成，候选前必须先想清楚）+ `candidates[3]` |
| `critic` | `node_reply_critic` | 最新消息、近期对话、Persona、记忆、回复计划、候选 | `ReplyCriticAnalysis`：`reviews[3]`、`passed`；失败项含 `revised_candidate` |
| `user_mem_recognition` | `node_user_memory_recognition` | 本轮真实用户消息、精确命中的既有记忆、允许分类 | `UserMemoryRecognition`：去重后的 `memories[]` |
| `proactive`（非图节点） | `generate_proactive_draft` | 近期对话、关系、Persona、记忆 | `ProactiveDraft`：`draft` `reason` |

## 备注

- 所有 LLM 输入通过 `_run_structured_agent` 封装，实际请求体统一为 `{"messages": [{"role": "user", "content": json(payload)}]}`。
- `turn_interpreter`、Persona Habit Retrieval、Memory Planner 与 User Memory Recognition 在 context 后并行开始；User Memory Recognition 不读取历史聊天，且只接收精确命中的记忆以避免提示词噪声。
- Turn Interpreter 完成后并行触发 Self Emotion 与 Guidance；Self Emotion、Guidance、Habit Retrieval 和 Memory Retrieval 汇合后执行 Response Composer。
- Critic 对失败候选直接改写并自循环复评。`critic_complete` 与 User Memory Recognition 的 fan-in barrier 完成后才进入 `save`。
- `context`、`persona_habit_retrieval`、`memory_retrieval`、`guidance`、`critic_complete`、`save` 不调用 LLM。
