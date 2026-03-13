"""In-process recipe cache for reusing parameterized API-call + SQL pipelines."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Any, Callable

import structlog
from rapidfuzz import fuzz

from ..config import settings
from .naming import sanitize_tool_name

logger = structlog.get_logger(__name__)


def _log_recipe(msg: str) -> None:
    """Log recipe activity only in debug mode."""
    if settings.DEBUG:
        logger.debug("recipe_trace", detail=msg)


def sha256_hex(text: str) -> str:
    """Hash text, normalizing JSON when possible for stable schema hashes."""
    normalized = text
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if parsed is not None:
        normalized = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


def normalize_ws(text: str) -> str:
    """Whitespace-normalize for template equivalence checks."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def render_text_template(template: str, params: dict[str, Any]) -> str:
    """Render {{param}} placeholders using raw string insertion (template carries quoting).

    Parameter values containing '{{' are rejected to prevent template injection.
    """

    def _as_text(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if v is None:
            return "null"
        return str(v)

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in params:
            raise KeyError(f"missing param: {name}")
        text = _as_text(params[name])
        if "{{" in text:
            raise ValueError(
                "parameter '" + name + "' contains template syntax '{{' which is not allowed"
            )
        return text

    return _PLACEHOLDER_RE.sub(repl, template)


def render_sql_safe(template: str, params: dict[str, Any]) -> str:
    """Render {{param}} placeholders with SQL-safe escaping.

    Unlike render_text_template(), this escapes string values to prevent
    SQL injection. Use for recipe SQL steps only (not GraphQL/gRPC templates).

    Escaping:
    - Single quotes doubled (SQL standard: ' → '')
    - Semicolons stripped (prevent multi-statement injection)
    - Comment markers stripped (``--``, ``/*``, ``*/``)
    - Numeric/bool/None pass through unquoted

    Limitations:
    - This is a string-based sanitizer, not a full SQL parser. It cannot
      detect injection patterns that span multiple parameters or exploit
      encoding tricks (e.g. Unicode homoglyphs, double-encoding).
    - It does not validate SQL syntax or semantics — only individual
      parameter values are escaped.
    - It does not prevent abuse of legitimate SQL operators (LIKE, UNION)
      within parameter values beyond stripping known injection vectors.
    - String values are **not** auto-quoted.  The template must supply
      surrounding quotes (e.g., ``'{{param}}'`` not ``{{param}}``).
    - For defense-in-depth, always combine with DuckDB sandboxing
      (read-only mode, restricted functions) as provided by ``executor.py``.
    """

    def _safe_text(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if v is None:
            return "null"
        if isinstance(v, (int, float)):
            return str(v)
        # String values: escape single quotes + strip injection vectors
        s = (
            str(v)
            .replace("'", "''")
            .replace(";", "")
            .replace("--", "")
            .replace("/*", "")
            .replace("*/", "")
        )
        return s

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in params:
            raise KeyError(f"missing param: {name}")
        return _safe_text(params[name])

    return _PLACEHOLDER_RE.sub(repl, template)


def render_param_refs(obj: Any, params: dict[str, Any]) -> Any:
    """Recursively replace {'$param': 'x'} nodes with params['x']."""
    if isinstance(obj, dict):
        if set(obj.keys()) == {"$param"} and isinstance(obj.get("$param"), str):
            pname = obj["$param"]
            if pname not in params:
                raise KeyError(f"missing param: {pname}")
            return params[pname]
        return {k: render_param_refs(v, params) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render_param_refs(v, params) for v in obj]
    return obj


def get_example_values(
    params_spec: dict[str, Any] | None, provided: dict[str, Any] | None
) -> dict[str, Any]:
    """Extract example values from param specs, overlaid with provided values.

    Stored "default" values are examples from the original execution, not
    runtime fallbacks.  Used by the extractor to verify template equivalence.
    """
    out: dict[str, Any] = {}
    if isinstance(params_spec, dict):
        for pname, spec in params_spec.items():
            if isinstance(spec, dict) and "default" in spec:
                out[pname] = spec["default"]
    if isinstance(provided, dict):
        out.update(provided)
    return out


def _normalize_question(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip().lower())


def _tokens(q: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", _normalize_question(q)))


def _similarity(query: str, signature: str) -> float:
    """Similarity score using RapidFuzz token-based matching."""
    q_norm = _normalize_question(query)
    s_norm = _normalize_question(signature)
    if not q_norm or not s_norm:
        return 0.0
    if q_norm == s_norm:
        return 1.0

    q_tokens = _tokens(query)
    s_tokens = _tokens(signature)
    if not q_tokens or not s_tokens:
        return 0.0

    q_text = " ".join(sorted(q_tokens))
    s_text = " ".join(sorted(s_tokens))
    base = fuzz.token_set_ratio(q_text, s_text)

    partial_fn = getattr(fuzz, "partial_token_set_ratio", None)
    extra: float = (
        partial_fn(q_text, s_text) if callable(partial_fn) else fuzz.WRatio(q_text, s_text)
    )

    overlap = len(q_tokens & s_tokens) / max(len(q_tokens), 1)
    coverage = len(s_tokens & q_tokens) / max(len(s_tokens), 1)
    token_balance = min(overlap, coverage) * 100.0

    return (0.55 * base + 0.25 * extra + 0.20 * token_balance) / 100.0


@dataclass
class RecipeRecord:
    recipe_id: str
    api_id: str
    schema_hash: str
    question: str
    question_sig: str
    question_tokens: set[str]
    recipe: dict[str, Any]
    tool_name: str  # LLM-generated function name for this recipe
    created_at: float
    last_used_at: float


class RecipeStore:
    """Async-safe global recipe store with simple intent matching and LRU eviction.

    This store is designed for **single-tenant** use: it is a global in-memory
    cache shared across all requests in the process.  There is no namespace
    isolation between different users or API keys.

    If multi-tenant isolation is required in the future, each tenant would need
    its own ``RecipeStore`` instance (or an internal namespace key) so that
    recipes from one tenant are never visible to another.  The ``api_id`` +
    ``schema_hash`` key partially segments data by target API, but does not
    provide security isolation.
    """

    def __init__(self, max_size: int = 64) -> None:
        self._max_size = max(1, int(max_size))
        self._lock = asyncio.Lock()
        self._records: dict[str, RecipeRecord] = {}
        self._by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._lru: OrderedDict[str, None] = OrderedDict()

    async def save_recipe(
        self,
        *,
        api_id: str,
        schema_hash: str,
        question: str,
        recipe: dict[str, Any],
        tool_name: str,
    ) -> str:
        recipe_id = f"r_{uuid.uuid4().hex[:8]}"
        now = time.time()
        record = RecipeRecord(
            recipe_id=recipe_id,
            api_id=api_id,
            schema_hash=schema_hash,
            question=question,
            question_sig=_normalize_question(question),
            question_tokens=_tokens(question),
            recipe=dict(recipe),
            tool_name=tool_name,
            created_at=now,
            last_used_at=now,
        )

        async with self._lock:
            self._records[recipe_id] = record
            self._by_key[(api_id, schema_hash)].add(recipe_id)
            self._touch(recipe_id)
            self._evict_if_needed()

        params = list(recipe.get("params", {}).keys())
        _log_recipe(f"SAVE {recipe_id} params={params} q={question[:40]}")
        return recipe_id

    async def save_recipe_if_unique(
        self,
        *,
        api_id: str,
        schema_hash: str,
        question: str,
        recipe: dict[str, Any],
        tool_name: str,
        equivalence_checker: Callable[[dict[str, Any], dict[str, Any]], bool] | None = None,
    ) -> str | None:
        """Atomically check for duplicates and save in a single lock acquisition.

        This avoids the TOCTOU race where two concurrent coroutines both see
        no duplicate and both insert.  The *equivalence_checker* receives
        ``(existing_recipe_dict, candidate_recipe_dict)`` and should return
        ``True`` if they are equivalent.

        Returns the new recipe_id on success, or ``None`` if a duplicate was
        found (or no checker was supplied and the save proceeds normally).
        """
        recipe_id = f"r_{uuid.uuid4().hex[:8]}"
        now = time.time()
        record = RecipeRecord(
            recipe_id=recipe_id,
            api_id=api_id,
            schema_hash=schema_hash,
            question=question,
            question_sig=_normalize_question(question),
            question_tokens=_tokens(question),
            recipe=dict(recipe),
            tool_name=tool_name,
            created_at=now,
            last_used_at=now,
        )

        async with self._lock:
            if equivalence_checker is not None:
                key = (api_id, schema_hash)
                for rid in list(self._by_key.get(key, set())):
                    existing = self._records.get(rid)
                    if existing and equivalence_checker(existing.recipe, recipe):
                        return None

            self._records[recipe_id] = record
            self._by_key[(api_id, schema_hash)].add(recipe_id)
            self._touch(recipe_id)
            self._evict_if_needed()

        params = list(recipe.get("params", {}).keys())
        _log_recipe(f"SAVE {recipe_id} params={params} q={question[:40]}")
        return recipe_id

    async def get_recipe(self, recipe_id: str) -> dict[str, Any] | None:
        async with self._lock:
            rec = self._records.get(recipe_id)
            if not rec:
                return None
            rec.last_used_at = time.time()
            self._touch(recipe_id)
            return dict(rec.recipe)

    async def get_recipe_meta(self, recipe_id: str) -> dict[str, Any] | None:
        """Return {api_id, schema_hash, recipe} for safety checks."""
        async with self._lock:
            rec = self._records.get(recipe_id)
            if not rec:
                return None
            rec.last_used_at = time.time()
            self._touch(recipe_id)
            return {
                "api_id": rec.api_id,
                "schema_hash": rec.schema_hash,
                "recipe": dict(rec.recipe),
            }

    async def suggest_recipes(
        self,
        *,
        api_id: str,
        schema_hash: str,
        question: str,
        k: int = 3,
    ) -> list[dict[str, Any]]:
        q_sig = _normalize_question(question)
        key = (api_id, schema_hash)
        async with self._lock:
            ids = list(self._by_key.get(key, set()))
            recs = [self._records[i] for i in ids if i in self._records]

        min_score = settings.RECIPE_MIN_SIMILARITY / 100.0
        scored: list[tuple[float, RecipeRecord]] = []
        for r in recs:
            score = _similarity(q_sig, r.question_sig)
            if score >= min_score:
                scored.append((score, r))

        scored.sort(key=lambda t: (t[0], t[1].last_used_at), reverse=True)
        out: list[dict[str, Any]] = []
        for score, r in scored[: max(0, int(k))]:
            out.append(
                {
                    "recipe_id": r.recipe_id,
                    "score": round(score, 4),
                    "created_at": r.created_at,
                    "last_used_at": r.last_used_at,
                    "question": r.question,
                    "tool_name": r.tool_name,
                }
            )

        if out:
            matches = " ".join(f"{s['recipe_id']}({s['score']:.2f})" for s in out)
            _log_recipe(f"SUGGEST found={len(out)} [{matches}]")
        return out

    async def list_recipes(
        self,
        *,
        api_id: str,
        schema_hash: str,
    ) -> list[dict[str, Any]]:
        """List recipes for a given api_id + schema_hash."""
        key = (api_id, schema_hash)
        async with self._lock:
            ids = list(self._by_key.get(key, set()))
            recs = [self._records[i] for i in ids if i in self._records]

        recs.sort(key=lambda r: r.last_used_at, reverse=True)
        out: list[dict[str, Any]] = []
        for r in recs:
            out.append(
                {
                    "recipe_id": r.recipe_id,
                    "question": r.question,
                    "tool_name": r.tool_name,
                    "created_at": r.created_at,
                    "last_used_at": r.last_used_at,
                    "params": dict(r.recipe.get("params", {})),
                    "steps": list(r.recipe.get("steps", [])),
                    "sql_steps": list(r.recipe.get("sql_steps", [])),
                }
            )
        return out

    async def find_recipe_by_tool_slug(
        self,
        *,
        api_id: str,
        schema_hash: str,
        tool_slug: str,
        max_slug_len: int | None = None,
    ) -> dict[str, Any] | None:
        """Find the most recent recipe by tool_slug for an api_id + schema_hash."""
        key = (api_id, schema_hash)
        if not (isinstance(max_slug_len, int) and max_slug_len > 0):
            max_slug_len = None

        async with self._lock:
            ids = list(self._by_key.get(key, set()))
            recs: list[RecipeRecord] = []
            for i in ids:
                rec = self._records.get(i)
                if not rec:
                    continue
                slug = sanitize_tool_name(rec.tool_name)
                if max_slug_len:
                    slug = slug[:max_slug_len]
                if slug == tool_slug:
                    recs.append(rec)

        if not recs:
            return None
        recs.sort(key=lambda r: (r.last_used_at, r.created_at), reverse=True)
        r = recs[0]
        return {
            "recipe_id": r.recipe_id,
            "question": r.question,
            "tool_name": r.tool_name,
            "created_at": r.created_at,
            "last_used_at": r.last_used_at,
            "params": dict(r.recipe.get("params", {})),
            "steps": list(r.recipe.get("steps", [])),
            "sql_steps": list(r.recipe.get("sql_steps", [])),
        }

    def _touch(self, recipe_id: str) -> None:
        self._lru.pop(recipe_id, None)
        self._lru[recipe_id] = None

    def _evict_if_needed(self) -> None:
        while len(self._records) > self._max_size and self._lru:
            oldest_id = next(iter(self._lru))
            self._delete(oldest_id)

    def _delete(self, recipe_id: str) -> None:
        rec = self._records.pop(recipe_id, None)
        self._lru.pop(recipe_id, None)
        if not rec:
            return
        key = (rec.api_id, rec.schema_hash)
        ids = self._by_key.get(key)
        if ids:
            ids.discard(recipe_id)
            if not ids:
                self._by_key.pop(key, None)


RECIPE_STORE = RecipeStore(max_size=settings.RECIPE_CACHE_SIZE)
