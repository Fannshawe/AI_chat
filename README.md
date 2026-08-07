# Persona Chat

一个在 Windows 本地运行的恋爱聊天 AI：读取对方的消息后，结合固定 Persona、情绪、六维关系状态、分类长期记忆和聊天技巧文档，生成 3 个可编辑候选回复。候选只有在页面点击“确认已发送”后才进入真实聊天记录。

## 已实现

- FastAPI 本地服务与现代化聊天页面
- 独立管理后台：Persona YAML、Core 提案、记忆、情绪、关系、指导文档、导入导出、模型切换
- LangGraph 工作流：上下文（预加载 Persona Core + Personality） → User Emotion → Relationship → Persona Habit Retrieval → Memory Planner → Memory Retrieval → AI Emotion Update → 技巧文档 → Dialogue Composer → Prompt Composer → Reply Critic → Reflection → 保存
- 每个 LLM 节点保留独立的 LangChain v1 `create_agent()`，节点直接调用 `agent.invoke()`
- Pydantic + `ToolStrategy` 结构化输出，结果从 `structured_response` 读取，不再手工解析模型 JSON
- `ModelRetryMiddleware` 处理瞬时模型故障，`ModelCallLimitMiddleware` 限制单节点调用次数
- 最近短期上下文
- SQLite 长期记忆与可读 JSON 全量导出
- Persona YAML 模板：Core 与 Personality 在上下文加载阶段固定注入，Habit 作为按需检索层由 Persona Habit Retrieval 选择性注入；Habit 可由 Reflection 自动更新
- 四类长期记忆：episodic、semantic、emotional、shared_experience
- 六维关系状态：trust、familiarity、intimacy、attraction、attachment、comfort
- 六阶段关系状态机：陌生人、认识、朋友、亲密朋友、暧昧、恋人；严重事件才允许降级
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
   - `AGENT_AI_EMOTION_BASE_URL` / `AGENT_AI_EMOTION_API_KEY` / `AGENT_AI_EMOTION_MODEL`
   - `AGENT_RELATIONSHIP_BASE_URL` / `AGENT_RELATIONSHIP_API_KEY` / `AGENT_RELATIONSHIP_MODEL`
   - `AGENT_MEMORY_PLANNER_BASE_URL` / `AGENT_MEMORY_PLANNER_API_KEY` / `AGENT_MEMORY_PLANNER_MODEL`
   - `AGENT_DIALOGUE_COMPOSER_BASE_URL` / `AGENT_DIALOGUE_COMPOSER_API_KEY` / `AGENT_DIALOGUE_COMPOSER_MODEL`
   - `AGENT_PROMPT_COMPOSER_BASE_URL` / `AGENT_PROMPT_COMPOSER_API_KEY` / `AGENT_PROMPT_COMPOSER_MODEL`
   - `AGENT_CRITIC_BASE_URL` / `AGENT_CRITIC_API_KEY` / `AGENT_CRITIC_MODEL`
   - `AGENT_REFLECTION_BASE_URL` / `AGENT_REFLECTION_API_KEY` / `AGENT_REFLECTION_MODEL`
   - `AGENT_PROACTIVE_BASE_URL` / `AGENT_PROACTIVE_API_KEY` / `AGENT_PROACTIVE_MODEL`
   - `WORKFLOW_DEBUG`：是否在终端输出 `prompt_composer` 候选词、`critic` 分析结果、`prompt_rewriter` 输出候选词
   - `CRITIC_MAX_ATTEMPTS`：Reply Critic 最大评审次数，默认 `3`
2. 安装依赖：`pip install -r requirements.txt`
3. 启动：`python app.py`
4. 浏览器打开 `http://127.0.0.1:8000`
5. 管理后台：`http://127.0.0.1:8000/admin`

也可以用 `uvicorn app:app --host 127.0.0.1 --port 8000` 启动。

## 首次使用建议

1. 先编辑根目录 `persona.yaml`，填写 Core、Personality、Habit 和表达示例。Core 与 Personality 不会被自动修改。
2. 导入历史聊天，格式为每行 `我: 内容` 或 `对方: 内容`。
3. 在“指导文档”中维护聊天技巧。关系行为边界由 Relationship State 每轮动态生成，不由技巧文档直接决定。
4. 回到聊天页，粘贴对方最新消息并生成候选；“暂不回复”也可能作为一个候选策略。

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

## Render 部署

这个项目可以直接部署到 Render 的 Web Service。

