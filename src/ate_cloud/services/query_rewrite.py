"""Golden-Retriever query rewriting for hybrid diagnosis.

Disambiguates production-test jargon (I2C, SPI, BGA, ESD, ...) BEFORE
retrieval: a deterministic local dictionary expands known abbreviations
(always available, key-free), and an optional LLM call further augments the
query when an OpenAI-compatible key is configured. The LLM call is protected
by a CircuitBreaker; if the LLM is unavailable the dictionary-expanded query
is returned (logged, not silent) — the dictionary step is a first-class,
deterministic part of the pattern, not a degraded mode.

Extracted from ``hybrid_retriever`` to keep that module under the size
ceiling; behavior is unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from ate_cloud.config import settings
from ate_platform.common.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)

#: System prompt for LLM query rewriting — output only the rewritten query.
_REWRITE_SYSTEM_PROMPT = (
    "You are an electronics test engineering expert. "
    "Rewrite the following fault query to include disambiguated domain terms. "
    "Expand abbreviations and add relevant technical context. "
    "Output ONLY the rewritten query text, nothing else."
)

#: Domain dictionary for electronics testing jargon (Golden-Retriever pattern).
#: Maps abbreviations to full expansions (English gloss).
_DOMAIN_DICTIONARY: dict[str, str] = {
    "I2C": "Inter-Integrated Circuit (two-wire serial communication protocol)",
    "SPI": "Serial Peripheral Interface (synchronous serial communication protocol)",
    "UART": "Universal Asynchronous Receiver-Transmitter (serial communication)",
    "USART": "Universal Synchronous/Asynchronous Receiver-Transmitter",
    "Vreg": "Voltage Regulator (voltage stabilization component)",
    "BGA": "Ball Grid Array (integrated circuit package type)",
    "PCB": "Printed Circuit Board",
    "SMT": "Surface Mount Technology (component assembly method)",
    "ESD": "Electrostatic Discharge (sudden electrical transfer between objects)",
    "THD": "Total Harmonic Distortion (signal quality metric)",
    "SNR": "Signal-to-Noise Ratio (signal quality metric)",
    "BER": "Bit Error Rate (digital communication quality metric)",
    "JTAG": "Joint Test Action Group (boundary-scan test interface)",
    "CAN": "Controller Area Network (vehicle communication bus)",
    "USB": "Universal Serial Bus",
    "PCIe": "PCI Express (high-speed serial computer expansion bus)",
    "DDR": "Double Data Rate (synchronous dynamic RAM)",
    "GPIO": "General-Purpose Input/Output",
    "ADC": "Analog-to-Digital Converter",
    "DAC": "Digital-to-Analog Converter",
    "PWM": "Pulse Width Modulation",
    "RF": "Radio Frequency",
    "EMI": "Electromagnetic Interference",
    "EMC": "Electromagnetic Compatibility",
    "DMM": "Digital Multimeter",
    "OSC": "Oscilloscope",
    "PSU": "Power Supply Unit",
    "DUT": "Device Under Test",
    "FMEA": "Failure Mode and Effects Analysis",
    "HALT": "Highly Accelerated Life Test",
    "HASS": "Highly Accelerated Stress Screening",
    "ICT": "In-Circuit Test",
    "FCT": "Functional Circuit Test",
    "BODE": "Bode plot (frequency response analysis)",
    "LDO": "Low Dropout (linear voltage regulator)",
    "SOC": "System on Chip",
    "FPGA": "Field-Programmable Gate Array",
    "ASIC": "Application-Specific Integrated Circuit",
    "MCU": "Microcontroller Unit",
    "EEPROM": "Electrically Erasable Programmable Read-Only Memory",
}


def lookup_domain_terms(query: str) -> list[tuple[str, str]]:
    """Find domain jargon in ``query`` and return (abbreviation, expansion) pairs.

    Case-insensitive word-boundary matching (an abbreviation must appear as a
    standalone token, never as a substring of a longer word — so ``SPI`` does
    not match inside ``SPIDER``). Results are ordered by first appearance.
    """
    query_upper = query.upper()
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for abbr, full in _DOMAIN_DICTIONARY.items():
        abbr_upper = abbr.upper()
        if abbr_upper in query_upper and abbr_upper not in seen:
            idx = query_upper.find(abbr_upper)
            before_ok = idx == 0 or not query_upper[idx - 1].isalnum()
            after_idx = idx + len(abbr_upper)
            after_ok = after_idx >= len(query_upper) or not query_upper[after_idx].isalnum()
            if before_ok and after_ok:
                found.append((abbr, full))
                seen.add(abbr_upper)
    return found


def dictionary_expand(query: str) -> str:
    """Augment ``query`` with dictionary expansions for any jargon found."""
    expansions = lookup_domain_terms(query)
    if not expansions:
        return query
    expansion_text = "; ".join(f"{abbr} = {full}" for abbr, full in expansions)
    return f"{query} ({expansion_text})"


class QueryRewriter:
    """Golden-Retriever query rewriter: dictionary expansion + optional LLM.

    Args:
        api_key: OpenAI-compatible API key. When falsy, only the
            deterministic dictionary expansion runs (retrieval-only mode).
        model: Chat model name for LLM augmentation.
        breaker: CircuitBreaker protecting the LLM call.
    """

    def __init__(
        self,
        api_key: str | None,
        model: str | None,
        breaker: CircuitBreaker,
    ) -> None:
        self._api_key = api_key or settings.openai_api_key
        self._model = model or settings.openai_model
        self._breaker = breaker
        self._llm: Any = None
        self._prompt: Any = None
        self._initialized = False

    async def rewrite(self, query: str) -> str:
        """Rewrite ``query`` (dictionary expansion, then LLM if configured)."""
        augmented = dictionary_expand(query)
        if not self._api_key:
            logger.debug("No OpenAI API key; using dictionary-expanded query")
            return augmented
        try:
            self._ensure_initialized()
            llm_result = await self._call_llm(augmented)
            if llm_result.strip():
                return llm_result.strip()
            return augmented
        except CircuitBreakerOpenError:
            logger.warning("LLM circuit breaker open; using dictionary-expanded query")
            return augmented
        except Exception as e:  # noqa: BLE001 — rewrite must never break retrieval
            logger.warning("LLM query rewriting failed: %s; using dictionary-expanded query", e)
            return augmented

    def _ensure_initialized(self) -> None:
        """Lazily initialize LangChain chat model/prompt (deferred import)."""
        if self._initialized:
            return
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr

        kwargs: dict[str, Any] = {
            "model": self._model,
            "api_key": SecretStr(self._api_key),
            "temperature": 0,
        }
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        self._llm = ChatOpenAI(**kwargs)
        self._prompt = ChatPromptTemplate.from_messages([
            ("system", _REWRITE_SYSTEM_PROMPT),
            ("human", "{query}"),
        ])
        self._initialized = True

    async def _call_llm(self, query: str) -> str:
        """Call the LLM through the CircuitBreaker."""
        async def _do() -> str:
            messages = self._prompt.format_messages(query=query)
            response = await self._llm.ainvoke(messages)
            return str(response.content)

        return await self._breaker.call(_do)


__all__ = ["QueryRewriter", "dictionary_expand", "lookup_domain_terms"]
