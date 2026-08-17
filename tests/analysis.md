## LangGraph 节点总览

当前测试覆盖的主流程为：`context（预加载 persona_core + persona_personality） → turn_interpreter / persona_habit_retrieval / memory_planner / user_mem_recognition` 并行；`memory_planner → memory_retrieval`，`turn_interpreter → self_emotion / guidance`，四项上下文汇合后执行 `response_composer → critic（必要时内联改写并复评）→ critic_complete`，最后与 `user_mem_recognition` 汇合至 `save`。

其中 `persona_habit_retrieval` 对应 Persona 第三层 Habit（生活习惯），负责从 `persona_habits` 中按需筛出 `relevant_habits`。

这份文件只保留现状提醒：

- 旧的 `persona`、`dialogue_policy`、`response` 节点说明已经过期。
- 当前应以 [src/graph.py](src/graph.py)、[src/agents.py](src/agents.py) 和 [requirement/improvement/7.30 pipline update.md](requirement/improvement/7.30%20pipline%20update.md) 为准。