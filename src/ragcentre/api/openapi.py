"""Print the API's OpenAPI document: ``python -m ragcentre.api.openapi > openapi.json``.

The UI generates TypeScript types from it (``npm run types:api`` in ``ui/``) and type-checks them
against its hand-written contract (``ui/src/api/contract-check.ts``).
"""

from __future__ import annotations

import json
import sys
from typing import Any, cast

from ragcentre.api.app import create_app
from ragcentre.context import AppContext


def openapi() -> dict[str, Any]:
    # Building the schema needs only the routes; no data directory is opened.
    return create_app(cast(AppContext, None)).openapi()


def main() -> None:
    json.dump(openapi(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
