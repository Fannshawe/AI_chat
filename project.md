# Chat LangGraph: Node / LLM Input-Output

## 表 1：所有 Node 的输入和输出

| Node | 主要输入（来自 state / 外部） | 主要输出（写回 state） |
|---|---|---|
| `context` (`node_load_context`) | `contact_id?` `provider_name?`；外部：`store` | `contact_id` `history` `persona_core` `persona_personality` `persona_habits` `emotion_profile` `relationship` `recent_emotions` `ai_emotion_state` `provider_name` `critic_attempts` `critic_analysis` `fallback_candidates` |
| `emotion` (`node_emotion_detection`) | `user_request` `history` `recent_emotions` | `emotion_analysis` `warnings` `_debug_llm` |
| `relationship` (`node_relationship_state`) | `relationship` `user_request` `history` | `relationship_analysis` `warnings` `_debug_llm` |
| `persona_retrieval` (`node_persona_retrieval`) | `user_request` `persona_habits` | `relevant_habits` |
| `ai_emotion` (`node_ai_emotion_update`) | `user_request` `history` `emotion_analysis` `ai_emotion_state` `relationship` `relationship_analysis` `emotion_profile` `contact_id`；外部：`store.list_conversation_events` | `ai_emotion_analysis` `warnings` `_debug_llm` |
| `memory_planner` (`node_memory_planner`) | `user_request` `history` `emotion_analysis` `ai_emotion_analysis/ai_emotion_state` `relationship_analysis` | `memory_plan` `warnings` `_debug_llm` |
| `memory_retrieval` (`node_memory_retrieval`) | `contact_id` `memory_plan`；外部：`store.search_memories_by_plan` `store.get_ai_memory_profile` | `memories` |
| `guidance` (`node_chat_guidance`) | `user_request` `relationship.current_level` `emotion_analysis`；外部：`guidance_retriever` | `guidance_chunks` |
| `dialogue_composer` (`node_dialogue_composer`) | `user_request` `history` `persona_core` `persona_personality` `relevant_habits` `emotion_analysis` `ai_emotion_analysis/ai_emotion_state` `relationship` `relationship_analysis` `memories` `guidance_chunks` | `dialogue_composition` `strategy` `warnings` `_debug_llm` |
| `prompt_composer` (`node_prompt_composer`) | `provider_name` `critic_attempts` `image_data_urls` + `_prompt_composer_payload`所需字段（`user_request` `history` `persona_*` `relevant_habits` `emotion_analysis` `ai_emotion*` `relationship*` `memories` `dialogue_composition` `critic_analysis` `candidates`） | `candidates` `_debug_llm`；首次成功且有 3 条候选时额外写 `fallback_candidates`；失败时可能回退 `fallback_candidates` 并返回 `warnings` |
| `critic` (`node_reply_critic`) | `user_request` `history` `persona_*` `memories` `dialogue_composition` `candidates` `critic_attempts` `critic_analysis` | `critic_attempts` `critic_analysis` `warnings` `_debug_llm` |
| `reflection` (`node_reflection`) | `user_request` `history` `candidates` `memories` `ai_emotion_analysis/ai_emotion_state` `persona_habits` | `reflection_analysis` `warnings` `_debug_llm` |
| `save` (`node_content_save`) | `contact_id` `input_message_id?` `user_request` `emotion_analysis` `relationship_analysis` `ai_emotion_analysis` `reflection_analysis` `memory_plan` `memories` `dialogue_composition` `critic_analysis` `critic_attempts` `strategy` `candidates` `fallback_candidates` `warnings` `debug_trace`；外部：`store` | `input_message_id` `relationship` `generation_id` `candidates` |

## 表 2：所有 LLM（Agent）的输入和输出

| Agent | 由哪个节点调用 | LLM 输入（payload 关键字段） | 结构化输出（Schema） |
|---|---|---|---|
| `emotion` | `node_emotion_detection` | `latest_message` `recent_conversation_with_time` `recent_emotional_state` `instruction` | `EmotionAnalysis`：`emotions[]`，项字段 `subject` `emotion` `intensity` `confidence` `reason` |
| `ai_emotion` | `node_ai_emotion_update` | `latest_message` `recent_conversation_with_time` `user_emotion` `previous_ai_emotion` `relationship` `relationship_judgment` `persona_emotion_profile` `recent_events` | `AIEmotionUpdateAnalysis`：`events[]`、`ai_emotion{mood, energy, dimensions[], reason}` |
| `relationship` | `node_relationship_state` | `allowed_levels` `current_relationship` `latest_message` `recent_conversation_with_time` | `RelationshipAnalysis`：`changes[]` `suggested_level` `stage_reason` `serious_event` `behavior_boundaries[]` |
| `memory_planner` | `node_memory_planner` | `latest_message` `recent_conversation_with_time` `emotion_analysis` `ai_emotion` `relationship_analysis` `available_memory_types` `instruction` | `MemoryPlan`：`requests[]`，项字段 `memory_type` `query` `keywords[]` `priority` `top_k` `time_range` `reason` |
| `dialogue_composer` | `node_dialogue_composer` | `latest_message` `recent_conversation_with_time` `persona` `relevant_habits` `emotion` `ai_emotion` `relationship` `relationship_judgment` `memories` `chat_skill_documents` | `DialogueComposerAnalysis`：`goal` `strategy[]` `structure[]` `memory_usage[]` `persona_expression[]` `emotion_expression[]` `topic_plan[]` `constraints[]` |
| `prompt_composer` | `node_prompt_composer` / `_invoke_prompt_composer` | `latest_message` `recent_conversation_with_time` `persona` `relevant_habits` `emotion_analysis` `ai_emotion` `relationship` `relationship_boundaries` `relevant_memories` `dialogue_plan` `rewrite_instruction` `rewrite_candidate_indexes` `existing_candidates` `candidate_count`（首轮可带图片） | `ResponseCandidates`：`candidates[3]` |
| `critic` | `node_reply_critic` | `latest_message` `recent_conversation_with_time` `persona` `relevant_memories` `dialogue_plan` `candidates` `review_dimensions` | `ReplyCriticAnalysis`：`candidates[]`（含 `candidate_index` `ai_taste` `interview_taste` `empathy_excess` `naturalness` `persona_consistent` `memory_consistent` `passed` `issues[]` `rewrite_instruction`），以及 `passed` `rewrite_candidate_indexes[]` |
| `reflection` | `node_reflection` | `latest_real_message` `recent_conversation_with_time` `generated_candidates` `existing_relevant_memories` `ai_emotion` `current_habits` `allowed_memory_types` `rules` | `ReflectionAnalysis`：`memories[]` `habit_updates[]` `core_update_proposals[]` |
| `proactive`（非图节点） | `generate_proactive_draft` | `recent_conversation_with_time` `relationship` `persona` `memories` | `ProactiveDraft`：`draft` `reason` |

## 备注

- 以上 LLM 输入都通过 `_run_structured_agent` 或 `_invoke_prompt_composer` 封装，实际请求体统一是：`{"messages": [{"role": "user", "content": json(payload)}]}`。
- `context`、`persona_retrieval`、`memory_retrieval`、`guidance`、`save` 不调用 LLM。
