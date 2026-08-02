"""Pydantic schemas for AI script generation.

Defines request/response models for the LLM-powered test script generator:
- ScriptGenerateRequest: natural-language spec + product type + optional context
- ScriptGenerateResponse: generated code, confidence, validation errors, suggestions
- ScriptRefineRequest: existing code + feedback for iterative refinement
"""

from pydantic import BaseModel, Field


class ScriptGenerateRequest(BaseModel):
    """Request body for POST /api/v1/scripts/generate.

    Attributes:
        spec_text: Natural-language test specification (e.g. "power on 5V
            rail, check I2C communication").
        product_type: Product type identifier for context (e.g. "COMM-DEV-001").
        context: Optional additional context (instrument assignments, test
            limits, checkpoints) to guide script generation.
    """

    spec_text: str = Field(
        ...,
        min_length=1,
        max_length=5000,
        description="Natural-language test specification",
    )
    product_type: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Product type identifier",
    )
    context: dict[str, str] | None = Field(
        default=None,
        description="Optional additional context (instrument assignments, limits)",
    )


class ScriptGenerateResponse(BaseModel):
    """Response for POST /api/v1/scripts/generate.

    Attributes:
        code: Generated Python test script source code.
        confidence: Confidence score (0.0-1.0) based on validation passes.
        validation_errors: List of validation errors found during post-processing.
        suggestions: List of suggestions for improving the generated script.
    """

    code: str = Field(default="", description="Generated Python test script")
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence score based on validation results",
    )
    validation_errors: list[str] = Field(
        default_factory=list,
        description="Validation errors found during AST parse / security scan",
    )
    suggestions: list[str] = Field(
        default_factory=list,
        description="Suggestions for improving the script",
    )


class ScriptRefineRequest(BaseModel):
    """Request body for POST /api/v1/scripts/refine.

    Used for iterative refinement: the user provides the current code and
    feedback (e.g. "add retry logic", "change voltage to 3.3V"), and the
    LLM regenerates the script incorporating the feedback.

    Attributes:
        code: Current script source code.
        feedback: Natural-language feedback for refinement.
        product_type: Product type identifier for context.
    """

    code: str = Field(
        ...,
        min_length=1,
        description="Current script source code",
    )
    feedback: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Feedback for iterative refinement",
    )
    product_type: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Product type identifier",
    )
