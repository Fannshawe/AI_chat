现在我有足够的信息来整理这张表格了。

---

## LangGraph 节点总览

> 节点顺序：context → emotion → relationship → memory → persona → guidance → dialogue_policy → output → critic → save

---

### 1. `node_load_context`（context）

**无 LLM，纯存储读取**

| 项目 | 内容 |
|------|------|
| 输入 | `contact_id`、`provider_name`（来自初始 state） |
| 输出字段 | `history`、`self_persona`、`other_persona`、`relationship`、`recent_emotions`、`life_state`、`life_state_history` |
| Agent / Prompt | 无 |

---

### 2. `node_emotion_detection`（emotion）

| 项目 | 内容 |
|------|------|
| **Payload 输入** | `latest_message`、`recent_conversation`、`recent_emotional_state` |
| **结构化输出** `EmotionAnalysis` | `emotions: list[EmotionItem]`（最多 4 条） |
| `EmotionItem` 字段 | `subject`（我/对方）、`emotion`、`intensity`、`confidence`、`reason` |
| **Prompt** emotion.md | 情绪连续性分析节点；昨日冲突等持续事件可影响判断，但不能盖过新证据；不输出诊断 |

---

### 3. `node_relationship_state`（relationship）

| 项目 | 内容 |
|------|------|
| **Payload 输入** | `allowed_levels`、`current_level`、`latest_message`、`recent_conversation`、`emotion_analysis` |
| **结构化输出** `RelationshipAnalysis` | `suggested_level`、`confidence`、`reason` |
| **Prompt** relationship.md | 关系状态评估节点；阶段固定为 6 级；证据不足时 suggested_level 保持当前；不跨多阶段变化 |

---

### 4. `node_memory_retrieval`（memory）

**无 LLM，纯关键词检索**

| 项目 | 内容 |
|------|------|
| 查询构建 | `user_request` + `relationship_analysis.reason` + `emotion_analysis` 拼接 |
| 输出字段 | `memories: list[dict]`（最多 12 条，来自本地存储） |
| **Prompt** | 无 |

---

### 5. `node_persona_engine`（persona）

| 项目 | 内容 |
|------|------|
| **Payload 输入** | `self_persona`、`counterpart_persona`、`relevant_memories`、`latest_message`、`recent_conversation`、`manually_authored_life_state`、`recent_life_state_history` |
| **结构化输出** `PersonaAnalysis` | `style_instruction`、`counterpart_observations: list[str]`（≤8）、`conflicts: list[str]`（≤5） |
| **Prompt** persona.md | 人物一致性节点；手工档案和示例优先；Life State 仅用于当日语气与可引用事实，禁止扩写不存在经历 |

---

### 6. `node_chat_guidance`（guidance）

| 项目 | 内容 |
|------|------|
| 向量检索 | 用 `user_request + current_level + emotion_analysis` 查询 guidance_retriever（limit=5），结果存入 `guidance_chunks` |
| **Payload 输入** | `latest_message`、`recent_conversation`、`relationship`、`relationship_suggestion`、`emotions`、`retrieved_guidance` |
| **结构化输出** `GuidanceAnalysis` | `strategy`、`tone`、`avoid: list[str]`（≤8）、`document_basis: list[str]`（≤5） |
| **Prompt** guidance.md | 聊天指导节点；指导文档是重要依据；无匹配文档时采用克制的通用策略；只给内部策略，不直接生成候选回复 |

---

### 7. `node_dialogue_policy`（dialogue_policy）

| 项目 | 内容 |
|------|------|
| **Payload 输入** | `latest_message`、`recent_conversation`、`recent_question_density`（最近 6 条中的问句统计）、`current_relationship`、`emotions`、`chat_guidance`、`retrieved_guidance`、`manually_authored_life_state` |
| **结构化输出** `DialoguePolicyAnalysis` | `primary_mode`、`candidate_modes: list[DialogueMode]`（恰好 3 个）、`question_probability`、`question_budget`（0 或 1）、`should_resist`、`rationale` |
| `DialogueMode` 枚举 | 分享自己 / 开玩笑·调侃 / 回忆共同经历 / 简短回应 / 提问 / 转移或发起话题 / 暂不回复 |
| **Prompt** dialogue_policy.md | 独立对话决策节点；根据近期问题密度动态给出 question_probability；should_resist 用于对方过快推进亲密时 |

---

### 8. `node_response_output`（output → "response" agent）

| 项目 | 内容 |
|------|------|
| **Payload 输入** | `latest_message`、`recent_conversation`、`current_relationship`、`emotion_analysis`、`relevant_memories`、`persona_instruction`、`manually_authored_life_state`、`chat_guidance`、`dialogue_policy`、`rewrite_instruction`（空串）、`candidate_count=3` |
| 图片处理 | 若有 `image_data_urls` 则以多模态 content 传入；模型不支持时降级为纯文字并记录 warning |
| **结构化输出** `ResponseCandidates` | `candidates: list[str]`（恰好 3 条） |
| **Prompt** response.md | 最终回复整合节点；按 `dialogue_policy.candidate_modes` 顺序分别生成 3 个候选；不泄露内部分析；不虚构未发生事实 |

---

### 9. `node_reply_critic`（critic）

| 项目 | 内容 |
|------|------|
| **第一轮 Payload** | `latest_message`、`recent_conversation`、`dialogue_policy`、`persona_instruction`、`candidates` |
| **第二轮 Payload**（若 passed=false） | 同上 + `previous_issues`；candidates 替换为重写后的新候选 |
| **结构化输出** `ReplyCriticAnalysis` | `candidates: list[CandidateCritique]`（恰好 3 条）、`passed`、`rewrite_instruction` |
| `CandidateCritique` 字段 | `candidate_index`、`ai_taste`、`interview_taste`、`empathy_excess`、`naturalness`、`passed`、`issues` |
| 触发重写条件 | 任意普通候选前三项 > 7，或自然度 < 5 |
| **Prompt** critic.md | 只评审不改写；检查 AI 味、采访味、过度共情、长度与措辞自然度四项指标 |

---

### 10. `node_content_save`（save）— 含 "memory" agent

| 项目 | 内容 |
|------|------|
| 存储操作 | 保存 user 消息、情绪条目（≤4）、关系分析、generation 记录 |
| **Memory Agent Payload** | `latest_real_message`（speaker + content）、`existing_relevant_memories`、`allowed_categories` |
| **结构化输出** `MemoryAnalysis` | `memories: list[MemoryItem]`（最多 8 条） |
| `MemoryItem` 字段 | `category`（5 类）、`content`、`sensitive` |
| `MemoryCategory` 枚举 | 人物事实与偏好 / 共同经历与纪念日 / 情绪事件与冲突 / 承诺和待办事项 / 敏感信息 |
| **Prompt** memory.md | 长期记忆析取节点；只保存有明确证据的持久事实；收入、性生活、精确位置、证件等标记 sensitive=true；只有确认发送的文字才属于真实聊天 |