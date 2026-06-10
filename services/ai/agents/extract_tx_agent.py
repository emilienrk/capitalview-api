"""
Transaction screenshot extraction agent.

Uses AIProviderManager to resolve the best available provider for vision,
respecting the user's configured provider and model preferences.
"""

import json
import logging
from typing import Any, Optional

from models.enums import AssetType, CryptoTransactionType, StockTransactionType, CryptoCompositeTransactionType
from services.ai.manager import AIProviderManager, NoProviderAvailableError
from services.market import get_all_assets, search_assets

logger = logging.getLogger(__name__)


class ExtractTxAgent:
    """
    Extracts transaction data from a screenshot image.

    Requires a provider with VISION capability to process images.
    """

    def __init__(self, user_uuid: str, session: Any, master_key: bytes):
        self.user_uuid: str = user_uuid
        self.session: Any = session
        self.master_key: bytes = master_key
        self.messages: list = []

        self._manager = AIProviderManager.from_user_settings(session, user_uuid, master_key)
        self._provider = self._manager.get_provider_for_capability("vision")

    @staticmethod
    def system_prompt() -> str:
        return """You are an information extraction agent expert in financial transactions. 
        You will analyze a document potentially containing financial transactions and extract the relevant information according to the provided schema. Return an empty list if you find nothing.
        """

    def _build_output_config(self, asset_type: AssetType) -> dict[str, Any]:
        if asset_type is AssetType.STOCK:
            return {
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "transactions": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "reasoning": {
                                            "type": "string",
                                            "description": (
                                                "Mandatory step-by-step analysis before extracting data:\n"
                                                "1. Visual Location: Describe exactly where the key information is located on the image.\n"
                                                "2. Literal Extraction: Quote the exact raw text seen on the screen for amounts, tickers, fees, and dates. Do not perform any calculations, conversions, or assumptions."
                                            ),
                                        },
                                        "asset_key": {
                                            "type": ["string", "null"],
                                            "description": (
                                                "ISIN code of the stock. Format: 2-letter country code + "
                                                "9 alphanumeric chars + 1 check digit. "
                                                "Example: FR0000131104, US0378331005."
                                            ),
                                        },
                                        "type": {
                                            "type": "string",
                                            "enum": [t.value for t in StockTransactionType],
                                        },
                                        "amount": {"type": "string"},
                                        "price_per_unit": {
                                            "type": "string",
                                            "description": (
                                                "Price per unit as seen exactly on the document. "
                                                "DO NOT perform any math or division. "
                                                "Note that the comma ',' can be a decimal separator in European formats. "
                                                "Remove the currency symbol and return only the raw numeric value with its decimals."
                                            ),
                                        },
                                        "fees": {
                                            "type": "string",
                                            "description": (
                                                "Transaction fees in EUR. Null if not applicable or unknown. "
                                                "Use '0' if explicitly zero."
                                            ),
                                        },
                                        "executed_at": {
                                            "type": "string",
                                            "description": (
                                                "Date and time of the transaction. "
                                                "Format as ISO 8601: YYYY-MM-DDTHH:mm:ssZ "
                                                "(e.g. 2024-01-15T10:30:00Z) or YYYY-MM-DDTHH:mm:ss."
                                            ),
                                        },
                                        "notes": {"type": ["string", "null"]},
                                    },
                                    "required": ["reasoning", "type", "amount", "price_per_unit", "fees", "executed_at"],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        "required": ["transactions"],
                        "additionalProperties": False,
                    },
                }
            }

        # CRYPTO
        return {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "transactions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "reasoning": {
                                        "type": "string",
                                        "description": (
                                            "Mandatory step-by-step analysis before extracting data:\n"
                                            "1. Visual Location: Describe exactly where the key information is located on the image.\n"
                                            "2. Literal Extraction: Quote the exact raw text seen on the screen for amounts, tickers, fees, and dates. Do not perform any calculations, conversions, or assumptions."
                                        ),
                                    },
                                    "type": {
                                        "type": "string",
                                        "enum": [
                                            CryptoCompositeTransactionType.BUY.value,
                                            CryptoCompositeTransactionType.FIAT_DEPOSIT.value,
                                            CryptoCompositeTransactionType.FIAT_WITHDRAW.value,
                                            CryptoCompositeTransactionType.SELL_TO_FIAT.value,
                                            CryptoCompositeTransactionType.CRYPTO_DEPOSIT.value,
                                            CryptoCompositeTransactionType.EXIT.value,
                                        ],
                                        "description": (
                                            "Type of transaction:\n"
                                            "- BUY: Buying crypto using fiat or swapping crypto to crypto.\n"
                                            "- FIAT_DEPOSIT: Depositing fiat (EUR, USD, etc.) to an exchange.\n"
                                            "- FIAT_WITHDRAW: Withdrawing fiat to a bank account.\n"
                                            "- SELL_TO_FIAT: Selling crypto for fiat.\n"
                                            "- CRYPTO_DEPOSIT: Depositing crypto from another wallet.\n"
                                            "- EXIT: Withdrawing crypto to an external wallet or spending it."
                                        )
                                    },
                                    "asset_key": {
                                        "type": "string",
                                        "description": (
                                            "Cryptocurrency ticker symbol (e.g., BTC, ETH). For FIAT_DEPOSIT or FIAT_WITHDRAW, use EUR, USD, etc."
                                        ),
                                    },
                                    "amount": {
                                        "type": "string",
                                        "description": (
                                            "The amount of the asset (asset_key) involved. "
                                            "Return only the raw numeric value, using '.' for decimals."
                                        ),
                                    },
                                    "quote_asset_key": {
                                        "type": ["string", "null"],
                                        "description": "Ticker of the asset used to pay (e.g., EUR, USDT)."
                                    },
                                    "quote_amount": {
                                        "type": ["string", "null"],
                                        "description": "The amount of the quote asset used to pay."
                                    },


                                    "fee_asset_key": {
                                        "type": ["string", "null"],
                                        "description": "Asset used to pay fees. IMPORTANT: If there are fees but the fee ticker is not specified, use the ticker of the asset being bought."
                                    },
                                    "fee_amount": {
                                        "type": ["string", "null"],
                                        "description": "Amount of fees paid."
                                    },
                                    "executed_at": {
                                        "type": "string",
                                        "description": (
                                            "Date and time of the transaction. "
                                            "Format as ISO 8601: YYYY-MM-DDTHH:mm:ssZ "
                                            "(e.g. 2024-01-15T10:30:00Z) or YYYY-MM-DDTHH:mm:ss."
                                        ),
                                    },
                                    "tx_hash": {"type": ["string", "null"]},
                                    "notes": {"type": ["string", "null"]},
                                },
                                "required": ["reasoning", "type", "asset_key", "amount", "executed_at"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["transactions"],
                    "additionalProperties": False,
                },
            }
        }

    async def analyse(
        self,
        image_data: str,
        image_media_type: str,
        asset_type: AssetType | None = None,
    ) -> str:
        output_config = self._build_output_config(asset_type)

        asset_list = get_all_assets(
            user_uuid=self.user_uuid,
            session=self.session,
            master_key=self.master_key,
            only_owned=False,
            asset_type=asset_type,
            limit=50,
        )

        self.messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": image_media_type,
                            "data": image_data,
                        },
                    },
                    {"type": "text", "text": "Extract the transaction information from this image."},
                ],
            }
        )

        # Inject pre-fetched asset list as a fake tool result to give the model context.
        # Note: the Google provider converts these synthetic blocks (id starts with 'toolu_fake')
        # to plain text to avoid the thought_signature requirement; Claude/Deepseek use them natively.
        if asset_list:
            self.messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "I will check the known assets in the database...",
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_fake_assets",
                            "name": "get_assets",
                            "input": {},
                        },
                    ],
                }
            )
            self.messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_fake_assets",
                            "content": json.dumps(asset_list, ensure_ascii=False),
                        }
                    ],
                }
            )


        search_tool_def = {
            "name": "search_assets",
            "description": (
                "Searches for an asset by its name or ticker. "
                "In response, provide the ISIN of the asset or the symbol if it's a crypto."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The name or ticker of the asset to search for.",
                    }
                },
                "required": ["query"],
            },
        }
        formatted_tools = self._provider.format_tools([search_tool_def])

        while True:
            response = await self._provider._send_message(
                messages=self.messages,
                tools=formatted_tools,
                system=self.system_prompt(),
                output_config=output_config,
            )
            self.messages.append(self._provider.build_assistant_message(response))

            stop_reason = self._provider.extract_stop_reason(response)
            if stop_reason == "end_turn":
                break

            tool_uses = self._provider.extract_tool_uses(response)
            tool_result_blocks = []

            for tool_use in tool_uses:
                tool_name = tool_use["name"]
                tool_args = tool_use["input"]
                tool_id = tool_use["id"]

                if tool_name != "search_assets":
                    raise ValueError(f"L'outil {tool_name} n'existe pas.")

                result = search_assets(tool_args.get("query", ""), asset_type=asset_type)

                if not isinstance(result, str):
                    try:
                        result = json.dumps(result, ensure_ascii=False, default=str)
                    except Exception:
                        result = str(result)

                tool_result_blocks.append(
                    self._provider.build_tool_result_block(tool_id, result)
                )

            if tool_result_blocks:
                self.messages.append({"role": "user", "content": tool_result_blocks})

        return self._get_last_agent_text()

    def _get_last_agent_text(self) -> str:
        content = self.messages[-1].get("content", [])

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
