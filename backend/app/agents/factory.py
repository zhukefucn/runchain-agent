from __future__ import annotations

import logging
from typing import Any, Type

from pydantic import BaseModel

from agentscope.agent import Agent
from agentscope.credential import CredentialBase, OpenAICredential
from agentscope.message import Msg, TextBlock
from agentscope.model import ChatModelBase, ChatResponse, OpenAIChatModel
from agentscope.tool import ToolBase, Toolkit

from app.config import Settings


logger = logging.getLogger(__name__)


class _FakeCredential(CredentialBase):
    @classmethod
    def get_chat_model_class(cls) -> Type[ChatModelBase]:
        return DeterministicFakeModel


class DeterministicFakeModel(ChatModelBase):
    """Small deterministic AgentScope model used by tests and local setup."""

    class Parameters(BaseModel):
        pass

    def __init__(self) -> None:
        super().__init__(
            credential=_FakeCredential(name="deterministic-fake"),
            model="deterministic-fake",
            parameters=self.Parameters(),
            stream=False,
            max_retries=0,
        )

    async def _call_api(self, _model: str, *, messages: list[Msg], **_kwargs: Any):
        text = ""
        if messages:
            text = "".join(
                block.text
                for block in messages[-1].content
                if isinstance(block, TextBlock)
            )
        return ChatResponse(
            content=[TextBlock(text=f"fake: {text}")],
            is_last=True,
        )


def build_model(settings: Settings) -> ChatModelBase:
    """Build a network-free fake model unless the explicit real mode is set."""
    if settings.app_env == "test":
        return DeterministicFakeModel()

    base_url = str(settings.model_base_url).rstrip("/")
    logger.info(
        "Configuring model %s at %s://%s",
        settings.model_name,
        settings.model_base_url.scheme,
        settings.model_base_url.host,
    )
    credential = OpenAICredential(
        name="runtime-model",
        api_key=settings.model_api_key.get_secret_value(),
        base_url=base_url,
    )
    return OpenAIChatModel(
        credential=credential,
        model=settings.model_name,
        stream=True,
        max_retries=2,
        client_kwargs={"timeout": 60.0},
    )


def build_agent(
    *,
    model: ChatModelBase,
    tools: list[ToolBase],
    name: str = "runchain_assistant",
    system_prompt: str = "You are the RunChain multi-tenant demo assistant.",
) -> Agent:
    """Construct a real AgentScope agent with a fresh toolkit."""
    return Agent(
        name=name,
        system_prompt=system_prompt,
        model=model,
        toolkit=Toolkit(tools=list(tools)),
    )
