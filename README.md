# Persona Chat

一个在 Windows 本地运行的恋爱聊天 AI：读取对方的消息后，结合固定 Persona、情绪、六维关系状态、分类长期记忆和聊天技巧文档，生成 3 个可编辑候选回复。候选只有在页面点击“确认已发送”后才进入真实聊天记录。

## 已实现

- FastAPI 本地服务与现代化聊天页面
- 独立管理后台：Persona YAML、Core 提案、记忆、情绪、关系、指导文档、导入导出、模型切换
- LangGraph 工作流：上下文 → Turn Interpreter（对方情绪 + 关系）→ 并行自身情绪/技巧检索/习惯检索/记忆检索 → Response Composer（计划 + 候选）→ Reply Critic（评审 + 改写 + 复评）；User Memory Recognition 与回复链并行，最终汇合保存
- 每个 LLM 节点保留独立的 LangChain v1 `create_agent()`，节点直接调用 `agent.invoke()`
- Pydantic 结构化输出，交给 LangChain 自动优先选择 `ProviderStrategy`，不支持时再回退；结果从 `structured_response` 读取，不再手工解析模型 JSON
- `ModelRetryMiddleware` 处理瞬时模型故障，`ModelCallLimitMiddleware` 限制单节点调用次数
- 最近短期上下文
- SQLite 长期记忆与可读 JSON 全量导出
- Persona YAML 模板：Core 与 Personality 在上下文加载阶段固定注入，Habit 作为按需检索层由 Persona Habit Retrieval 选择性注入
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
- Turn Interpreter：在同一份上下文中分别输出对方情绪和关系状态判断，避免重复注入聊天历史
- Response Composer：一次输出精简回复计划和 3 个最终候选；策略、记忆用法、人格表达和情绪表达仅进入 debug 快照
- Reply Critic：按 Style、Persona、Memory、AI 味、采访味、过度共情和自然度评审，并直接给出不通过候选的改写版本后复评
- User Memory Recognition：只从本轮真实用户消息识别新记忆，并与检索到的记忆库比对去重
- 候选选择反馈：只记录最终选择的候选序号，不额外保存编辑过程

## 启动

### 单实例模式

1. 保持现有 `.env` 中以下变量可用：
   - `AGENT_TURN_INTERPRETER_BASE_URL` / `AGENT_TURN_INTERPRETER_API_KEY` / `AGENT_TURN_INTERPRETER_MODEL`
   - `AGENT_SELF_EMOTION_BASE_URL` / `AGENT_SELF_EMOTION_API_KEY` / `AGENT_SELF_EMOTION_MODEL`
   - `AGENT_MEMORY_PLANNER_BASE_URL` / `AGENT_MEMORY_PLANNER_API_KEY` / `AGENT_MEMORY_PLANNER_MODEL`
   - `AGENT_RESPONSE_COMPOSER_BASE_URL` / `AGENT_RESPONSE_COMPOSER_API_KEY` / `AGENT_RESPONSE_COMPOSER_MODEL`
   - `AGENT_CRITIC_BASE_URL` / `AGENT_CRITIC_API_KEY` / `AGENT_CRITIC_MODEL`
   - `AGENT_USER_MEM_RECOGNITION_BASE_URL` / `AGENT_USER_MEM_RECOGNITION_API_KEY` / `AGENT_USER_MEM_RECOGNITION_MODEL`
   - `AGENT_PROACTIVE_BASE_URL` / `AGENT_PROACTIVE_API_KEY` / `AGENT_PROACTIVE_MODEL`
   - 可选：每个节点独立 `AGENT_<NAME>_ENABLE_THINKING=true|false`
   - 迁移兼容：未配置新变量时，Turn Interpreter 回退使用 `AGENT_EMOTION_*`，Response Composer 回退使用 `AGENT_PROMPT_COMPOSER_*`，User Memory Recognition 回退使用 `AGENT_REFLECTION_*`
   - `WORKFLOW_DEBUG`：是否在终端输出 Response Composer 的计划/候选和 Critic 分析结果
   - `CRITIC_MAX_ATTEMPTS`：Reply Critic 最大评审次数，默认 `3`
2. 安装依赖：`pip install -r requirements.txt`
3. 启动：`python app.py`
4. 浏览器打开 `http://127.0.0.1:8000`
5. 管理后台：`http://127.0.0.1:8000/admin`

