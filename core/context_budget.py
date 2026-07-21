from __future__ import annotations

import base64
from dataclasses import dataclass, field
import functools
import hashlib
import importlib
import importlib.metadata
import math
import os
import stat
import tempfile
import time
import types
from typing import Any, Callable, Iterable, Mapping

from core.schema import validate_schema


CONTEXT_BUDGET_SCHEMA_VERSION = "1.0"
# Installed from the calibration-only side of
# tests/fixtures/token_calibration/synthetic_acceptance_v1.json.  The source
# is deliberately named in the version: it is production wiring and offline
# acceptance evidence, not a claim of real-provider accuracy.  The frozen
# holdout has zero observed under-estimation; the normal ContextBudget safety
# ratio is applied separately.
ESTIMATOR_VERSION = "mixed-endpoint-synthetic-calibration-v1"
CALIBRATED_TOKENS_PER_UTF8_BYTE = 7 / 18
ESTIMATOR_DATASET_SOURCE = "synthetic_acceptance_v1"
ESTIMATOR_CALIBRATION_MANIFEST_HASH = (
    "60564e43724b947ce70b6054e5e72af58c35de3fd47dca6c603b7922e1f06edc"
)
ESTIMATOR_HOLDOUT_MANIFEST_HASH = (
    "864b3798e6eafee2a0c7e20c093278981fdbecc76bfecdf0923a197c00a42a1e"
)
ESTIMATOR_REAL_PROVIDER_VERIFIED = False
ESTIMATOR_CALIBRATION_METHOD = "max-observed-tokens-per-utf8-byte-v1"
# The fitted ratio controls normal admission.  A previous enforcement profile
# took max(calibrated, every UTF-8 byte), which reduced every realistic request
# to byte_count + framing and made calibration behaviorally irrelevant.  The
# v2 profile keeps the calibrated margin for non-ASCII bytes while retaining
# a one-token-per-byte floor for the ASCII portion of unknown-tokenizer input.
# This protects JSON syntax, escapes, punctuation, hashes, and high-entropy
# ASCII without charging each byte of ordinary Chinese as a token.
ESTIMATOR_ENFORCEMENT_METHOD = "ascii-byte-floor-plus-nonascii-calibration-v2"
# Retained as compatibility/report metadata: there is no longer a global byte
# floor in the enforcement formula.
ESTIMATOR_ENFORCEMENT_FLOOR_TOKENS_PER_UTF8_BYTE = 0.0
ESTIMATOR_ASCII_FLOOR_TOKENS_PER_BYTE = 1.0
# Predeclared allowance for provider-side message framing that is not present
# in canonical request JSON. It is intentionally independent of holdout data.
ESTIMATOR_ENFORCEMENT_FIXED_OVERHEAD_TOKENS = 64
ESTIMATOR_ENFORCEMENT_SAFETY_RATIO = 0.15
MODEL_TOKENIZER_FIXED_OVERHEAD_TOKENS = 64
_TIKTOKEN_CACHE_BLOBS: dict[str, tuple[tuple[str, str], ...]] = {
    "gpt2": (
        (
            "https://openaipublic.blob.core.windows.net/gpt-2/encodings/main/vocab.bpe",
            "1ce1664773c50f3e0cc8842619a93edc4624525b728b188a9e0be33b7726adc5",
        ),
        (
            "https://openaipublic.blob.core.windows.net/gpt-2/encodings/main/encoder.json",
            "196139668be63f3b5d6574427317ae82f612a97c5d1cdaf36ed2256dbf636783",
        ),
    ),
    "r50k_base": (
        (
            "https://openaipublic.blob.core.windows.net/encodings/r50k_base.tiktoken",
            "306cd27f03c1a714eca7108e03d66b7dc042abe8c258b44c199a7ed9838dd930",
        ),
    ),
    "p50k_base": (
        (
            "https://openaipublic.blob.core.windows.net/encodings/p50k_base.tiktoken",
            "94b5ca7dff4d00767bc256fdd1b27e5b17361d7b8a5f968547f9f23eb70d2069",
        ),
    ),
    "p50k_edit": (
        (
            "https://openaipublic.blob.core.windows.net/encodings/p50k_base.tiktoken",
            "94b5ca7dff4d00767bc256fdd1b27e5b17361d7b8a5f968547f9f23eb70d2069",
        ),
    ),
    "cl100k_base": (
        (
            "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken",
            "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
        ),
    ),
    "o200k_base": (
        (
            "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken",
            "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
        ),
    ),
    "o200k_harmony": (
        (
            "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken",
            "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
        ),
    ),
}
_TIKTOKEN_PINNED_VERSION = "0.13.0"
_TIKTOKEN_MAX_ASSET_BYTES = 16 * 1024 * 1024
_TIKTOKEN_EXPECTED_RANK_COUNTS = {
    "r50k_base": 50_256,
    "p50k_base": 50_280,
    "p50k_edit": 50_280,
    "cl100k_base": 100_256,
    "o200k_base": 199_998,
    "o200k_harmony": 199_998,
}
NEW_TOKEN_COUNT_MODES = frozenset(
    {
        "provider_exact",
        "model_tokenizer",
        "calibrated_estimate",
    }
)
LEGACY_TOKEN_COUNT_MODES = frozenset({"exact", "estimate"})
SAFE_ENDPOINT_TYPES = frozenset({"official", "openai_compatible", "unknown"})
TokenCounterCallable = Callable[[str], int]
ExactTokenCounter = TokenCounterCallable


class ContextBudgetError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _require_endpoint_type(endpoint_type: str) -> None:
    if endpoint_type not in SAFE_ENDPOINT_TYPES:
        raise ContextBudgetError(
            "endpoint_type_invalid",
            f"endpoint_type must be one of {sorted(SAFE_ENDPOINT_TYPES)}",
        )


