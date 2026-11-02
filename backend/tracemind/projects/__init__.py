"""Project lookup and ingest-key authentication.

TEMPORARY SEAM (M1): projects come from a config file and keys from environment variables.
M5 replaces `ConfigProjectRegistry` with a database-backed registry and real authentication;
nothing outside this package may know where projects come from.
"""

from tracemind.projects.registry import ConfigProjectRegistry, Project, ProjectRegistry

__all__ = ["ConfigProjectRegistry", "Project", "ProjectRegistry"]