也可以用 `uvicorn app:app --host 127.0.0.1 --port 8000` 启动。

### 多实例网关模式

当需要“一个 8000 入口 + 每个用户一个独立实例”时，启动网关而不是直接启动聊天实例：

1. 运行 `python gateway.py`
2. 浏览器打开 `http://127.0.0.1:8000`
3. 用户在入口页自行注册或登录
4. 网关会自动为每个用户分配固定内部端口，并将请求代理到该用户自己的实例

默认行为：

- 网关端口：`8000`（可用 `GATEWAY_PORT` 覆盖）
- 用户实例端口：从 `8101` 开始固定分配
- 用户数据目录：`users/<用户名>/data/`
- 用户 persona 文件：`users/<用户名>/data/persona.yaml`
- 空闲实例回收：默认 30 分钟，可用 `INSTANCE_IDLE_TIMEOUT_SECONDS` 调整
- 登录保持时长：默认 30 天，可用 `GATEWAY_SESSION_TTL_SECONDS` 调整
- 会话签名密钥：`GATEWAY_SESSION_SECRET`（务必改成你自己的随机字符串）

首个注册用户会自动继承项目当前的旧 `data/` 目录，便于把现有单用户数据迁移为第一个用户实例。

## 首次使用建议

1. 先编辑根目录 `persona.yaml`，填写 Core、Personality、Habit 和表达示例。Core 与 Personality 不会被自动修改。
2. 导入历史聊天，格式为每行 `我: 内容` 或 `对方: 内容`。
3. 在“指导文档”中维护聊天技巧。关系行为边界由 Relationship State 每轮动态生成，不由技巧文档直接决定。
4. 回到聊天页，粘贴对方最新消息并生成候选；“暂不回复”也可能作为一个候选策略。

## 新回复流程

当前主流程为：上下文（预加载 Persona Core + Personality）后，Turn Interpreter、Persona Habit Retrieval、Memory Planner 和 User Memory Recognition 并行启动。Memory Planner 完成本地检索；Turn Interpreter 同时触发 AI Emotion Update 和聊天技巧检索。自身情绪、技巧、习惯和记忆汇合后，由 Response Composer 一次产出计划和候选；Reply Critic 直接改写不通过候选并复评。最终结果与并行的 User Memory Recognition 汇合后保存。

其中 Persona Habit Retrieval 对应 Persona 的第三层 Habit（生活习惯）：它会从 `persona_habits` 中按当前话题筛出 `relevant_habits`，而不是把全部 Habit 一次性塞进 Prompt。

AI Emotion Update 读取 Previous AI Emotion、User Emotion、Relationship、Persona emotionProfile、Conversation Event 与时间衰减信息。User Emotion 只描述对方，不直接修改 AI。

Reply Critic 会增加一次模型调用；若首轮不通过，会在 `CRITIC_MAX_ATTEMPTS` 限制内直接返回改写后的候选并再次复评。仍不通过时返回最后一版候选并记录质量预警。若 `WORKFLOW_DEBUG=true`，终端会打印 Response Composer 的计划/候选和每轮 Critic 结构化分析结果。

User Memory Recognition 只使用本轮真实用户消息和记忆库检索结果；它不会读取聊天历史、候选回复或 Persona，也不会自动修改 Habit、Core 或 Personality。

## 模型供应商

`default` 供应商由 `DEFAULT_*` 配置；每个工作流节点可使用上方的 `AGENT_<NAME>_*` 独立配置。可选的多供应商配置使用 `MODEL_PROVIDERS_JSON`，密钥仍从独立环境变量读取：

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

六维关系和变更日志保存在 SQLite；生成快照保存本轮 Memory Plan、Response Composer、Critic 与 User Memory Recognition 结果。Core 修改提案可由后台手工维护。

## 图片说明

图片以 OpenAI 兼容的多模态消息格式发送给当前模型。若模型不支持图片，系统会自动退回文字上下文，并在页面提示警告。

## 当前发送语义

首期按需求不连接微信等外部平台。“确认已发送”表示你已在外部聊天工具中发送该候选，系统随后把它记录为真实聊天内容；它本身不会调用外部平台接口。

## 测试

运行 `python -m unittest discover -s tests -v`。测试不调用大模型接口。
