# Interface Contract: TOONEncoder

**Feature**: 001-toon-format-integration  
**Date**: 2026-02-23  
**Phase**: 1 (Design & Contracts)
**Status**: Draft

## 1. Overview

This document specifies the public API contract for the `TOONEncoder` class. This class is the primary interface for TOON serialization within Ratatoskr. It encapsulates the `toon-format` library, adding a layer of error handling, observability, performance monitoring, and token estimation.

All interactions with the TOON format **MUST** go through this class to ensure consistent error handling, fallback logic, and metric collection.

**Module**: `api_agent.llm.toon_encoder.TOONEncoder`

## 2. Initialization

### `__init__(self, config: TOONConfig)`

**Description**: Initializes the encoder with a given configuration.

**Parameters**:
- `config` (`TOONConfig`): An instance of `TOONConfig` that specifies feature flags and behavior (e.g., timeouts, enabled features).

**Guarantees**:
- The constructor itself will not raise an exception.
- It will not load the `tiktoken` library at initialization time (lazy loading).

**Example**:
```python
from api_agent.llm.toon_encoder import TOONConfig, TOONEncoder

# Load config from environment
config = TOONConfig.from_env()

# Create an encoder instance
encoder = TOONEncoder(config=config)
```

## 3. Core Methods

### `encode(self, data: Any) -> str | None`

**Description**: Encodes a given Python data structure into a TOON-formatted string. This is the primary method for serialization.

**Parameters**:
- `data` (`Any`): The Python object (typically `dict` or `list`) to be serialized.

**Returns**:
- `str`: A valid TOON-formatted string on successful encoding.
- `None`: If encoding fails for any reason (internal library error, timeout, unsupported data type).

**Guarantees**:
- **No Exceptions**: This method **MUST NOT** raise any exceptions. It must catch all internal exceptions from the `toon-format` library and `TimeoutError`.
- **Idempotency**: Calling `encode` multiple times on the same input data (that does not contain non-deterministic elements like `datetime.now()`) **MUST** produce the same output string.
- **Performance**:
    - For input data < 10KB, P95 latency **MUST** be < 10ms.
    - For input data < 100KB, P95 latency **MUST** be < 50ms (or the configured `encoding_timeout_ms`).
- **Validity**: The returned string, if not `None`, **MUST** be a syntactically valid TOON string.

**Failure Modes**:
- **Internal Error**: `toon-format` library raises an exception.
    - **Action**: Log the error with stack trace and data sample. Return `None`.
- **Timeout**: Encoding exceeds `config.encoding_timeout_ms`.
    - **Action**: Log a warning. Return `None`.
- **Unsupported Type**: `data` contains a type that cannot be serialized (e.g., a function, a circular reference).
    - **Action**: Log the error. Return `None`.

### `estimate_tokens(self, data: Any) -> tuple[int, int]`

**Description**: Estimates and returns the token counts for both JSON and TOON representations of the input data. This method is crucial for observability and measuring savings.

**Parameters**:
- `data` (`Any`): The Python object to be measured.

**Returns**:
- `tuple[int, int]`: A tuple containing `(json_tokens, toon_tokens)`.
    - `json_tokens`: The estimated token count for the JSON-serialized data.
    - `toon_tokens`: The estimated token count for the TOON-serialized data. If TOON encoding fails, this value will be equal to `json_tokens` to ensure a compression ratio of 0.

**Guarantees**:
- **No Exceptions**: This method **MUST NOT** raise exceptions, including `ImportError` if `tiktoken` is not installed.
- **Graceful Degradation**: If `tiktoken` is not installed, it **MUST** return `(0, 0)` and log a warning.
- **Consistency**: `toon_tokens` **MUST** be less than or equal to `json_tokens`.
- **Accuracy**: Token counts are estimates based on OpenAI's `cl100k_base` tokenizer. They are suitable for calculating compression ratios but may differ from actual provider-billed tokens (especially for Anthropic).

**Failure Modes**:
- **TOON Encoding Fails**: If the internal call to `encode(data)` returns `None`.
    - **Action**: `toon_tokens` will be set to `json_tokens`. The method will still succeed.
- **`tiktoken` Not Installed**: If the `tiktoken` library is not available.
    - **Action**: Log a warning on first use. Return `(0, 0)`.

## 4. Utility Methods

### `compare_formats(self, data: Any) -> ComparisonResult`

**Description**: Provides a detailed, side-by-side comparison of JSON and TOON encodings for debugging, analysis, and documentation.

**Parameters**:
- `data` (`Any`): The Python object to be compared.

**Returns**:
- `ComparisonResult`: A dataclass instance containing the full encoded strings, token counts, character sizes, compression ratio, latency, and any error messages.

**Guarantees**:
- **No Exceptions**: This method **MUST NOT** raise exceptions.
- **Completeness**: The returned `ComparisonResult` object **MUST** accurately reflect the outcomes of both JSON and TOON encoding attempts, including any errors.

## 5. Observability Contract

Interaction with `TOONEncoder` **MUST** produce the following observability data.

### OpenTelemetry Spans

- A new span named **`toon.encode`** **MUST** be created for every call to the `encode` method.
- This span **MUST** contain the following attributes upon completion:
    - `toon.input_size_bytes` (int): Size of the input Python object.
    - `toon.encoding_time_ms` (float): Wall-clock time for the encoding process.
    - `toon.output_size_chars` (int): Length of the resulting TOON string, or 0 on failure.
    - `toon.timed_out` (bool): `True` if the operation failed due to a timeout.
- If an error occurs, the span **MUST**:
    - Have its status set to `ERROR`.
    - Contain the `toon.error` (string) attribute with the exception message.

### Metrics

The following metrics **MUST** be emitted:

- **`ratatoskr.toon.encoding.errors`** (Counter): Incremented by 1 every time `encode()` returns `None`.
    - **Attributes**: `reason: "timeout" | "exception"`
- **`ratatoskr.toon.encoding.latency`** (Histogram): Records the duration of every successful `encode()` call.
    - **Unit**: milliseconds

## 6. Thread Safety and Concurrency

The `TOONEncoder` instance is intended to be created once per application startup (or per request, depending on the dependency injection framework).

**Guarantees**:
- All methods (`encode`, `estimate_tokens`) **MUST** be thread-safe.
- The lazy-loading of the `tiktoken` encoder **MUST** be handled in a thread-safe manner (e.g., using `threading.Lock`).

## 7. Versioning and Evolution

This contract is versioned as **v1.0** for the initial implementation.

- **Minor Version Changes (1.x)**: Will be backward-compatible. New optional parameters may be added to methods. New methods may be added.
- **Major Version Changes (2.0)**: May include backward-incompatible changes, such as renaming methods, changing return types, or altering required parameters. Major changes will require a new design review and contract update.
