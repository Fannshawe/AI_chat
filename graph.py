import os

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langchain_protocol import TypedDict
from langgraph.graph import END, MessagesState, StateGraph
from dotenv import load_dotenv


load_dotenv()


def _env_to_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DEBUG = _env_to_bool("DEBUG", default=False)


def debug_log(message: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {message}")


# 定义状态结构
class WorkflowState(TypedDict):
    user_request: str

    agent1: str
    agent2: str
    agent3: str

    final_answer: str


llm1 = ChatOpenAI(
    model=os.getenv("QWEN_MODEL", "Qwen3.5-27B-FP16"),
    api_key=os.getenv("MODEL_BASE_URL_API_KEY"),
    base_url=os.getenv("MODEL_BASE_URL"),
)

llm2 = ChatOpenAI(
    model=os.getenv("QWEN_MODEL", "Qwen3.5-27B-FP16"),
    api_key=os.getenv("MODEL_BASE_URL_API_KEY"),
    base_url=os.getenv("MODEL_BASE_URL"),
)

llm3 = ChatOpenAI(
    model=os.getenv("QWEN_MODEL", "Qwen3.5-27B-FP16"),
    api_key=os.getenv("MODEL_BASE_URL_API_KEY"),
    base_url=os.getenv("MODEL_BASE_URL"),
)

agent_1 = create_agent(
    model=llm1,
    tools=[],
    system_prompt="你是第1个agent，负责接收输入并整理为简洁任务描述。",
)

agent_2 = create_agent(
    model=llm2,
    tools=[],
    system_prompt="你是第2个agent，负责分析任务描述并给出中间处理结果。",
)

agent_3 = create_agent(
    model=llm3,
    tools=[],
    system_prompt="你是第3个agent，负责输出最终结果。",
)


# 节点函数
def node_input(state: WorkflowState) -> WorkflowState:
    debug_log(f"node_input start, user_request={state['user_request']}")
    response = agent_1.invoke({"messages": [state["user_request"]]})
    state["agent1"] = response["messages"][-1].content
    debug_log(f"node_input end, agent1={(state['agent1'])}")
    return state


def node_process(state: WorkflowState) -> WorkflowState:
    debug_log(f"node_process start, agent1={len(state['agent1'])}")
    response = agent_2.invoke({"messages": [state["agent1"]]})
    state["agent2"] = response["messages"][-1].content
    debug_log(f"node_process end, agent2={(state['agent2'])}")
    return state


def node_output(state: WorkflowState) -> WorkflowState:
    debug_log(f"node_output start, agent2={len(state['agent2'])}")
    response = agent_3.invoke({"messages": [state["agent2"]]})
    state["agent3"] = response["messages"][-1].content
    state["final_answer"] = state["agent3"]
    debug_log(f"node_output end, final_answer_len={(state['final_answer'])}")
    return state


# 构建图
def build_graph() -> StateGraph:
    graph = StateGraph(WorkflowState)

    # 添加节点
    graph.add_node("input", node_input)
    graph.add_node("process", node_process)
    graph.add_node("output", node_output)

    # 设置入口节点
    graph.set_entry_point("input")

    # 添加边
    graph.add_edge("input", "process")
    graph.add_edge("process", "output")
    graph.add_edge("output", END)

    return graph.compile()


if __name__ == "__main__":
    debug_log("starting graph execution")
    app = build_graph()

    initial_state: WorkflowState = {
        "user_request": "写一句话，描述一个美丽的女孩。",
        "agent1": "",
        "agent2": "",
        "agent3": "",
        "final_answer": "",
    }

    final_state = app.invoke(initial_state)
    debug_log("graph execution finished")
    print("\n最终状态:", final_state)
