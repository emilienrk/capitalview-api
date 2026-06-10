"""
Card generation agent (redactor + validator sub-agents).

Uses AIProviderManager to resolve the best available provider for the user.
Requires TEXT capability (no vision needed).
"""

import json
import inspect
import logging
from datetime import datetime, timezone
from typing import Any

from services.ai.manager import AIProviderManager, NoProviderAvailableError
from services.ai.providers.base import ModelCapability, AIProvider
from services.ai.tools import get_tool_registry, get_tools
from services.encryption import decrypt_data

logger = logging.getLogger(__name__)


class CardAgent:

    def __init__(self, user_uuid: str, session: Any, master_key: bytes):
        self.user_uuid: str = user_uuid
        self.session: Any = session
        self.master_key: bytes = master_key
        self.main_agent_messages: list[dict[str, Any]] = []
        self.sub_agent_messages: list[dict[str, Any]] = []

        # Resolve provider once at construction time
        self._manager = AIProviderManager.from_user_settings(session, user_uuid, master_key)
        self._provider: AIProvider = self._manager.get_provider(
            required=ModelCapability.TEXT
        )

    # ------------------------------------------------------------------
    # System prompts
    # ------------------------------------------------------------------

    @staticmethod
    def validator_system_prompt() -> str:
        return """Tu es un agent validateur expert en communication financière. Ton rôle est d'évaluer des "cards textuelles" pour une application de gestion de patrimoine.

    Règles d'évaluation strictes - La card DOIT valider ces 3 critères :
    1. Clarté : Le texte doit être compréhensible par un novice. Aucun jargon financier complexe sans explication contextuelle.
    2. Concision : La card ne doit contenir aucune phrase superflue. 
    3. Pertinence : Le texte doit être directement corrélé au contexte thématique fourni.

    Format de sortie OBLIGATOIRE en JSON valide uniquement :
    {
        "thought": "Analyse factuelle en une phrase de la conformité aux 3 règles.",
        "validated": true/false,
        "feedback": "Si validated est false, fournis des instructions de réécriture sous forme de tirets. Si validated est true, renvoie null."
    }"""

    @staticmethod
    def validator_prompt(theme_context: str, first_try: str) -> str:
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return f"""Date du jour : {current_date}

    Contexte thématique :
    {theme_context}

    Texte généré à évaluer :
    <texte>{first_try}</texte>
    """

    @staticmethod
    def redactor_system_prompt() -> str:
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return f"""Tu es un rédacteur spécialisé dans la génération de cards textuelles financières pour une application de gestion de patrimoine.
    Ton rôle est de créer des cards concises et informatives qui servent de résumé ou d'affichage d'informations basées sur les données financières de l'utilisateur.
    Tu as accès à des outils pour interroger en temps réel les soldes (actions, cryptos, cash, etc.) et les plus-values. Utilise plusieurs outils pour essayer de croiser les informations si nécessaire.

    Date du jour : {current_date}

    Règles de rédaction :
    1. Génère un résumé sous forme de phrases simples et de façon impersonnelle.
    2. Rédige 4 à 5 phrases maximum pour résumer ton analyse.
    3. N'utilise AUCUN formatage Markdown (pas de gras, d'italique, de puces ou de titres) dans le texte généré.
    4. N'utilise AUCUN emoji.
    5. N'invente jamais de données financières. Utilise toujours les outils fournis pour obtenir les vrais montants.
    6. Réponds toujours en français.

    Format de sortie OBLIGATOIRE : tu dois absolument répondre en JSON valide uniquement, sans aucun texte autour, avec la structure suivante :
    {{
        "title": "Un titre explicite pour la card",
        "body": "Le résumé que tu as généré en respectant les règles."
    }}"""

    @staticmethod
    def redactor_prompt(theme_context: str) -> str:
        return f"""Rediger une card pour l'utilisateur sur ce thème : {theme_context}.

    PROTOCOLE DE TRAITEMENT :
    1. ANALYSE : Identifie les actifs (actions, cryptos, cash) via les outils.
    2. RÉDACTION : Produis et résume un constat factuel.

    RÈGLES STRICTES :
    - Ne jamais mentionner d'estimations : si l'outil retourne une erreur, indique "Donnée indisponible".
    - Style : Professionnel, froid, sans emphase narrative.
    """

    # ------------------------------------------------------------------
    # Main orchestration
    # ------------------------------------------------------------------

    async def main(
        self,
        theme_context: str,
        selected_theme: str,
        recent_cards: list,
        is_significant: bool = False,
        perf_data: dict[str, Any] = None,
    ) -> dict[str, Any]:
        past_theme_cards = []

        for card in recent_cards:
            try:
                if decrypt_data(card.theme_enc, self.master_key) == selected_theme:
                    past_theme_cards.append(
                        {
                            "theme": selected_theme,
                            "text": decrypt_data(card.text_enc, self.master_key),
                            "created_at": card.created_at.isoformat(),
                        }
                    )
            except Exception:
                continue

        if past_theme_cards:
            theme_context += "Cartes récentes publiées sur ce même thème (pour éviter les répétitions graves) :\n"
            for c in past_theme_cards:
                theme_context += f"- {c['created_at']} : {c['text']}\n"

        prompt_sub_agent = self.redactor_prompt(theme_context)
        self.sub_agent_messages.append({"role": "user", "content": prompt_sub_agent})

        if is_significant and perf_data:
            # Inject pre-fetched performance data as a fake tool call
            # Note: the Google provider converts these synthetic blocks to plain text
            # to avoid the thought_signature requirement; Claude/Deepseek use them natively.
            self.sub_agent_messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "Je vais regarder les performances du portefeuille pour analyser les derniers changements...",
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_fake_perf",
                            "name": "get_performance_since_last_login",
                            "input": {},
                        },
                    ],
                }
            )
            self.sub_agent_messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_fake_perf",
                            "content": json.dumps(perf_data, ensure_ascii=False),
                        }
                    ],
                }
            )

        await self.redactor_agent()
        await self.validate_agent(theme_context)

        text_response = self._get_last_agent_text()
        try:
            return json.loads(text_response)
        except Exception:
            return {"title": "Résumé", "body": text_response}

    # ------------------------------------------------------------------
    # Sub-agents
    # ------------------------------------------------------------------

    async def redactor_agent(self):
        output_config: dict[str, Any] = {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                    "required": ["title", "body"],
                    "additionalProperties": False,
                },
            }
        }
        tool_registry = get_tool_registry()

        while True:
            tools = self._provider.format_tools(get_tools())

            response = await self._provider._send_message(
                messages=self.sub_agent_messages,
                tools=tools,
                system=self.redactor_system_prompt(),
                output_config=output_config,
            )

            self.sub_agent_messages.append(
                self._provider.build_assistant_message(response)
            )

            stop_reason = self._provider.extract_stop_reason(response)
            if stop_reason == "end_turn":
                break

            tool_uses = self._provider.extract_tool_uses(response)
            tool_result_blocks = []

            for tool_use in tool_uses:
                tool_name = tool_use["name"]
                tool_args = tool_use["input"]
                tool_id = tool_use["id"]

                try:
                    func = tool_registry.get(tool_name)
                    if not func:
                        raise ValueError(f"L'outil {tool_name} n'existe pas.")

                    func_args = dict(tool_args or {})
                    func_args["session"] = self.session
                    func_args["user_uuid"] = self.user_uuid
                    func_args["master_key"] = self.master_key
                    accepted_params = set(inspect.signature(func).parameters.keys())
                    func_args = {k: v for k, v in func_args.items() if k in accepted_params}

                    result = func(**func_args)
                    if inspect.iscoroutine(result):
                        result = await result

                except Exception as error:
                    result = f"Tool execution error for {tool_name}: {error}"

                if not isinstance(result, str):
                    try:
                        result = json.dumps(result, ensure_ascii=False, default=str)
                    except Exception:
                        result = str(result)

                tool_result_blocks.append(
                    self._provider.build_tool_result_block(tool_id, result)
                )

            if tool_result_blocks:
                self.sub_agent_messages.append(
                    {"role": "user", "content": tool_result_blocks}
                )

    async def validate_agent(self, theme_context: str):
        output_config: dict[str, Any] = {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "thought": {"type": "string"},
                        "validated": {"type": "boolean"},
                        "feedback": {"type": "string"},
                    },
                    "required": ["thought", "validated", "feedback"],
                    "additionalProperties": False,
                },
            }
        }
        prompt_validation_agent = self.validator_prompt(
            theme_context, self._get_last_agent_text()
        )
        self.main_agent_messages.append(
            {"role": "user", "content": prompt_validation_agent}
        )

        response = await self._provider._send_message(
            messages=self.main_agent_messages,
            system=self.validator_system_prompt(),
            output_config=output_config,
        )
        self.main_agent_messages.append(
            self._provider.build_assistant_message(response)
        )

        response_text = self._provider.extract_text(response)
        response_data = json.loads(response_text)

        if response_data.get("validated") is False or response_data.get("validated") == "false":
            feedback = response_data.get(
                "feedback", "La réponse n'a pas été validée, merci de l'améliorer."
            )
            self.sub_agent_messages.append({"role": "user", "content": feedback})
            await self.redactor_agent()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_last_agent_text(self) -> str:
        content = self.sub_agent_messages[-1].get("content", [])

        # Gemini Content object (stored by build_assistant_message): iterate over .parts
        if hasattr(content, "parts"):
            for part in content.parts:
                if hasattr(part, "text") and part.text:
                    return part.text
            return ""

        if isinstance(content, list) and len(content) > 0:
            if hasattr(content[0], "text"):
                return content[0].text
            elif isinstance(content[0], dict) and "text" in content[0]:
                return content[0]["text"]
        elif isinstance(content, str):
            return content
        return str(content)