"""Domain specialist registry: maps course-domain strings to specialist callables.

Document 2.12 Lab 13: pluggable specialists for theory, lab, and project
courses. The registry is the single lookup point so the generation node
always resolves the correct specialist through a validated contract.

Design rationale:
- Singular, append-only registry (same pattern as SkillLedger and SkillRegistry).
- Case-insensitive domain matching for caller convenience.
- Raises SkillError on unknown domains — no silent fallbacks.
- Named so tests can assert available domains and validate the mapping.
"""

from __future__ import annotations
from collections.abc import Mapping
from typing import Any

from course_organizer.skills.base import SkillError

# Import the three specialist functions; they are registered into the
# global registry at module import time via the module-level _register
# calls below.
from .specialists_lab import lab_specialist  # noqa: F401  # registers "lab"
from .specialists_project import project_specialist  # noqa: F401  # registers "project"
from .specialists_theory import theory_specialist  # noqa: F401  # registers "theory"

# ---------------------------------------------------------------------------
# Global registry instance
# ---------------------------------------------------------------------------

_global_registry: Any | None = None


class DomainSpecialistRegistry:
    """Append-only registry that maps domain names to specialist callables.

    Each specialist is a callable ``(state: Mapping[str, Any], ctx: Any) ->
    SpecialistResult`` matching the existing ``run_specialists`` convention.
    """

    def __init__(self) -> None:
        self._specialists: dict[str, Any] = {}

    # -- registration ------------------------------------------------------

    def register(self, domain: str, specialist: Any) -> None:
        """Register a specialist for the given domain.

        Args:
            domain: The course-domain string (e.g. ``"theory"``, ``"lab"``,
                ``"project"``). Case-insensitive; stored lowercased.
            specialist: A callable ``(state: Mapping[str, Any], ctx: Any)
                -> SpecialistResult`` registered after import.

        Raises:
            SkillError: If a specialist is already registered for this domain.
        """
        key = domain.lower()
        if key in self._specialists:
            raise SkillError(
                f"domain specialist {domain!r} is already registered",
                code="duplicate_skill",
            )
        self._specialists[key] = specialist

    # -- lookup -----------------------------------------------------------

    def get(self, domain: str) -> Any:
        """Look up the specialist for the given domain.

        Args:
            domain: The course-domain string. Case-insensitive.

        Returns:
            The registered specialist callable.

        Raises:
            SkillError: If no specialist is registered for this domain.
        """
        key = domain.lower()
        if key not in self._specialists:
            available = ", ".join(sorted(self._specialists))
            raise SkillError(
                f"unknown domain {domain!r}; available: {available}",
                code="unknown_domain",
            )
        return self._specialists[key]

    def names(self) -> list[str]:
        """Registered domain names, sorted."""
        return sorted(self._specialists)

    def describe_all(self) -> str:
        """Every declaration as a readable block, for the notebook and CLI."""
        lines: list[str] = [
            f"domain specialist registry",
            "-" * 34,
        ]
        for name in self.names():
            # Import and describe the specialist's summary line.
            spec = self.get(name)
            lines.append(f"  {name:12s} -> {spec.__name__}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module-level specialist registration
# ---------------------------------------------------------------------------

# Register the three specialists at module import time.
_register_module_specialists: None = None  # type: ignore


def _register_module_specialists_func(registry: Any) -> None:
    """Register the three module-imported specialists on the given registry."""
    for domain, specialist in (("theory", theory_specialist),
                               ("lab", lab_specialist),
                               ("project", project_specialist)):
        registry.register(domain, specialist)  # type: ignore[arg-type]


def get_global_registry() -> Any:  # type: ignore[override]
    """Return the single global registry instance, lazily initialised once.

    The three specialists (theory, lab, project) are registered at
    module import time via _register_module_specialists.

    Returns:
        The global ``DomainSpecialistRegistry`` instance.
    """
    global _global_registry  # type: ignore[annotation-missing]
    if _global_registry is None:
        _global_registry = DomainSpecialistRegistry()
        _register_module_specialists_func(_global_registry)  # type: ignore[arg-type]
    return _global_registry


def reset_global_registry() -> Any:  # type: ignore[returnvalue]
    """For testing: replace the global registry with a fresh one."""
    global _global_registry  # type: ignore[annotation-missing]
    _global_registry = DomainSpecialistRegistry()
    _register_module_specialists_func(_global_registry)  # type: ignore[arg-type]
    return _global_registry  # type: ignore[returnvalue]