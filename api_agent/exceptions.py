"""Exception hierarchy for API Agent.

Base class APIAgentError provides a common ancestor for all domain exceptions.
Existing exceptions (MissingHeaderError, MaxTurnsExceeded) are re-parented to
inherit from APIAgentError while retaining backward compatibility (all still
inherit from Exception).
"""


class APIAgentError(Exception):
    """Base exception for all API Agent errors.

    Provides a common ancestor so callers can catch all domain-specific
    errors with a single ``except APIAgentError`` clause.
    """

    pass


class SchemaError(APIAgentError):
    """Error fetching or parsing an API schema."""

    pass


class ProviderError(APIAgentError):
    """Error from the LLM provider (API call failure, auth, rate limit)."""

    pass


class RecipeError(APIAgentError):
    """Error validating, executing, or storing a recipe."""

    pass
