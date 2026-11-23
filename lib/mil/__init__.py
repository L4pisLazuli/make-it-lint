"""make-it-lint CLI package.

Contract version is the single string mil lint, the JSON schemas, and
docs/contract.md must agree on. Bump all three together.
"""

CONTRACT = "make-it-lint/v1"

__all__ = ["CONTRACT", "MilError"]


def __getattr__(name: str):
    if name == "MilError":
        from mil.errors import MilError

        return MilError
    raise AttributeError(name)
