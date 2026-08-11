# LangChain ReAct Agent with K7 Sandbox Tool

This tutorial shows a minimal LangChain ReAct-style agent equipped with a tool that executes shell commands inside a K7 sandbox.

## Prerequisites
- K7 API deployed and reachable (use `k7 start-api` and check `k7 api-status` for the public URL)
- API key generated: `k7 generate-api-key <name>`
- Python 3.10+
- uv (recommended): https://docs.astral.sh/uv/

## Setup
0. Install uv (if not installed):
```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

1. Create a `.env` file in this directory with:
```
K7_ENDPOINT=https://your-k7-endpoint
K7_API_KEY=your-api-key
K7_SANDBOX_NAME=lc-agent
K7_SANDBOX_IMAGE=alpine:latest
K7_NAMESPACE=default
OPENAI_API_KEY=sk-your-openai-key
OPENAI_MODEL=gpt-4o-mini
```

2. Create an isolated environment and install dependencies (using uv):
```
# from this tutorial directory
uv venv .venv-lc
. .venv-lc/bin/activate

# core deps for the tutorial
uv pip install -r requirements.txt

# install the local K7 SDK from the repo source
# (two levels up from this tutorial dir)
uv pip install -e ../..

# or from the PyPI registry:
uv pip install k7-sdk
```

## Run
```
python agent.py
```

Ask the agent to perform simple shell actions, e.g., "List files in /". The agent will decide to use the sandbox tool and return the output. 

In parallel if you want you can shell into its sandbox:
```shell
k7 shell lc-agent
```
or replace `lc-agent` with the sandbox name you chose.