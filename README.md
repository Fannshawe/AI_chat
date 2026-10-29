# Persona Chat

一个在 Windows 本地运行的恋爱聊天 AI：读取对方的消息后，结合固定 Persona、情绪、六维关系状态、分类长期记忆和聊天技巧文档，生成 3 个可编辑候选回复。候选只有在页面点击“确认已发送”后才进入真实聊天记录。

## 已实现

- FastAPI 本地服务与现代化聊天页面
- 独立管理后台：Persona YAML、Core 提案、记忆、情绪、关系、指导文档、导入导出、模型切换
- LangGraph 工作流：上下文（预加载 Persona Core + Personality） → User Emotion → Relationship → Persona Habit Retrieval → Memory Planner → Memory Retrieval → AI Emotion Update → 技巧文档 → Dialogue Composer → Prompt Composer → Reply Critic → Reflection → 保存
- 每个 LLM 节点保留独立的 LangChain v1 `create_agent()`，节点直接调用 `agent.invoke()`
- Pydantic 结构化输出，交给 LangChain 自动优先选择 `ProviderStrategy`，不支持时再回退；结果从 `structured_response` 读取，不再手工解析模型 JSON
- `ModelRetryMiddleware` 处理瞬时模型故障，`ModelCallLimitMiddleware` 限制单节点调用次数
- 最近短期上下文
- SQLite 长期记忆与可读 JSON 全量导出
- Persona YAML 模板：Core 与 Personality 在上下文加载阶段固定注入，Habit 作为按需检索层由 Persona Habit Retrieval 选择性注入；Habit 可由 Reflection 自动更新
- 四类长期记忆：episodic、semantic、emotional、shared_experience
- 六维关系状态：trust、familiarity、intimacy、attraction、attachment、comfort
- 六阶段关系状态机：陌生人、认识、朋友、暧昧、恋人；严重事件才允许降级
- User Emotion 与 AI Emotion 分离：用户情绪保留旧字段输出，AI Emotion 作为持久内部状态单独更新
- AI Emotion 持久化：Mood、Energy 与多维短期 Emotion 按轮保存，并记录 Conversation Event
- Markdown 指导文档本地关键词检索
- 纯文本、图片输入和历史聊天导入
- 主动聊天草稿（不自动发送）
- OpenAI 兼容模型供应商切换
- Memory Planner：先决定检索类型、查询、关键词、优先级、Top K 和时间范围，再本地检索
- Dialogue Composer：只产出目标、策略、结构、记忆用法、人格表达、情绪表达、话题规划和限制
- Prompt Composer：唯一生成最终候选的节点，并接收 Critic 修改建议重写
- Reply Critic：按 Style、Persona、Memory、AI 味、采访味、过度共情和自然度评审，最多触发两次重写
- Reflection：决定保存哪些长期记忆、自动更新哪些 Habit、提出哪些 Core 修改候选
- 候选选择反馈：只记录最终选择的候选序号，不额外保存编辑过程

## 启动

1. 保持现有 `.env` 中以下变量可用：
   - `AGENT_EMOTION_BASE_URL` / `AGENT_EMOTION_API_KEY` / `AGENT_EMOTION_MODEL`
   - `AGENT_SELF_EMOTION_BASE_URL` / `AGENT_SELF_EMOTION_API_KEY` / `AGENT_SELF_EMOTION_MODEL`
   - `AGENT_RELATIONSHIP_BASE_URL` / `AGENT_RELATIONSHIP_API_KEY` / `AGENT_RELATIONSHIP_MODEL`
   - `AGENT_MEMORY_PLANNER_BASE_URL` / `AGENT_MEMORY_PLANNER_API_KEY` / `AGENT_MEMORY_PLANNER_MODEL`
   - `AGENT_DIALOGUE_COMPOSER_BASE_URL` / `AGENT_DIALOGUE_COMPOSER_API_KEY` / `AGENT_DIALOGUE_COMPOSER_MODEL`
   - `AGENT_PROMPT_COMPOSER_BASE_URL` / `AGENT_PROMPT_COMPOSER_API_KEY` / `AGENT_PROMPT_COMPOSER_MODEL`
   - `AGENT_REWRITE_BASE_URL` / `AGENT_REWRITE_API_KEY` / `AGENT_REWRITE_MODEL`
   - `AGENT_CRITIC_BASE_URL` / `AGENT_CRITIC_API_KEY` / `AGENT_CRITIC_MODEL`
   - `AGENT_REFLECTION_BASE_URL` / `AGENT_REFLECTION_API_KEY` / `AGENT_REFLECTION_MODEL`
   - `AGENT_PROACTIVE_BASE_URL` / `AGENT_PROACTIVE_API_KEY` / `AGENT_PROACTIVE_MODEL`
   - 可选：每个节点独立 `AGENT_<NAME>_ENABLE_THINKING=true|false`
   - `WORKFLOW_DEBUG`：是否在终端输出 `prompt_composer` 候选词、`critic` 分析结果、`prompt_rewriter` 输出候选词
   - `CRITIC_MAX_ATTEMPTS`：Reply Critic 最大评审次数，默认 `3`
2. 安装依赖：`pip install -r requirements.txt`
3. 启动：`python app.py`
4. 浏览器打开 `http://127.0.0.1:8000`
5. 管理后台：`http://127.0.0.1:8000/admin`

也可以用 `uvicorn app:app --host 127.0.0.1 --port 8000` 启动。

启动后直接进入聊天页面，无需注册或登录。数据统一保存在根目录 `data/`，Persona 文件为 `data/persona.yaml`。旧 `users/` 目录保留但不再读取；如需沿用其中某个用户的数据，请先备份，再将该用户的 `data/` 内容迁移到根目录 `data/`，不要直接覆盖需要保留的数据。

