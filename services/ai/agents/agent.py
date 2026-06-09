"""
Base conversational agent (chat with tool use).

Uses AIProviderManager to resolve the best available provider for the user.
"""

import json
import logging

from sqlmodel import Session

from services.ai.manager import AIProviderManager, NoProviderAvailableError
from services.ai.providers.base import ModelCapability
from services.ai.tools import get_tool_registry, get_tools

logger = logging.getLogger(__name__)

_agent_instances: dict[str, "Agent"] = {}


def get_agent_for_user(user_uuid: str) -> "Agent":
    if user_uuid not in _agent_instances:
        _agent_instances[user_uuid] = Agent(user_uuid)
    return _agent_instances[user_uuid]


class Agent:
    """
    General-purpose conversational agent with tool-use support.
    Provider is resolved at call time via AIProviderManager.
    """

    def __init__(self, user_uuid: str):
        self.user_uuid = user_uuid
        self.messages: list = []

    def message(
        self,
        request: str,
        session: Session,
        master_key: bytes | str,
        system_prompt: str | None = None,
    ) -> str:
        """
        Send a user message and return the assistant's final text response.

        Raises:
            NoProviderAvailableError: if no AI provider is configured for this user.
        """
        import asyncio

        return asyncio.get_event_loop().run_until_complete(
            self._message_async(request, session, master_key, system_prompt)
        )

    async def _message_async(
        self,
        request: str,
        session,
        master_key: bytes | str,
        system_prompt: str | None = None,
    ) -> str:
        manager = AIProviderManager.from_user_settings(session, self.user_uuid, master_key)
        provider = manager.get_provider(required=ModelCapability.TEXT)

        tool_registry = get_tool_registry()
        tools = provider.format_tools(get_tools())

        self.messages.append({"role": "user", "content": request})

        while True:
            response = await provider._send_message(
                messages=self.messages,
                tools=tools,
                system=system_prompt,
            )

            self.messages.append(provider.build_assistant_message(response))

            stop_reason = provider.extract_stop_reason(response)
            if stop_reason != "tool_use":
                break

            tool_uses = provider.extract_tool_uses(response)
            tool_result_blocks = []

            for tool_use in tool_uses:
                tool_name = tool_use["name"]
                tool_args = tool_use["input"]
                tool_id = tool_use["id"]

                try:
                    func = tool_registry.get(tool_name)
                    if not func:
                        raise ValueError(f"L'outil {tool_name} n'existe pas.")
                    result = func(**tool_args)
                    result_str = json.dumps(result)
                except Exception as exc:
                    result_str = f"Erreur critique lors de l'execution: {exc}"

                tool_result_blocks.append(
                    provider.build_tool_result_block(tool_id, result_str)
                )

            self.messages.append({"role": "user", "content": tool_result_blocks})

        return provider.extract_text(response)