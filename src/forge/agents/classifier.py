"""Fast intent classifier for @mention messages."""

from __future__ import annotations


import logging
from typing import TYPE_CHECKING

from agno.agent import Agent

if TYPE_CHECKING:
    from agno.models.litellm import LiteLLM

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
Classify the user's message into exactly one of these intent categories.
Reply with ONLY the category name, nothing else.

Categories:
- explain: User wants code or concepts explained ("explain this", "what does this do")
- question: User asks a factual question ("why did we choose X", "how does Y work")
- generate: User wants code written ("write a test", "add error handling")
- summarize: User wants a summary ("summarize this discussion", "what's the status")
- review: User wants a code review ("review this MR", "check this code")
- debug: User wants help debugging ("why is the pipeline failing", "this test fails")
- security: User asks about security ("are there security issues", "is this safe")
- help: User asks what the bot can do ("what can you do", "help")
- general: Anything that doesn't fit the above categories
"""


class IntentClassifier:
    """Classify @mention messages into intent categories.

    Uses the fast LLM model via LiteLLM for speed and cost efficiency.
    """

    INTENTS = frozenset(
        {
            "explain",
            "question",
            "generate",
            "summarize",
            "review",
            "debug",
            "security",
            "help",
            "general",
        }
    )

    def __init__(self, model: LiteLLM) -> None:
        self.model = model

    async def classify(self, message: str, context_hint: str = "") -> str:
        """Classify user intent.  Returns one of :attr:`INTENTS`.

        Falls back to ``"general"`` if classification is uncertain or fails.
        """
        if not message.strip():
            return "general"

        user_msg = message
        if context_hint:
            user_msg = f"[Context: {context_hint}]\n\n{message}"

        try:
            agent = Agent(
                model=self.model,
                system_message=_SYSTEM_PROMPT,
                markdown=False,
                telemetry=False,
            )
            response = await agent.arun(user_msg)

            if response and response.content:
                intent = (
                    response.content.strip().lower()
                    if isinstance(response.content, str)
                    else str(response.content).strip().lower()
                )
                if intent in self.INTENTS:
                    return intent
                logger.warning(
                    "Classifier returned unknown intent '%s', falling back to 'general'",
                    intent,
                )
        except Exception:
            logger.warning("Intent classification failed, falling back to 'general'", exc_info=True)

        return "general"
