"""Process-boundary adapters for Problem Locator V1.

The package deliberately depends only on the frozen S00 ports.  S08 supplies
the concrete application, query, and administration implementations through
the composition hooks exposed here.
"""

from .composition_hooks import InterfaceDependencies, create_asgi_app

__all__ = ["InterfaceDependencies", "create_asgi_app"]
