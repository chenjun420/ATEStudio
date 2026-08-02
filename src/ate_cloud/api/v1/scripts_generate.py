"""Script Generation API endpoints - AI-powered test script generation.

Provides:
- ``POST /api/v1/scripts/generate`` - generate a Python test script from a
  natural-language specification + product type context.
- ``POST /api/v1/scripts/refine`` - iteratively refine an existing script
  based on user feedback (e.g. "add retry logic", "change voltage to 3.3V").

The generation pipeline: receive spec text -> build LLM prompt with ATE
Studio framework context -> call LLM via CircuitBreaker -> post-process
(AST validation, security scan, dependency check) -> return code +
confidence + validation errors + suggestions.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ate_cloud.config import settings
from ate_cloud.schemas.script_generator import (
    ScriptGenerateRequest,
    ScriptGenerateResponse,
    ScriptRefineRequest,
)
from ate_cloud.services.script_generator import (
    CircuitBreakerOpenError,
    LLMScriptGenerator,
)

router = APIRouter(prefix="/scripts", tags=["script-generation"])


def _get_script_generator(request: Request) -> LLMScriptGenerator:
    """Dependency: lazily create or retrieve LLMScriptGenerator from app state.

    Caches on app.state for reuse across requests. The CircuitBreaker
    state is shared so failure tracking persists.
    """
    service: LLMScriptGenerator | None = getattr(
        request.app.state, "script_generator", None
    )
    if service is not None:
        return service
    service = LLMScriptGenerator(api_key=settings.openai_api_key)
    request.app.state.script_generator = service
    return service


@router.post(
    "/generate",
    response_model=ScriptGenerateResponse,
    status_code=status.HTTP_200_OK,
)
async def generate_script(
    request_body: ScriptGenerateRequest,
    service: Annotated[LLMScriptGenerator, Depends(_get_script_generator)],
) -> ScriptGenerateResponse:
    """POST /api/v1/scripts/generate - generate a test script from natural language.

    Receives a natural-language test specification (e.g. "power on 5V rail,
    check I2C communication") and product type, calls an LLM to generate a
    Python test script, then post-processes with AST validation, security
    scan, and dependency check.

    Returns:
        ScriptGenerateResponse with generated code, confidence score,
        validation errors, and improvement suggestions.

    Raises:
        HTTPException: 503 if the LLM circuit breaker is OPEN or no API key.
        HTTPException: 502 if the LLM call fails for other reasons.
    """
    try:
        result = await service.generate(
            spec_text=request_body.spec_text,
            product_config=request_body.context,
        )
    except CircuitBreakerOpenError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"LLM circuit breaker open: {e}",
        ) from e
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Script generation failed: {e}",
        ) from e

    return ScriptGenerateResponse(
        code=result.code,
        confidence=result.confidence,
        validation_errors=result.validation_errors,
        suggestions=result.suggestions,
    )


@router.post(
    "/refine",
    response_model=ScriptGenerateResponse,
    status_code=status.HTTP_200_OK,
)
async def refine_script(
    request_body: ScriptRefineRequest,
    service: Annotated[LLMScriptGenerator, Depends(_get_script_generator)],
) -> ScriptGenerateResponse:
    """POST /api/v1/scripts/refine - iteratively refine a generated script.

    Accepts the current script code and natural-language feedback (e.g.
    "add retry logic", "change voltage to 3.3V"), calls the LLM to
    regenerate the script incorporating the feedback, then post-processes
    the result with the same validation pipeline.

    Returns:
        ScriptGenerateResponse with refined code, confidence, validation
        errors, and suggestions.

    Raises:
        HTTPException: 503 if the LLM circuit breaker is OPEN or no API key.
        HTTPException: 502 if the LLM call fails.
    """
    try:
        result = await service.refine(
            code=request_body.code,
            feedback=request_body.feedback,
            product_config=None,
        )
    except CircuitBreakerOpenError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"LLM circuit breaker open: {e}",
        ) from e
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Script refinement failed: {e}",
        ) from e

    return ScriptGenerateResponse(
        code=result.code,
        confidence=result.confidence,
        validation_errors=result.validation_errors,
        suggestions=result.suggestions,
    )
