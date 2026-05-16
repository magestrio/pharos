import anthropic

from agent.reason.schema import Decision
from agent.reason.prompt import SYSTEM_PROMPT


async def reason(state: dict) -> Decision:
    client = anthropic.AsyncAnthropic()

    response = await client.messages.create(
        model="claude-opus-4-7",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": str(state)}],
    )

    raise NotImplementedError("Structured output parsing not yet implemented")
