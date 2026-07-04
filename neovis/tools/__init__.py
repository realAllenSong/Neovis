"""Built-in tools. Importing this package registers them into the registry.

Add a company-internal tool by dropping a module here that imports
``neovis.core.registry.tool`` and decorating a function — nothing else wires up.
"""

from . import files, screen, shell, system  # noqa: F401  (import side effects register tools)

__all__ = ["files", "screen", "shell", "system"]