def _require_safe_metadata(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContextBudgetError("token_metadata_invalid", f"{name} must be a non-empty string")
    if len(value) > 200 or any(ord(character) < 32 for character in value):
        raise ContextBudgetError(
            "token_metadata_invalid",
            f"{name} must be a compact, control-character-free label",
        )


@dataclass(frozen=True)
class CalibratedTokenEstimator:
    """Deterministic, offline token estimate with explicit calibration metadata."""

    version: str = ESTIMATOR_VERSION
    tokens_per_utf8_byte: float = CALIBRATED_TOKENS_PER_UTF8_BYTE
    fixed_overhead_tokens: int = 0

    def __post_init__(self) -> None:
        _require_safe_metadata(self.version, "calibration version")
        ratio = self.tokens_per_utf8_byte
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(ratio) or ratio <= 0:
            raise ContextBudgetError(
                "token_calibration_invalid",
                "tokens_per_utf8_byte must be a positive finite number",
            )
        overhead = self.fixed_overhead_tokens
        if isinstance(overhead, bool) or not isinstance(overhead, int) or overhead < 0:
            raise ContextBudgetError(
                "token_calibration_invalid",
                "fixed_overhead_tokens must be a non-negative integer",
            )

    def estimate(self, text: str) -> int:
        byte_count = len(str(text).encode("utf-8"))
        return math.ceil(byte_count * self.tokens_per_utf8_byte) + self.fixed_overhead_tokens

    def __call__(self, text: str) -> int:
        return self.estimate(text)


DEFAULT_CALIBRATED_ESTIMATOR = CalibratedTokenEstimator()
CJK_CHARACTER_OUTPUT_ESTIMATOR = CalibratedTokenEstimator(
    version="cjk-one-token-per-character-v1",
    tokens_per_utf8_byte=1 / 3,
)


@dataclass(frozen=True)
class TokenCounter:
    """A counter bound to the model and endpoint it is safe to describe."""

    counter: TokenCounterCallable
    count_mode: str
    provider: str
    model: str
    endpoint_type: str
    version: str
    tokenizer: str | None = None
    model_is_known: bool = False
    fixed_overhead_tokens: int = 0

    def __post_init__(self) -> None:
        if not callable(self.counter):
            raise ContextBudgetError("token_counter_invalid", "counter must be callable")
        if self.count_mode not in {"provider_exact", "model_tokenizer"}:
            raise ContextBudgetError(
                "token_counter_invalid",
                "counter mode must be provider_exact or model_tokenizer",
            )
        _require_safe_metadata(self.provider, "counter provider")
        _require_safe_metadata(self.model, "counter model")
        _require_endpoint_type(self.endpoint_type)
        _require_safe_metadata(self.version, "counter version")
        if not isinstance(self.model_is_known, bool):
            raise ContextBudgetError("token_counter_invalid", "model_is_known must be boolean")
        if (
            isinstance(self.fixed_overhead_tokens, bool)
            or not isinstance(self.fixed_overhead_tokens, int)
            or self.fixed_overhead_tokens < 0
        ):
            raise ContextBudgetError(
                "token_counter_invalid",
                "fixed_overhead_tokens must be a non-negative integer",
            )
        if self.count_mode == "provider_exact" and self.fixed_overhead_tokens:
            raise ContextBudgetError(
                "token_counter_invalid",
                "provider_exact counters cannot add fixed overhead tokens",
            )
        if self.tokenizer is not None:
            _require_safe_metadata(self.tokenizer, "tokenizer")
        if self.count_mode == "model_tokenizer" and not self.tokenizer:
            raise ContextBudgetError(
                "token_counter_invalid",
                "model_tokenizer requires an explicit tokenizer name",
            )
        if self.count_mode == "provider_exact" and (
            self.endpoint_type != "official" or not self.model_is_known
        ):
            raise ContextBudgetError(
                "token_counter_unsafe_exact",
                "provider_exact requires an official endpoint and an explicitly known model",
            )

    def count(self, text: str) -> int:
        value = self.counter(text)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContextBudgetError("token_counter_invalid", "token counter returned an invalid value")
        return value + self.fixed_overhead_tokens

    def metadata(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "endpoint_type": self.endpoint_type,
            "model_is_known": self.model_is_known,
            "counter_source": "provider_usage" if self.count_mode == "provider_exact" else "tokenizer",
        }
        if self.tokenizer:
            result["tokenizer"] = self.tokenizer
            result["tokenizer_version"] = self.version
            result["tokenizer_fixed_overhead_tokens"] = self.fixed_overhead_tokens
        else:
            result["provider_counter_version"] = self.version
        return result


def model_token_counter(
    *,
    provider: str,
    model: str,
    endpoint_type: str,
) -> TokenCounter | None:
    """Return a model-bound local tokenizer when its mapping is explicit.

    A local tokenizer is an admission estimate, never provider-exact usage.
    Only an official OpenAI endpoint is auto-bound. Compatible endpoints may
    advertise an OpenAI-looking alias without using the same tokenizer, so they
    and unknown models fall back to calibrated admission unless an embedding
    caller explicitly supplies a bound counter.
    """

    _require_safe_metadata(provider, "counter provider")
    _require_safe_metadata(model, "counter model")
    _require_endpoint_type(endpoint_type)
    if provider.strip().lower() != "openai" or endpoint_type != "official":
        return None
    try:
        import tiktoken

        encoding_name = tiktoken.encoding_name_for_model(model)
        encoding = _cached_tiktoken_encoding(encoding_name)
        if encoding is None:
            return None
    except Exception:  # Missing package, unknown model, or unavailable cached data.
        return None
    try:
        version = importlib.metadata.version("tiktoken")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"

    def count(text: str) -> int:
        return len(encoding.encode(str(text), disallowed_special=()))

    return TokenCounter(
        counter=count,
        count_mode="model_tokenizer",
        provider=provider,
        model=model,
        endpoint_type=endpoint_type,
        version=f"tiktoken-{version}",
        tokenizer=str(encoding.name),
        model_is_known=True,
        fixed_overhead_tokens=MODEL_TOKENIZER_FIXED_OVERHEAD_TOKENS,
    )


def _cached_tiktoken_encoding(encoding_name: str) -> Any | None:
    """Load an already-local encoding without invoking tiktoken's URL loader."""

    try:
        if importlib.metadata.version("tiktoken") != _TIKTOKEN_PINNED_VERSION:
            return None
    except importlib.metadata.PackageNotFoundError:
        return None
    blobs = _TIKTOKEN_CACHE_BLOBS.get(encoding_name)
    # GPT-2's legacy data-gym format uses two source files. Unknown or
    # multi-file formats deliberately fall back to calibration rather than
    # entering the upstream loader.
    if not blobs or len(blobs) != 1:
        return None
    if "TIKTOKEN_CACHE_DIR" in os.environ:
        cache_dir = os.environ["TIKTOKEN_CACHE_DIR"]
    elif "DATA_GYM_CACHE_DIR" in os.environ:
        cache_dir = os.environ["DATA_GYM_CACHE_DIR"]
    else:
        cache_dir = os.path.join(tempfile.gettempdir(), "data-gym-cache")
    if not cache_dir:
        return None
    blob_url, expected_hash = blobs[0]
    cache_key = hashlib.sha1(blob_url.encode("utf-8")).hexdigest()
    cache_path = os.path.join(cache_dir, cache_key)
    try:
        return _load_local_tiktoken_encoding(
            encoding_name,
            cache_path,
            blob_url,
            expected_hash,
        )
    except (ImportError, OSError, TypeError, ValueError):
        return None


@functools.lru_cache(maxsize=8)
def _load_local_tiktoken_encoding(
    encoding_name: str,
    cache_path: str,
    blob_url: str,
    expected_hash: str,
) -> Any:
    """Construct an Encoding from one hash-verified local BPE snapshot.

    The complete asset is read and verified before parsing.  The installed
    constructor is cloned with a local rank loader, so this code never calls
    tiktoken's cache-miss downloader and has no check/use race with that loader.
    """

    cache_stat = os.stat(cache_path, follow_symlinks=False)
    if not stat.S_ISREG(cache_stat.st_mode):
        raise OSError("cached tiktoken asset is not a regular file")
    if cache_stat.st_size > _TIKTOKEN_MAX_ASSET_BYTES:
        raise ValueError("cached tiktoken asset exceeds the size limit")
    with open(cache_path, "rb") as handle:
        contents = handle.read(_TIKTOKEN_MAX_ASSET_BYTES + 1)
    if len(contents) > _TIKTOKEN_MAX_ASSET_BYTES:
        raise ValueError("cached tiktoken asset exceeds the size limit")
    if hashlib.sha256(contents).hexdigest() != expected_hash:
        raise ValueError("cached tiktoken asset hash mismatch")
    mergeable_ranks: dict[bytes, int] = {}
    for line in contents.splitlines():
        if not line:
            continue
        try:
            encoded_token, encoded_rank = line.split()
            token = base64.b64decode(encoded_token, validate=True)
            rank = int(encoded_rank)
        except Exception as exc:
            raise ValueError("cached tiktoken asset is malformed") from exc
        if token in mergeable_ranks:
            raise ValueError("cached tiktoken asset contains duplicate tokens")
        mergeable_ranks[token] = rank
    expected_count = _TIKTOKEN_EXPECTED_RANK_COUNTS.get(encoding_name)
    if expected_count is None or len(mergeable_ranks) != expected_count:
        raise ValueError("cached tiktoken asset has an unexpected rank count")
    ranks = set(mergeable_ranks.values())
    if len(ranks) != expected_count or min(ranks) != 0 or max(ranks) != expected_count - 1:
        raise ValueError("cached tiktoken asset ranks are not unique and contiguous")

    public = importlib.import_module("tiktoken_ext.openai_public")
    constructors = getattr(public, "ENCODING_CONSTRUCTORS", {})
    constructor = constructors.get(encoding_name)
    if constructor is None:
        raise ValueError("installed tiktoken has no matching constructor")

    manifest_hash = expected_hash
    loader_calls = 0

    def local_rank_loader(
        requested_path: str,
        expected_hash: str | None = None,
    ) -> dict[bytes, int]:
        nonlocal loader_calls
        if requested_path != blob_url:
            raise ValueError("tiktoken constructor requested an unexpected asset")
        if expected_hash != manifest_hash:
            raise ValueError("tiktoken constructor expected an unexpected asset hash")
        loader_calls += 1
        return dict(mergeable_ranks)

    def clone_with_local_loader(source: Any) -> Any:
        local_globals = dict(source.__globals__)
        local_globals["load_tiktoken_bpe"] = local_rank_loader
        return types.FunctionType(
            source.__code__,
            local_globals,
            name=source.__name__,
            argdefs=source.__defaults__,
            closure=source.__closure__,
        )

    local_constructor = clone_with_local_loader(constructor)
    if encoding_name == "o200k_harmony":
        base_constructor = constructors.get("o200k_base")
        if base_constructor is None:
            raise ValueError("installed tiktoken has no o200k base constructor")
        local_constructor.__globals__["o200k_base"] = clone_with_local_loader(
            base_constructor
        )
    config = local_constructor()
    if not isinstance(config, dict) or config.get("name") != encoding_name:
        raise ValueError("tiktoken constructor returned mismatched metadata")
    if loader_calls != 1 or config.get("mergeable_ranks") != mergeable_ranks:
        raise ValueError("tiktoken constructor bypassed the verified local ranks")
    import tiktoken

    return tiktoken.Encoding(**config)


@dataclass(frozen=True)
class ContextBudget:
    provider: str
    model: str
    model_context_window: int
    output_reserve_tokens: int = 8_000
    protocol_overhead_tokens: int = 1_000
    safety_margin_tokens: int = 1_000
    max_input_tokens: int = 32_000
    story_project_tokens: int = 16_000
    previous_chapter_tokens: int = 6_000
    safety_ratio: float = 0.15
    endpoint_type: str = "unknown"
    bound_token_counter: TokenCounter | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        integer_fields = (
            "model_context_window",
            "output_reserve_tokens",
            "protocol_overhead_tokens",
            "safety_margin_tokens",
            "max_input_tokens",
            "story_project_tokens",
            "previous_chapter_tokens",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContextBudgetError("context_budget_invalid", f"{name} must be a non-negative integer")
        if self.model_context_window < 1 or self.max_input_tokens < 1:
            raise ContextBudgetError("context_budget_invalid", "context window and max input must be positive")
        if not 0 <= self.safety_ratio < 1:
            raise ContextBudgetError("context_budget_invalid", "safety_ratio must be in [0, 1)")
        if not self.provider.strip() or not self.model.strip():
            raise ContextBudgetError("context_budget_invalid", "provider and model must be explicit")
        _require_safe_metadata(self.provider, "provider")
        _require_safe_metadata(self.model, "model")
        _require_endpoint_type(self.endpoint_type)
        if self.bound_token_counter is not None:
            self._validate_counter_binding(self.bound_token_counter)
        if self.usable_input_tokens <= 0:
            raise ContextBudgetError("context_budget_invalid", "reserves leave no usable input tokens")

    @property
    def usable_input_tokens(self) -> int:
        return max(
            0,
            self.model_context_window
            - self.output_reserve_tokens
            - self.protocol_overhead_tokens
            - self.safety_margin_tokens,
        )

    @property
    def hard_input_limit(self) -> int:
        return min(self.max_input_tokens, self.usable_input_tokens)

    def measure(
        self,
        text: str,
        *,
        stage: str,
        exact_counter: ExactTokenCounter | None = None,
        token_counter: TokenCounter | None = None,
        calibrated_estimator: CalibratedTokenEstimator | None = None,
        protocol_texts: Iterable[str] = (),
    ) -> dict[str, Any]:
        combined = "\n".join([*(str(item) for item in protocol_texts), str(text)])
        if token_counter is not None and exact_counter is not None:
            raise ContextBudgetError(
                "token_counter_invalid",
                "token_counter and legacy exact_counter are mutually exclusive",
            )
        if calibrated_estimator is not None and (token_counter is not None or exact_counter is not None):
            raise ContextBudgetError(
                "token_counter_invalid",
                "calibrated_estimator cannot be combined with a token counter",
            )

        effective_counter = token_counter
        if effective_counter is None and exact_counter is not None:
            effective_counter = _legacy_model_tokenizer(
                exact_counter,
                provider=self.provider,
                model=self.model,
                endpoint_type=self.endpoint_type,
            )
        if (
            effective_counter is None
            and calibrated_estimator is None
            and exact_counter is None
        ):
            effective_counter = self.bound_token_counter

        if effective_counter is not None:
            self._validate_counter_binding(effective_counter)
            raw_tokens = effective_counter.count(combined)
            mode = effective_counter.count_mode
            counter_version = effective_counter.version
            budgeted_tokens = raw_tokens
            count_metadata = effective_counter.metadata()
        else:
            estimator = calibrated_estimator or DEFAULT_CALIBRATED_ESTIMATOR
            raw_tokens = estimator.estimate(combined)
            mode = "calibrated_estimate"
            counter_version = estimator.version
            budgeted_tokens = math.ceil(raw_tokens * (1 + self.safety_ratio))
            count_metadata = {
                "provider": self.provider,
                "model": self.model,
                "endpoint_type": self.endpoint_type,
                "model_is_known": False,
                "counter_source": "calibration",
                "calibration_version": estimator.version,
            }
            if estimator is DEFAULT_CALIBRATED_ESTIMATOR:
                budgeted_tokens = conservative_calibrated_token_estimate(
                    combined,
                    estimator=estimator,
                    safety_ratio=self.safety_ratio,
                )
                count_metadata.update(
                    {
                        "calibration_source": ESTIMATOR_DATASET_SOURCE,
                        "calibration_manifest_hash": ESTIMATOR_CALIBRATION_MANIFEST_HASH,
                        "holdout_manifest_hash": ESTIMATOR_HOLDOUT_MANIFEST_HASH,
                        "holdout_role": "evaluation_only",
                        "calibration_real_provider_verified": ESTIMATOR_REAL_PROVIDER_VERIFIED,
                        "calibration_method": ESTIMATOR_CALIBRATION_METHOD,
                        "calibration_tokens_per_utf8_byte": estimator.tokens_per_utf8_byte,
                        "enforcement_method": ESTIMATOR_ENFORCEMENT_METHOD,
                        "enforcement_floor_tokens_per_utf8_byte": (
                            ESTIMATOR_ENFORCEMENT_FLOOR_TOKENS_PER_UTF8_BYTE
                        ),
                        "ascii_floor_tokens_per_byte": (
                            ESTIMATOR_ASCII_FLOOR_TOKENS_PER_BYTE
                        ),
                        "enforcement_fixed_overhead_tokens": (
                            ESTIMATOR_ENFORCEMENT_FIXED_OVERHEAD_TOKENS
                        ),
                        "enforcement_safety_ratio": self.safety_ratio,
                        "ascii_floor_applied": any(
                            ord(character) < 128 for character in combined
                        ),
                    }
                )
        report = {
            "schema_version": CONTEXT_BUDGET_SCHEMA_VERSION,
            "stage": stage,
            "provider": self.provider,
            "model": self.model,
            "model_context_window": self.model_context_window,
            "usable_input_tokens": self.usable_input_tokens,
            "hard_input_limit": self.hard_input_limit,
            "raw_input_tokens": raw_tokens,
            "budgeted_input_tokens": budgeted_tokens,
            "count_mode": mode,
            "counter_version": str(counter_version),
            "count_metadata": count_metadata,
            "within_budget": budgeted_tokens <= self.hard_input_limit,
            "context_digest": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
        }
        return validate_schema(report, "context_budget_report.schema.json")

    def _validate_counter_binding(self, counter: TokenCounter) -> None:
        if counter.provider != self.provider or counter.model != self.model:
            raise ContextBudgetError(
                "token_counter_binding_mismatch",
                "token counter provider/model does not match the context budget",
            )
        if counter.endpoint_type != self.endpoint_type:
            raise ContextBudgetError(
                "token_counter_binding_mismatch",
                "token counter endpoint_type does not match the context budget",
            )

    def require_input(self, text: str, *, stage: str, **kwargs: Any) -> dict[str, Any]:
        report = self.measure(text, stage=stage, **kwargs)
        if not report["within_budget"]:
            raise ContextBudgetError(
                "story_project_context_budget_exceeded",
                f"{stage} input requires {report['budgeted_input_tokens']} tokens; "
                f"hard limit is {report['hard_input_limit']}",
            )
        return report


@dataclass(frozen=True)
class RunBudgetLimits:
    max_provider_calls: int = 20
    max_total_input_tokens: int = 160_000
    max_total_output_tokens: int = 40_000
    max_elapsed_seconds: float = 900.0
    max_estimated_cost: float | None = None


class RunBudgetTracker:
    def __init__(
        self,
        limits: RunBudgetLimits,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self._now = now
        self._started_at = now()
        self.provider_calls = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.reserved_output_tokens = 0
        self.estimated_cost = 0.0
        self._model_reservations: dict[str, dict[str, Any]] = {}

    def reserve_call(self, input_tokens: int, *, estimated_cost: float = 0.0) -> None:
        self._check_elapsed()
        self._require_non_negative(input_tokens, "input_tokens")
        if self.provider_calls + 1 > self.limits.max_provider_calls:
            raise ContextBudgetError("run_provider_call_budget_exceeded", "max_provider_calls exceeded")
        if self.total_input_tokens + input_tokens > self.limits.max_total_input_tokens:
            raise ContextBudgetError("run_input_token_budget_exceeded", "max_total_input_tokens exceeded")
        if (
            self.limits.max_estimated_cost is not None
            and self.estimated_cost + estimated_cost > self.limits.max_estimated_cost
        ):
            raise ContextBudgetError("run_estimated_cost_budget_exceeded", "max_estimated_cost exceeded")
        self.provider_calls += 1
        self.total_input_tokens += input_tokens
        self.estimated_cost += estimated_cost

    def record_output(self, output_tokens: int, *, estimated_cost: float = 0.0) -> None:
        self._check_elapsed()
        self._require_non_negative(output_tokens, "output_tokens")
        if self.total_output_tokens + output_tokens > self.limits.max_total_output_tokens:
            raise ContextBudgetError("run_output_token_budget_exceeded", "max_total_output_tokens exceeded")
        if (
            self.limits.max_estimated_cost is not None
            and self.estimated_cost + estimated_cost > self.limits.max_estimated_cost
        ):
            raise ContextBudgetError("run_estimated_cost_budget_exceeded", "max_estimated_cost exceeded")
        self.total_output_tokens += output_tokens
        self.estimated_cost += estimated_cost

    def reserve_model_call(
        self,
        *,
        input_tokens: int,
        max_output_tokens: int,
        call_id: str,
        attempt_id: str,
        estimated_cost: float = 0.0,
    ) -> None:
        """Reserve one physical attempt, including its timeout upper bound."""

        self._check_elapsed()
        self._require_non_negative(input_tokens, "input_tokens")
        self._require_non_negative(max_output_tokens, "max_output_tokens")
        if not isinstance(call_id, str) or not call_id:
            raise ContextBudgetError("run_budget_usage_invalid", "call_id must be non-empty")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ContextBudgetError("run_budget_usage_invalid", "attempt_id must be non-empty")
        if attempt_id in self._model_reservations:
            raise ContextBudgetError(
                "run_budget_attempt_conflict",
                f"attempt_id {attempt_id} was already reserved",
            )
        if self.provider_calls + 1 > self.limits.max_provider_calls:
            raise ContextBudgetError("run_provider_call_budget_exceeded", "max_provider_calls exceeded")
        if self.total_input_tokens + input_tokens > self.limits.max_total_input_tokens:
            raise ContextBudgetError("run_input_token_budget_exceeded", "max_total_input_tokens exceeded")
        if (
            self.total_output_tokens
            + self.reserved_output_tokens
            + max_output_tokens
            > self.limits.max_total_output_tokens
        ):
            raise ContextBudgetError(
                "run_output_token_budget_exceeded",
                "reserved output exceeds remaining max_total_output_tokens",
            )
        if (
            self.limits.max_estimated_cost is not None
            and self.estimated_cost + estimated_cost > self.limits.max_estimated_cost
        ):
            raise ContextBudgetError("run_estimated_cost_budget_exceeded", "max_estimated_cost exceeded")
        self.provider_calls += 1
        self.total_input_tokens += input_tokens
        self.reserved_output_tokens += max_output_tokens
        self.estimated_cost += estimated_cost
        self._model_reservations[attempt_id] = {
            "call_id": call_id,
            "reserved_input_tokens": input_tokens,
            "max_output_tokens": max_output_tokens,
            "status": "reserved",
            "actual_input_tokens": None,
            "actual_output_tokens": None,
            "settlement_error": None,
        }

    def ensure_model_call(
        self,
        *,
        input_tokens: int,
        max_output_tokens: int,
        call_id: str,
        attempt_id: str,
        estimated_cost: float = 0.0,
    ) -> bool:
        """Idempotently restore or create one immutable attempt reservation.

        Returns ``True`` when a new reservation was charged and ``False`` when
        the exact attempt was already present.  A reused attempt id with
        different immutable budget evidence remains a hard conflict.
        """

        self._require_non_negative(input_tokens, "input_tokens")
        self._require_non_negative(max_output_tokens, "max_output_tokens")
        if not isinstance(call_id, str) or not call_id:
            raise ContextBudgetError(
                "run_budget_usage_invalid", "call_id must be non-empty"
            )
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ContextBudgetError(
                "run_budget_usage_invalid", "attempt_id must be non-empty"
            )
        existing = self._model_reservations.get(attempt_id)
        if existing is not None:
            expected = {
                "call_id": call_id,
                "reserved_input_tokens": input_tokens,
                "max_output_tokens": max_output_tokens,
            }
            if any(existing.get(field) != value for field, value in expected.items()):
                raise ContextBudgetError(
                    "run_budget_attempt_conflict",
                    f"attempt_id {attempt_id} conflicts with its existing reservation",
                )
            return False
        self.reserve_model_call(
            input_tokens=input_tokens,
            max_output_tokens=max_output_tokens,
            call_id=call_id,
            attempt_id=attempt_id,
            estimated_cost=estimated_cost,
        )
        return True

    def record_model_response(
        self,
        *,
        response: Any,
        call_id: str,
        attempt_id: str,
    ) -> None:
        """Settle a reservation from provider usage or a conservative fallback."""

        reservation = self._model_reservations.get(attempt_id)
        if reservation is None or reservation.get("call_id") != call_id:
            raise ContextBudgetError(
                "run_budget_attempt_missing",
                f"attempt_id {attempt_id} has no matching reservation",
            )
        if reservation["status"] == "settled":
            settlement_error = reservation.get("settlement_error")
            if settlement_error is not None:
                code, message = settlement_error
                raise ContextBudgetError(str(code), str(message))
            return
        self._check_elapsed()
        reserved_input = int(reservation["reserved_input_tokens"])
        reserved_output = int(reservation["max_output_tokens"])
        fallback_output = conservative_token_estimate(
            str(getattr(response, "text", response))
        )
        usage = _model_response_token_usage(response)
        actual_input, actual_output = usage.settlement(
            reserved_input_tokens=reserved_input,
            reserved_output_tokens=reserved_output,
            fallback_output_tokens=fallback_output,
        )
        prospective_input = self.total_input_tokens - reserved_input + actual_input
        prospective_output = (
            self.total_output_tokens
            + self.reserved_output_tokens
            - reserved_output
            + actual_output
        )
        # The provider call and its successful Receipt already exist.  Settle
        # the tracker truthfully before reporting an overrun so failure records
        # and restart hydration charge the same actual usage.
        self.total_input_tokens = prospective_input
        self.reserved_output_tokens -= reserved_output
        self.total_output_tokens += actual_output
        reservation["status"] = "settled"
        reservation["actual_input_tokens"] = actual_input
        reservation["actual_output_tokens"] = actual_output
        settlement_error: tuple[str, str] | None = None
        if usage.invalid:
            settlement_error = (
                "run_budget_usage_invalid",
                "provider token usage is malformed or internally contradictory",
            )
        elif prospective_input > self.limits.max_total_input_tokens:
            settlement_error = (
                "run_input_token_budget_exceeded",
                "provider input usage exceeds max_total_input_tokens",
            )
        elif prospective_output > self.limits.max_total_output_tokens:
            settlement_error = (
                "run_output_token_budget_exceeded",
                "provider output exceeds remaining max_total_output_tokens",
            )
        reservation["settlement_error"] = settlement_error
        if settlement_error is not None:
            raise ContextBudgetError(*settlement_error)

    def remaining_seconds(self) -> float:
        return max(0.0, self.limits.max_elapsed_seconds - (self._now() - self._started_at))

    def report(self) -> dict[str, Any]:
        return {
            "provider_calls": self.provider_calls,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "charged_output_tokens": self.total_output_tokens + self.reserved_output_tokens,
            "unsettled_attempt_count": sum(
                item["status"] == "reserved" for item in self._model_reservations.values()
            ),
            "elapsed_seconds": max(0.0, self._now() - self._started_at),
            "estimated_cost": self.estimated_cost,
        }

    def _check_elapsed(self) -> None:
        if self._now() - self._started_at > self.limits.max_elapsed_seconds:
            raise ContextBudgetError("run_elapsed_budget_exceeded", "max_elapsed_seconds exceeded")

    @staticmethod
    def _require_non_negative(value: int, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContextBudgetError("run_budget_usage_invalid", f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class _ModelResponseTokenUsage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    invalid: bool = False

    def settlement(
        self,
        *,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        fallback_output_tokens: int,
    ) -> tuple[int, int]:
        if self.invalid:
            # A successful provider effect already exists, so fail closed while
            # preserving at least the entire reservation.  total_tokens is
            # charged to both independently bounded dimensions because an
            # inconsistent payload cannot safely reveal its allocation.
            total = self.total_tokens or 0
            return (
                max(reserved_input_tokens, self.input_tokens or 0, total),
                max(
                    reserved_output_tokens,
                    fallback_output_tokens,
                    self.output_tokens or 0,
                    total,
                ),
            )
        if (
            self.total_tokens is not None
            and self.input_tokens is None
            and self.output_tokens is None
        ):
            # With no allocation information, charging total to each separate
            # hard limit is the only representation that cannot understate one
            # of those limits.
            return (
                max(reserved_input_tokens, self.total_tokens),
                max(
                    reserved_output_tokens,
                    fallback_output_tokens,
                    self.total_tokens,
                ),
            )
        return (
            reserved_input_tokens if self.input_tokens is None else self.input_tokens,
            (
                max(reserved_output_tokens, fallback_output_tokens)
                if self.output_tokens is None
                else self.output_tokens
            ),
        )


def _model_response_token_usage(response: Any) -> _ModelResponseTokenUsage:
    usage = getattr(response, "usage", None)
    if not isinstance(usage, Mapping):
        return _ModelResponseTokenUsage(None, None, None)

    input_tokens, input_invalid = _first_usage_integer(
        usage,
        ("input_tokens", "prompt_tokens"),
        nested_key="input_tokens_details",
    )
    # Anthropic reports cache creation/read tokens as additional top-level
    # input components.  OpenAI cached_tokens lives in details and is already a
    # subset of prompt/input tokens, so it is deliberately not added here.
    cache_creation, cache_creation_invalid = _usage_field(
        usage, "cache_creation_input_tokens"
    )
    cache_read, cache_read_invalid = _usage_field(
        usage, "cache_read_input_tokens"
    )
    if cache_creation is not None or cache_read is not None:
        input_tokens = (input_tokens or 0) + (cache_creation or 0) + (cache_read or 0)

    output_tokens, output_invalid = _first_usage_integer(
        usage,
        ("output_tokens", "completion_tokens"),
        nested_key="output_tokens_details",
    )
    total_tokens, total_invalid = _usage_field(usage, "total_tokens")
    invalid = any(
        (
            input_invalid,
            cache_creation_invalid,
            cache_read_invalid,
            output_invalid,
            total_invalid,
        )
    )
    if total_tokens is not None:
        if input_tokens is not None and output_tokens is not None:
            invalid = invalid or input_tokens + output_tokens != total_tokens
        elif input_tokens is not None:
            if input_tokens <= total_tokens:
                output_tokens = total_tokens - input_tokens
            else:
                invalid = True
        elif output_tokens is not None:
            if output_tokens <= total_tokens:
                input_tokens = total_tokens - output_tokens
            else:
                invalid = True
    return _ModelResponseTokenUsage(
        input_tokens,
        output_tokens,
        total_tokens,
        invalid,
    )


def _first_usage_integer(
    usage: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    nested_key: str,
) -> tuple[int | None, bool]:
    invalid = False
    selected: int | None = None
    for key in keys:
        value, field_invalid = _usage_field(usage, key)
        invalid = invalid or field_invalid
        if value is not None:
            if selected is None:
                selected = value
            elif selected != value:
                invalid = True
    nested = usage.get(nested_key)
    if isinstance(nested, Mapping):
        value, field_invalid = _usage_field(nested, "total")
        invalid = invalid or field_invalid
        if value is not None:
            if selected is None:
                selected = value
            elif selected != value:
                invalid = True
    elif nested_key in usage:
        invalid = True
    return selected, invalid


def _usage_field(usage: Mapping[str, Any], key: str) -> tuple[int | None, bool]:
    if key not in usage:
        return None, False
    value = _usage_integer(usage.get(key))
    return value, value is None


def _usage_integer(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def default_context_budget(
    *,
    provider: str = "openai",
    model: str | None = None,
    endpoint_type: str | None = None,
    enable_model_tokenizer: bool = True,
) -> ContextBudget:
    resolved_model, resolved_endpoint_type = _runtime_model_binding(
        provider=provider,
        model=model,
        endpoint_type=endpoint_type,
    )
    window = _positive_env("NOVELAGENT_MODEL_CONTEXT_WINDOW", 128_000)
    max_input_tokens = _positive_env("NOVELAGENT_MAX_INPUT_TOKENS", 32_000)
    counter = (
        model_token_counter(
            provider=provider,
            model=resolved_model,
            endpoint_type=resolved_endpoint_type,
        )
        if enable_model_tokenizer
        else None
    )
    return ContextBudget(
        provider=provider,
        model=resolved_model,
        model_context_window=window,
        max_input_tokens=max_input_tokens,
        endpoint_type=resolved_endpoint_type,
        bound_token_counter=counter,
    )


def conservative_token_estimate(text: str) -> int:
    if not text:
        return 0
    return len(text.encode("utf-8"))


def conservative_calibrated_token_estimate(
    text: str,
    *,
    estimator: CalibratedTokenEstimator = DEFAULT_CALIBRATED_ESTIMATOR,
    safety_ratio: float = ESTIMATOR_ENFORCEMENT_SAFETY_RATIO,
    fixed_overhead_tokens: int = ESTIMATOR_ENFORCEMENT_FIXED_OVERHEAD_TOKENS,
) -> int:
    """Return the production reservation estimate for unknown tokenizers.

    Non-ASCII bytes use the calibrated estimate and its declared safety margin.
    The ASCII portion retains a one-token-per-byte floor so unknown tokenizers
    remain protected for JSON syntax, escapes, punctuation, and high-entropy
    atoms without globally charging Chinese once per UTF-8 byte.
    """

    if isinstance(safety_ratio, bool) or not isinstance(safety_ratio, (int, float)):
        raise ContextBudgetError(
            "token_calibration_invalid",
            "safety_ratio must be a finite non-negative number",
        )
    if not math.isfinite(safety_ratio) or safety_ratio < 0:
        raise ContextBudgetError(
            "token_calibration_invalid",
            "safety_ratio must be a finite non-negative number",
        )
    if (
        isinstance(fixed_overhead_tokens, bool)
        or not isinstance(fixed_overhead_tokens, int)
        or fixed_overhead_tokens < 0
    ):
        raise ContextBudgetError(
            "token_calibration_invalid",
            "fixed_overhead_tokens must be a non-negative integer",
        )
    source = str(text)
    ascii_bytes = sum(ord(character) < 128 for character in source)
    non_ascii_bytes = len(source.encode("utf-8")) - ascii_bytes
    ratio = estimator.tokens_per_utf8_byte
    ascii_calibrated = math.ceil(
        math.ceil(ascii_bytes * ratio) * (1 + safety_ratio)
    )
    ascii_floor = math.ceil(
        ascii_bytes * ESTIMATOR_ASCII_FLOOR_TOKENS_PER_BYTE
    )
    non_ascii_calibrated = math.ceil(
        (
            math.ceil(non_ascii_bytes * ratio)
            + estimator.fixed_overhead_tokens
        )
        * (1 + safety_ratio)
    )
    return (
        max(ascii_floor, ascii_calibrated)
        + non_ascii_calibrated
        + fixed_overhead_tokens
    )


def _runtime_model_binding(
    *,
    provider: str,
    model: str | None,
    endpoint_type: str | None,
) -> tuple[str, str]:
    normalized_provider = str(provider).strip().lower()
    resolved_model = str(model).strip() if model is not None else ""
    resolved_endpoint = str(endpoint_type).strip() if endpoint_type is not None else ""
    if normalized_provider in {"openai", "anthropic"} and (
        not resolved_model or not resolved_endpoint
    ):
        from core.config import get_config

        config = get_config()
        if normalized_provider == "openai":
            resolved_model = resolved_model or config.openai_model
            resolved_endpoint = resolved_endpoint or (
                "openai_compatible" if config.openai_base_url else "official"
            )
        else:
            resolved_model = resolved_model or config.claude_model or "runtime-default"
            resolved_endpoint = resolved_endpoint or (
                "unknown" if config.claude_base_url else "official"
            )
    return resolved_model or "runtime-default", resolved_endpoint or "unknown"


def preview_chinese_output_compatibility(
    max_output_tokens: int,
    *,
    minimum_chinese_chars: int = 3_000,
    maximum_chinese_chars: int = 4_500,
    calibrated_estimator: CalibratedTokenEstimator | None = None,
    safety_ratio: float = 0.15,
) -> dict[str, Any]:
    """Pure preview of whether an output cap can cover a Chinese target range."""

    for name, value in (
        ("max_output_tokens", max_output_tokens),
        ("minimum_chinese_chars", minimum_chinese_chars),
        ("maximum_chinese_chars", maximum_chinese_chars),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ContextBudgetError("output_compatibility_invalid", f"{name} must be a positive integer")
    if maximum_chinese_chars < minimum_chinese_chars:
        raise ContextBudgetError(
            "output_compatibility_invalid",
            "maximum_chinese_chars must be at least minimum_chinese_chars",
        )
    if isinstance(safety_ratio, bool) or not isinstance(safety_ratio, (int, float)) or not 0 <= safety_ratio < 1:
        raise ContextBudgetError("output_compatibility_invalid", "safety_ratio must be in [0, 1)")

    estimator = calibrated_estimator or DEFAULT_CALIBRATED_ESTIMATOR
    minimum_raw_tokens = estimator.estimate("字" * minimum_chinese_chars)
    maximum_raw_tokens = estimator.estimate("字" * maximum_chinese_chars)
    minimum_required_tokens = math.ceil(minimum_raw_tokens * (1 + safety_ratio))
    maximum_required_tokens = math.ceil(maximum_raw_tokens * (1 + safety_ratio))
    minimum_compatible = max_output_tokens >= minimum_required_tokens
    full_range_compatible = max_output_tokens >= maximum_required_tokens
    return {
        "minimum_chinese_chars": minimum_chinese_chars,
        "maximum_chinese_chars": maximum_chinese_chars,
        "max_output_tokens": max_output_tokens,
        "minimum_required_tokens": minimum_required_tokens,
        "maximum_required_tokens": maximum_required_tokens,
        "minimum_target_compatible": minimum_compatible,
        "full_target_range_compatible": full_range_compatible,
        "compatible": full_range_compatible,
        "shortfall_tokens": max(0, maximum_required_tokens - max_output_tokens),
        "count_mode": "calibrated_estimate",
        "calibration_version": estimator.version,
    }


def _legacy_model_tokenizer(
    counter: ExactTokenCounter,
    *,
    provider: str,
    model: str,
    endpoint_type: str,
) -> TokenCounter:
    version = str(getattr(counter, "version", None) or "legacy-tokenizer-v1")
    tokenizer = str(getattr(counter, "tokenizer", None) or getattr(counter, "__name__", None) or "legacy-callable")
    return TokenCounter(
        counter=counter,
        count_mode="model_tokenizer",
        provider=provider,
        model=model,
        endpoint_type=endpoint_type,
        version=version,
        tokenizer=tokenizer,
        model_is_known=bool(getattr(counter, "model_is_known", False)),
    )


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


__all__ = [
    "CalibratedTokenEstimator",
    "CJK_CHARACTER_OUTPUT_ESTIMATOR",
    "CONTEXT_BUDGET_SCHEMA_VERSION",
    "ContextBudget",
    "ContextBudgetError",
    "DEFAULT_CALIBRATED_ESTIMATOR",
    "ESTIMATOR_CALIBRATION_METHOD",
    "ESTIMATOR_CALIBRATION_MANIFEST_HASH",
    "ESTIMATOR_DATASET_SOURCE",
    "ESTIMATOR_ASCII_FLOOR_TOKENS_PER_BYTE",
    "ESTIMATOR_ENFORCEMENT_FIXED_OVERHEAD_TOKENS",
    "ESTIMATOR_ENFORCEMENT_FLOOR_TOKENS_PER_UTF8_BYTE",
    "ESTIMATOR_ENFORCEMENT_METHOD",
    "ESTIMATOR_ENFORCEMENT_SAFETY_RATIO",
    "ESTIMATOR_HOLDOUT_MANIFEST_HASH",
    "ESTIMATOR_REAL_PROVIDER_VERIFIED",
    "ESTIMATOR_VERSION",
    "LEGACY_TOKEN_COUNT_MODES",
    "NEW_TOKEN_COUNT_MODES",
    "MODEL_TOKENIZER_FIXED_OVERHEAD_TOKENS",
    "RunBudgetLimits",
    "RunBudgetTracker",
    "SAFE_ENDPOINT_TYPES",
    "TokenCounter",
    "conservative_calibrated_token_estimate",
    "conservative_token_estimate",
    "default_context_budget",
    "model_token_counter",
    "preview_chinese_output_compatibility",
]
