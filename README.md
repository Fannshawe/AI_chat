# Persona Chat

一个在 Windows 本地运行的恋爱聊天 AI：读取对方的消息后，结合稳定人格、双方情绪、关系阶段、长期记忆和聊天指导文档，生成 3 个可编辑候选回复。候选只有在页面点击“确认已发送”后才进入真实聊天记录。

## 已实现

- FastAPI 本地服务与现代化聊天页面
- 独立管理后台：人格、对方画像、记忆、情绪、关系、指导文档、导入导出、模型切换
- LangGraph 工作流：上下文 → 情绪 → 关系 → 记忆 → 人格 → 指导 → 回复 → 保存
- 每个 LLM 节点保留独立的 LangChain v1 `create_agent()`，节点直接调用 `agent.invoke()`
- Pydantic + `ToolStrategy` 结构化输出，结果从 `structured_response` 读取，不再手工解析模型 JSON
- `ModelRetryMiddleware` 处理瞬时模型故障，`ModelCallLimitMiddleware` 限制单节点调用次数
- 最近 50 轮短期上下文
- SQLite 长期记忆与可读 JSON 全量导出
- 六阶段关系状态机，AI 只建议、人工确认
- 双方情绪连续记录；低置信度记录等待确认
- Markdown 指导文档本地关键词检索
- 纯文本、图片输入和历史聊天导入
- 主动聊天草稿（不自动发送）
- OpenAI 兼容模型供应商切换
- 独立 Dialogue Policy：在分享、调侃、回忆、简短回应、提问、转移话题和暂不回复之间选择，并动态限制问句
- 每日 Life State：手工维护心情、精力、好奇心、依恋感、活动与事件，按日期保留历史
- 高置信度关系自动变更：仅允许置信度不低于 0.9 的相邻阶段变化自动生效
- Reply Critic：按 AI 味、采访味、过度共情和自然度评审，失败时自动重写一次
- 候选选择反馈：只记录最终选择的候选序号，不额外保存编辑过程

## 启动

1. 保持现有 `.env` 中以下变量可用：
   - `MODEL_BASE_URL`
   - `MODEL_BASE_URL_API_KEY`
   - `QWEN_MODEL`
2. 安装依赖：`pip install -r requirements.txt`
3. 启动：`python app.py`
4. 浏览器打开 `http://127.0.0.1:8000`
5. 管理后台：`http://127.0.0.1:8000/admin`

也可以用 `uvicorn app:app --host 127.0.0.1 --port 8000` 启动。

## 首次使用建议

1. 进入管理后台，填写“我的稳定人格”和示例回复。
2. 在“我的生活”中填写当天 Life State；系统不会自行编造未填写的生活细节。
3. 填写已知的对方画像。
4. 导入历史聊天，格式为每行 `我: 内容` 或 `对方: 内容`。
5. 在“指导文档”中写明各关系阶段的称呼、亲密度、Emoji 和话题边界。
6. 回到聊天页，粘贴对方最新消息并生成候选；“暂不回复”也可能作为一个候选策略。

## 新回复流程

当前主流程为：上下文 → 情绪 → 关系评估 → 记忆检索 → 人格 → 指导 → 对话策略 → 回复 → 评审 → 保存。

Reply Critic 会增加一次模型调用；若首轮不通过，还会增加一次回复重写和一次复评。复评仍不通过时保留候选，但在页面显示质量预警。关系边界只取自指导文档，没有对应文档时采用克制且不越界的通用策略。

## 模型供应商

现有 `.env` 配置自动成为 `default` 供应商。可选的多供应商配置使用 `MODEL_PROVIDERS_JSON`，密钥仍从独立环境变量读取：

```json
{"deepseek":{"model":"deepseek-chat","base_url":"https://api.example.com/v1","api_key_env":"DEEPSEEK_API_KEY"}}
```

管理后台只保存当前供应商名称，不把 API Key 写入 SQLite。

Agent 定义集中在 `agents.py`，并按模型供应商缓存。外层 `graph.py` 仍负责业务拓扑和本地状态流转；各 `create_agent()` 自身也是编译后的 LangGraph，因此中间件会在嵌入外层图后继续生效。

## 数据位置

- SQLite：`data/chat.db`
- 聊天图片：`data/uploads/`
- 提示词：`prompt/`
- 指导文档：`guidance_docs/`

`data/` 默认被 Git 忽略。聊天内容可能包含高度私密信息，请不要把数据库、导出文件、上传图片或 `.env` 提交到仓库。

Life State 保存在 SQLite 的 `life_states` 表；生成快照保存本轮 Dialogue Policy 与 Critic 结果。每日状态支持在管理后台编辑、删除和查看历史。

## 图片说明

图片以 OpenAI 兼容的多模态消息格式发送给当前模型。若模型不支持图片，系统会自动退回文字上下文，并在页面提示警告。

## 当前发送语义

首期按需求不连接微信等外部平台。“确认已发送”表示你已在外部聊天工具中发送该候选，系统随后把它记录为真实聊天内容；它本身不会调用外部平台接口。

## 测试

运行 `python -m unittest discover -s tests -v`。测试不调用大模型接口。
