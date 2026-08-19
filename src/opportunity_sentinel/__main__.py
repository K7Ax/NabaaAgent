"""Run the service: ``python -m opportunity_sentinel``.

One entry point rather than a uvicorn command line repeated in the Dockerfile, the
README and everyone's shell history — those drift, and the one in production is the
only one anybody notices is wrong.
"""

from __future__ import annotations

import os

import uvicorn

from opportunity_sentinel.api import app

DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - containers publish on all interfaces by design
DEFAULT_PORT = 8000


def main() -> None:
    """Serve the API on the port the platform assigns, or 8000 locally."""
    uvicorn.run(
        app,
        host=os.environ.get("HOST", DEFAULT_HOST),
        port=int(os.environ.get("PORT") or DEFAULT_PORT),
    )


if __name__ == "__main__":
    main()
