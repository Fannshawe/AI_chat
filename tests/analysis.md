## LangGraph 节点总览

当前测试覆盖的主流程已经更新为：`context → emotion / relationship / persona_retrieval → ai_emotion → memory_planner → memory_retrieval → guidance → dialogue_composer → prompt_composer → critic → reflection → save`。

这份文件只保留现状提醒：

- 旧的 `persona`、`dialogue_policy`、`response` 节点说明已经过期。
- `Life State` 与旧版 `personas(self/other)` 兼容层已经从当前工程移除。
- 当前应以 [src/graph.py](c:/Users/fix1wx/Documents/python/Chat_LangChain/src/graph.py)、[src/agents.py](c:/Users/fix1wx/Documents/python/Chat_LangChain/src/agents.py) 和 [requirement/improvement/7.30 pipline.md](c:/Users/fix1wx/Documents/python/Chat_LangChain/requirement/improvement/7.30%20pipline.md) 为准。