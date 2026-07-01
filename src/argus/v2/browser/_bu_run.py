"""Standalone browser-use runner, executed in a DEDICATED venv via subprocess.

The production argus runtime never imports browser-use (heavy deps: playwright,
chromium, its own pydantic/httpx pins). Instead runner._default_runner shells out
to this script with a dedicated venv's python. This file imports ONLY stdlib +
browser_use - never argus - so it runs in a venv that has browser-use but not
argus.

Protocol: JSON payload {task, allowed_domains, model, timeout} on stdin; the final
result text is printed between the sentinels below. The LLM API key comes from the
inherited environment (ANTHROPIC_API_KEY / OPENAI_API_KEY).
"""

import asyncio
import json
import sys

RESULT_START = "<<<BU_RESULT_START>>>"
RESULT_END = "<<<BU_RESULT_END>>>"


def _make_llm(model: str):
    from browser_use import ChatAnthropic, ChatOpenAI

    name = model.split("/")[-1]
    if name.lower().startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return ChatOpenAI(model=name, temperature=0.0)
    return ChatAnthropic(model=name, temperature=0.0)


def main() -> None:
    payload = json.load(sys.stdin)
    task = payload["task"]
    allowed_domains = list(payload.get("allowed_domains") or [])
    model = payload["model"]
    timeout = int(payload.get("timeout", 180))

    from browser_use import Agent, BrowserProfile

    async def _run() -> str:
        agent = Agent(
            task=task,
            llm=_make_llm(model),
            browser_profile=BrowserProfile(headless=True, allowed_domains=allowed_domains),
        )
        history = await asyncio.wait_for(agent.run(max_steps=12), timeout=timeout)
        return history.final_result() or ""

    result = asyncio.run(_run())
    # Sentinel-wrapped so the parent parses the result cleanly despite browser-use
    # logging to stdout/stderr.
    sys.stdout.write(f"\n{RESULT_START}\n{result}\n{RESULT_END}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
