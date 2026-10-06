"""Strict request and typed result contracts for the optional decision tool."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal, Mapping, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator, model_validator


MAX_REQUEST_BYTES = 64 * 1024
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$", re.ASCII)
FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$", re.ASCII)

Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
ShortText = Annotated[str, StringConstraints(strict=True, min_length=1)]
QuestionText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2000)]
ExcerptText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=8000)]
LocatorText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ChoiceOption(StrictModel):
    id: Identifier
    description: QuestionText


class ChoiceQuestion(StrictModel):
    type: Literal["choice"]
    instructions: QuestionText
    options: tuple[ChoiceOption, ...] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def unique_option_ids(self) -> "ChoiceQuestion":
        ids = [option.id for option in self.options]
        if len(ids) != len(set(ids)):
            raise ValueError("choice option identifiers must be unique")
        return self

    def kev_question(self) -> "KevQuestion":
        return KevQuestion(kind="choice", instructions=self.instructions,
                           options=tuple(KevOption(id=o.id, description=o.description) for o in self.options))


class YesNoQuestion(StrictModel):
    type: Literal["yes_no"]
    instructions: QuestionText
    true_description: Annotated[str, StringConstraints(strict=True, max_length=2000)] | None = None
    false_description: Annotated[str, StringConstraints(strict=True, max_length=2000)] | None = None

    def kev_question(self) -> "KevQuestion":
        return KevQuestion(kind="noul", instructions=self.instructions,
                           options=(KevOption(id="true", description=self.true_description or "True"),
                                    KevOption(id="false", description=self.false_description or "False")))


class ScoreQuestion(StrictModel):
    type: Literal["score"]
    instructions: QuestionText
    levels: tuple[QuestionText, ...] = Field(min_length=2, max_length=8)

    def kev_question(self) -> "KevQuestion":
        return KevQuestion(kind="score", instructions=self.instructions,
                           options=tuple(KevOption(id=str(index), description=level)
                                         for index, level in enumerate(self.levels)),
                           levels=self.levels)


DecisionQuestion = Annotated[Union[ChoiceQuestion, YesNoQuestion, ScoreQuestion], Field(discriminator="type")]


class SuppliedEvidence(StrictModel):
    id: Identifier
    text: ExcerptText
    source_locator: LocatorText
    origin: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
    retrieved_at: Annotated[str, StringConstraints(strict=True, max_length=100)] | None = None
    status: Annotated[str, StringConstraints(strict=True, max_length=500)] | None = None
    version: Annotated[str, StringConstraints(strict=True, max_length=500)] | None = None
    original_reference: dict[str, Any] | None = None


class DecisionRequest(StrictModel):
    objective: Annotated[str, StringConstraints(strict=True, min_length=3, max_length=4000)]
    query: Annotated[str, StringConstraints(strict=True, min_length=3, max_length=4000)]
    questions: dict[Identifier, DecisionQuestion] = Field(min_length=1, max_length=4)
    supplied_evidence: tuple[SuppliedEvidence, ...] = Field(default=(), max_length=8)
    seen_fingerprints: tuple[Annotated[str, StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$")], ...] = Field(default=(), max_length=64)

    @field_validator("questions")
    @classmethod
    def valid_question_keys(cls, questions: dict[str, Any]) -> dict[str, Any]:
        for identifier in questions:
            if not IDENTIFIER_RE.fullmatch(identifier):
                raise ValueError("question identifiers must use ASCII letters, digits, underscores, or hyphens")
        return questions

    @model_validator(mode="after")
    def unique_supplied_ids(self) -> "DecisionRequest":
        identifiers = [item.id for item in self.supplied_evidence]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("supplied evidence identifiers must be unique")
        return self


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def validate_decision_request(value: Mapping[str, Any] | str | bytes | bytearray) -> DecisionRequest:
    """Validate a mapping or raw JSON request, enforcing the byte cap first.

    Raw JSON is preferred at a transport boundary because only it can preserve and
    detect duplicate object keys before the JSON decoder discards them.
    """
    if isinstance(value, (str, bytes, bytearray)):
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("decision request exceeds 64 KiB")
        json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
        return DecisionRequest.model_validate_json(raw, strict=True)
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("decision request must contain JSON-compatible values") from exc
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("decision request exceeds 64 KiB")
    return DecisionRequest.model_validate_json(raw, strict=True)


class KevOption(StrictModel):
    id: Identifier
    description: ShortText


class KevQuestion(StrictModel):
    kind: Literal["choice", "noul", "score"]
    instructions: QuestionText
    options: tuple[KevOption, ...] = Field(min_length=2, max_length=8)
    levels: tuple[str, ...] = ()


class QuestionDistribution(StrictModel):
    probabilities: dict[Identifier, float]
    selected: Identifier | None = None
    score: float | None = None
    confidence: float | None = None


class ContextReference(StrictModel):
    source_locator: str | None
    original_reference: dict[str, Any] | None = None
    reference_id: str | None = None
    upstream_chunk_id: str | None = None
    source_ids: tuple[str, ...] = ()
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    caller_supplied: bool = False
    origin: str | None = None
    retrieved_at: str | None = None
    status: str | None = None
    version: str | None = None
    caller_evidence_id: str | None = None


class DecisionContextItem(StrictModel):
    id: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
    kind: Literal["supplied_passage", "retrieved_passage", "graph_assertion"]
    text: str
    references: tuple[ContextReference, ...] = ()
    supporting_item_ids: tuple[Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)], ...] = ()


class OmittedContextItem(StrictModel):
    id: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
    reason: Literal["token_budget", "unresolved_source_link", "duplicate"]


class ContextLimitations(StrictModel):
    items: tuple[str, ...] = ()


class ContextTokenCounts(StrictModel):
    state: int = Field(ge=0)
    question_branches: int = Field(ge=0)
    final_sequence: int = Field(ge=0)


class DecisionContext(StrictModel):
    state_text: str
    items: tuple[DecisionContextItem, ...]
    included_item_ids: tuple[str, ...]
    omitted_items: tuple[OmittedContextItem, ...]
    limitations: ContextLimitations
    token_counts: ContextTokenCounts
    status: Literal["ready", "insufficient_context"]


class DecisionAnswer(StrictModel):
    probabilities: dict[Identifier, float]
    selected: Identifier | None = None
    probability_true: float | None = None
    expected_score: float | None = None
    confidence: float | None = None


class ModelMetadata(StrictModel):
    resolved_model: str
    base: str
    tokenizer: str
    code_revision: str
    dtype: str
    temperature: float = Field(gt=0)
    execution_device: Literal["cpu", "cuda"] | None = None
    device_name: str | None = None
    cuda_runtime: str | None = None


class DecisionTimings(StrictModel):
    retrieval_ms: float = Field(ge=0)
    preparation_ms: float = Field(ge=0)
    inference_ms: float | None = Field(default=None, ge=0)


class DecisionResult(StrictModel):
    status: Literal["evaluated", "insufficient_context", "unavailable", "failed"]
    answers: dict[Identifier, DecisionAnswer] = Field(default_factory=dict)
    context: DecisionContext
    novelty_fingerprints: tuple[str, ...] = ()
    timings: DecisionTimings
    model: ModelMetadata | None = None
    calibration_status: Literal["not_validated_for_domain"] = "not_validated_for_domain"

    @model_validator(mode="after")
    def no_probabilities_without_evaluation(self) -> "DecisionResult":
        if self.status != "evaluated" and self.answers:
            raise ValueError("outcomes without inference cannot contain model probabilities")
        if self.status == "evaluated" and self.model is None:
            raise ValueError("evaluated outcomes require model metadata")
        return self