前提：

- 代码已经推到 GitHub 仓库
- `.env` 不要提交，改为在 Render 后台配置环境变量
- 你的模型 API 从 Render 机房可访问

推荐区域：

- `Singapore`：对中国大陆访问通常比欧美更稳

### 方式一：使用仓库内 `render.yaml`

仓库根目录已经提供 [render.yaml](c:\Users\fix1wx\Documents\python\Chat_LangChain\render.yaml)。

在 Render 中：

1. 进入 Blueprint / New From Repository
2. 选择你的 GitHub 仓库
3. Render 会读取 `render.yaml` 创建 Web Service 和持久化磁盘
4. 在服务的 Environment 中补齐所有 `AGENT_*_BASE_URL` / `AGENT_*_API_KEY` / `AGENT_*_MODEL`
5. 部署完成后访问 Render 分配的域名

`render.yaml` 里已经配置：

- 构建命令：`pip install -r requirements.txt`
- 启动命令：`uvicorn app:app --host 0.0.0.0 --port $PORT`
- 数据目录：`CHAT_DATA_DIR=/var/data`
- 持久化磁盘挂载：`/var/data`
- 健康检查：`/api/bootstrap`

### 方式二：手动创建 Web Service

如果你不想用 Blueprint，可以手动填：

1. Build Command：`pip install -r requirements.txt`
2. Start Command：`uvicorn app:app --host 0.0.0.0 --port $PORT`
3. Add Disk：
   - Mount Path：`/var/data`
   - Size：至少 `5GB`
4. 环境变量增加：`CHAT_DATA_DIR=/var/data`

然后补齐以下环境变量：

- `AGENT_EMOTION_BASE_URL`
- `AGENT_EMOTION_API_KEY`
- `AGENT_EMOTION_MODEL`
- `AGENT_AI_EMOTION_BASE_URL`
- `AGENT_AI_EMOTION_API_KEY`
- `AGENT_AI_EMOTION_MODEL`
- `AGENT_RELATIONSHIP_BASE_URL`
- `AGENT_RELATIONSHIP_API_KEY`
- `AGENT_RELATIONSHIP_MODEL`
- `AGENT_MEMORY_PLANNER_BASE_URL`
- `AGENT_MEMORY_PLANNER_API_KEY`
- `AGENT_MEMORY_PLANNER_MODEL`
- `AGENT_DIALOGUE_COMPOSER_BASE_URL`
- `AGENT_DIALOGUE_COMPOSER_API_KEY`
- `AGENT_DIALOGUE_COMPOSER_MODEL`
- `AGENT_PROMPT_COMPOSER_BASE_URL`
- `AGENT_PROMPT_COMPOSER_API_KEY`
- `AGENT_PROMPT_COMPOSER_MODEL`
- `AGENT_CRITIC_BASE_URL`
- `AGENT_CRITIC_API_KEY`
- `AGENT_CRITIC_MODEL`
- `AGENT_REFLECTION_BASE_URL`
- `AGENT_REFLECTION_API_KEY`
- `AGENT_REFLECTION_MODEL`
- `AGENT_PROACTIVE_BASE_URL`
- `AGENT_PROACTIVE_API_KEY`
- `AGENT_PROACTIVE_MODEL`

可选变量：

- `WORKFLOW_DEBUG=false`
- `CRITIC_MAX_ATTEMPTS=3`
- `MAX_IMAGE_BYTES=5242880`
- `SHORT_TERM_MESSAGE_LIMIT=100`

### Render 上的数据持久化

项目运行时会把数据写到 `CHAT_DATA_DIR`：

- SQLite：`chat.db`
- 上传图片：`uploads/`
- persona 数据文件：`persona.yaml`

所以一定要挂载磁盘；如果不挂载，服务重启后聊天记录和上传文件会丢失。

### 上线后访问地址

- 首页：`https://你的服务域名/`
- 管理后台：`https://你的服务域名/admin`
- 调试页：`https://你的服务域名/debug`

### 风险与限制

- 当前 `/admin` 和 API 没有登录保护，部署到公网后任何拿到地址的人都可能访问或修改数据
- SQLite 适合轻量单实例，不适合高并发多副本
- 如果模型接口在 Render 区域不可达，生成会返回 502

如果要长期公网使用，建议下一步加后台鉴权，再考虑把 SQLite 和图片上传迁移到专门的数据库/对象存储。

## Zeabur 部署

