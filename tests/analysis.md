## LangGraph 节点总览

当前测试覆盖的主流程已经更新为：`context（预加载 persona_core + persona_personality） → emotion / relationship / persona_habit_retrieval / memory_planner → memory_retrieval → ai_emotion → guidance → dialogue_composer → prompt_composer → critic → reflection → save`。

其中 `persona_habit_retrieval` 对应 Persona 第三层 Habit（生活习惯），负责从 `persona_habits` 中按需筛出 `relevant_habits`。

这份文件只保留现状提醒：

- 旧的 `persona`、`dialogue_policy`、`response` 节点说明已经过期。
- 当前应以 [src/graph.py](c:/Users/fix1wx/Documents/python/Chat_LangChain/src/graph.py)、[src/agents.py](c:/Users/fix1wx/Documents/python/Chat_LangChain/src/agents.py) 和 [requirement/improvement/7.30 pipline.md](c:/Users/fix1wx/Documents/python/Chat_LangChain/requirement/improvement/7.30%20pipline.md) 为准。