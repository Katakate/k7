#!/usr/bin/env python3
import os
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# LangChain
from langchain.agents import initialize_agent, AgentType
from langchain.memory import ConversationBufferMemory
from langchain.tools import Tool
from langchain_openai import ChatOpenAI

# K7 SDK
from k7_sdk import Client, SandboxProxy

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

K7_ENDPOINT = os.getenv("K7_ENDPOINT")
K7_API_KEY = os.getenv("K7_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SANDBOX_NAME = os.getenv("K7_SANDBOX_NAME", "lc-agent")
SANDBOX_IMAGE = os.getenv("K7_SANDBOX_IMAGE", "alpine:latest")
SANDBOX_NAMESPACE = os.getenv("K7_NAMESPACE", "default")

if not K7_ENDPOINT or not K7_API_KEY:
    raise SystemExit("K7_ENDPOINT and K7_API_KEY must be set in .env")
if not OPENAI_API_KEY:
    raise SystemExit("OPENAI_API_KEY must be set in .env for this LangChain example")

k7 = Client(endpoint=K7_ENDPOINT, api_key=K7_API_KEY)
_sb: Optional[SandboxProxy] = None


def ensure_sandbox_ready(timeout_seconds: int = 60) -> SandboxProxy:
    try:
        # Prefer to create and get a proxy back
        sb = k7.create(
            {
                "name": SANDBOX_NAME,
                "image": SANDBOX_IMAGE,
                "namespace": SANDBOX_NAMESPACE,
            }
        )
    except Exception:
        # If already exists or creation fails, construct a proxy directly
        sb = SandboxProxy(SANDBOX_NAME, SANDBOX_NAMESPACE, k7)

    # Wait for pod to be Running
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        items = k7.list(namespace=SANDBOX_NAMESPACE)
        for sb_info in items:
            if (
                sb_info.get("name") == SANDBOX_NAME
                and sb_info.get("status") == "Running"
            ):
                return sb
        time.sleep(2)
    raise RuntimeError("Sandbox did not become Running in time")


def run_code_in_sandbox(code: str) -> str:
    global _sb
    if _sb is None:
        _sb = ensure_sandbox_ready()
    result = _sb.exec(code)
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    if result.get("exit_code", 1) != 0:
        return f"[stderr]\n{stderr}\n[stdout]\n{stdout}"
    return stdout


def main() -> None:
    tool = Tool(
        name="sandbox_exec",
        description="Execute a shell command inside an isolated K7 sandbox. Input should be a shell command string.",
        func=run_code_in_sandbox,
    )

    llm = ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), temperature=0.0)

    memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)

    agent = initialize_agent(
        tools=[tool],
        llm=llm,
        agent=AgentType.CONVERSATIONAL_REACT_DESCRIPTION,
        memory=memory,
        verbose=True,
        handle_parsing_errors=True,
    )

    print("Ask me to run a command in a sandbox, e.g.: 'List files in /'\n")
    while True:
        try:
            user = input("You: ")
        except (EOFError, KeyboardInterrupt):
            break
        if not user.strip():
            continue
        resp = agent.invoke({"input": user})
        # resp can be dict with output
        print("Agent:", resp.get("output", str(resp)))


if __name__ == "__main__":
    main()