## 首次使用建议

1. 先在管理后台编辑 `data/persona.yaml`，填写 Core、Personality、Habit 和表达示例。Core 与 Personality 不会被自动修改。
2. 导入历史聊天，格式为每行 `我: 内容` 或 `对方: 内容`。导入后会分批调用正常聊天使用的 Reflection，从对方的真实消息中提取有长期价值的 Contact Memory 并去重保存；页面显示新增记忆数并刷新记忆列表。不会生成新回复或重放历史情绪、关系变化。若模型不可用，聊天记录仍保留，页面会提示记忆提取失败。
3. 在“指导文档”中维护聊天技巧。关系行为边界由 Relationship State 每轮动态生成，不由技巧文档直接决定。
4. 回到聊天页，粘贴对方最新消息并生成候选；“暂不回复”也可能作为一个候选策略。

## 导入长期记忆

在管理后台的“长期记忆”页点击“导入 AI Memory”或“导入 Contact Memory”，选择 `.yaml`、`.yml` 或 `.json` 文件。导入为追加操作，不覆盖已有记忆；同分类、同内容的记忆会跳过，并显示导入和跳过数量。文件大小最多 1 MB，每条内容最多 2000 字。

AI Memory 支持 episodic、semantic、emotional、shared_experience 四类，写入 `data/ai_memory.yaml`。可直接选择 `data/export/05_ai_memory.yaml`，或使用以下格式（列表项也可写成 `content` 对象）：

```yaml
semantic:
   - 我喜欢摄影
shared_experience:
   - content: 我和对方第一次一起看海
```

Contact Memory 只支持前三类，导入到当前联系人。可使用相同的分类格式，或选择 `data/export/04_contact_memories.yaml` 这样的记录列表：

```json
[
   {"category": "semantic", "content": "对方喜欢猫", "sensitive": false},
   {"category": "episodic", "content": "一起去了书店", "sensitive": false}
]
```

Contact Memory 导入保留敏感标记，重新生成记录 ID 和时间，不沿用源联系人 ID 或消息引用。含多个源联系人的文件需要拆分后分别导入。非法分类、空内容或格式错误会在写入前拒绝整个文件；全量数据导出的 JSON 不是记忆专用文件，请先取出对应记忆部分。

## 新回复流程

当前主流程为：上下文（预加载 Persona Core + Personality） → User Emotion → Relationship → Persona Habit Retrieval → Memory Planner → Memory Retrieval → AI Emotion Update → 聊天技巧文档 → Dialogue Composer → Prompt Composer → Reply Critic → Reflection → 内容保存。

其中 Persona Habit Retrieval 对应 Persona 的第三层 Habit（生活习惯）：它会从 `persona_habits` 中按当前话题筛出 `relevant_habits`，而不是把全部 Habit 一次性塞进 Prompt。

AI Emotion Update 读取 Previous AI Emotion、User Emotion、Relationship、Persona emotionProfile、Conversation Event 与时间衰减信息。User Emotion 只描述对方，不直接修改 AI。

Reply Critic 会增加一次模型调用；若首轮不通过，会在 `CRITIC_MAX_ATTEMPTS` 限制内继续执行“修改建议 → Prompt Composer 重写 → 复评”。仍不通过时返回综合评分最高的候选，并在页面显示质量预警。若 `WORKFLOW_DEBUG=true`，终端还会打印 `prompt_composer` 候选词、`critic` 结构化分析结果，以及重写轮次的 `prompt_rewriter` 候选词。

Reflection 只保存有长期价值且有明确证据的内容。Habit 会自动写回 `persona.yaml`；Core 修改只进入待确认提案；Personality 禁止自动修改。

## 模型供应商

`default` 供应商由 `AGENT_PROMPT_COMPOSER_*` 自动映射。可选的多供应商配置使用 `MODEL_PROVIDERS_JSON`，密钥仍从独立环境变量读取：

```json
{"deepseek":{"model":"deepseek-chat","base_url":"https://api.example.com/v1","api_key_env":"DEEPSEEK_API_KEY"}}
```

管理后台只保存当前供应商名称，不把 API Key 写入 SQLite。

Agent 定义集中在 `agents.py`，并按模型供应商缓存。外层 `graph.py` 仍负责业务拓扑和本地状态流转；各 `create_agent()` 自身也是编译后的 LangGraph，因此中间件会在嵌入外层图后继续生效。

## 数据位置

- SQLite：`data/chat.db`
- 聊天图片：`data/uploads/`
- Persona 模板：`persona.yaml`
- 提示词：`prompt/`
- 指导文档：`guidance_docs/`

`data/` 默认被 Git 忽略。聊天内容可能包含高度私密信息，请不要把数据库、导出文件、上传图片或 `.env` 提交到仓库。

六维关系和变更日志保存在 SQLite；生成快照保存本轮 Memory Plan、Dialogue Composer、Critic 与 Reflection 结果。Core 修改提案保存在 SQLite，批准后写回 `persona.yaml`。

## 图片说明

图片以 OpenAI 兼容的多模态消息格式发送给当前模型。若模型不支持图片，系统会自动退回文字上下文，并在页面提示警告。

## 当前发送语义

首期按需求不连接微信等外部平台。“确认已发送”表示你已在外部聊天工具中发送该候选，系统随后把它记录为真实聊天内容；它本身不会调用外部平台接口。

## 测试

运行 `python -m unittest discover -s tests -v`。测试不调用大模型接口。