这个项目也可以直接部署到 Zeabur，不需要改代码。

Zeabur 部署的关键点只有 3 个：

- 构建时安装 `requirements.txt`
- 启动时运行 `uvicorn app:app --host 0.0.0.0 --port $PORT`
- 给 `CHAT_DATA_DIR` 挂一个持久化存储目录

### 部署步骤

1. 把项目推到 GitHub
2. 登录 Zeabur，创建一个新项目
3. 选择从 GitHub 导入这个仓库
4. Zeabur 识别为 Python 项目后，进入服务设置
5. 确认构建命令和启动命令
6. 配置环境变量
7. 挂载持久化存储
8. 部署并绑定域名

### 构建与启动命令

如果 Zeabur 没有自动识别正确命令，就手动填写：

- Build Command：`pip install -r requirements.txt`
- Start Command：`uvicorn app:app --host 0.0.0.0 --port $PORT`

### 必填环境变量

除了模型相关变量，还必须设置：

- `CHAT_DATA_DIR=/data`

然后补齐：

- `AGENT_EMOTION_BASE_URL`
- `AGENT_EMOTION_API_KEY`
- `AGENT_EMOTION_MODEL`
- `AGENT_AI_EMOTION_BASE_URL`
- `AGENT_AI_EMOTION_API_KEY`
- `AGENT_AI_EMOTION_MODEL`
- `AGENT_RELATIONSHIP_BASE_URL`
- `AGENT_RELATIONSHIP_API_KEY`
- `AGENT_RELATIONSHIP_MODEL`
- `AGENT_MEMORY_PLANNER_BASE_URL`
- `AGENT_MEMORY_PLANNER_API_KEY`
- `AGENT_MEMORY_PLANNER_MODEL`
- `AGENT_DIALOGUE_COMPOSER_BASE_URL`
- `AGENT_DIALOGUE_COMPOSER_API_KEY`
- `AGENT_DIALOGUE_COMPOSER_MODEL`
- `AGENT_PROMPT_COMPOSER_BASE_URL`
- `AGENT_PROMPT_COMPOSER_API_KEY`
- `AGENT_PROMPT_COMPOSER_MODEL`
- `AGENT_CRITIC_BASE_URL`
- `AGENT_CRITIC_API_KEY`
- `AGENT_CRITIC_MODEL`
- `AGENT_REFLECTION_BASE_URL`
- `AGENT_REFLECTION_API_KEY`
- `AGENT_REFLECTION_MODEL`
- `AGENT_PROACTIVE_BASE_URL`
- `AGENT_PROACTIVE_API_KEY`
- `AGENT_PROACTIVE_MODEL`

可选：

- `WORKFLOW_DEBUG=false`
- `CRITIC_MAX_ATTEMPTS=3`
- `MAX_IMAGE_BYTES=5242880`
- `SHORT_TERM_MESSAGE_LIMIT=100`

可以直接参考 [.env.example](c:\Users\fix1wx\Documents\python\Chat_LangChain\.env.example)。

### 持久化存储

这个项目不是纯无状态服务，必须挂持久化存储。原因是运行时会写入：

- `chat.db`
- `uploads/`
- `persona.yaml`

在 Zeabur 中，给这个服务添加一个 Volume，并挂载到例如 `/data`，然后把环境变量设置为：

- `CHAT_DATA_DIR=/data`

如果不挂载 Volume，服务重启或重建后，这些数据会丢失。

### 健康检查

建议把健康检查路径设为：

- `/api/bootstrap`

只要这个接口返回 200，说明服务基本可用。

### 访问地址

部署成功后：

- 首页：`https://你的-zeabur-域名/`
- 管理后台：`https://你的-zeabur-域名/admin`
- 调试页：`https://你的-zeabur-域名/debug`

### Zeabur 上的注意事项

- 当前项目的 `/admin` 和 API 没有登录保护，公网部署后风险很高
- SQLite 适合单实例，若以后开多个副本会有一致性和锁问题
- 如果你的模型服务限制来源地区或出口 IP，需要先确认 Zeabur 节点可访问

### 推荐做法

如果你只是先跑起来：

1. 单服务部署
2. 挂一个 Volume 到 `/data`
3. 配置所有 `AGENT_*` 环境变量
4. 健康检查设 `/api/bootstrap`

如果你准备长期使用：

1. 先给 `/admin` 加鉴权
2. 以后把 SQLite 换成 MySQL/PostgreSQL
3. 图片上传迁移到对象存储
