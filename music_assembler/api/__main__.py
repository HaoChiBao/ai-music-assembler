"""Run the Music Assembly API with uvicorn."""

from __future__ import annotations

import os

import uvicorn
from dotenv import find_dotenv, load_dotenv


def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))
    host = os.environ.get("ASSEMBLY_API_HOST", "0.0.0.0")
    port = int(os.environ.get("ASSEMBLY_API_PORT", "8080"))
    uvicorn.run(
        "music_assembler.api.app:app",
        host=host,
        port=port,
        reload=os.environ.get("ASSEMBLY_API_RELOAD", "").lower() in ("1", "true", "yes"),
    )


if __name__ == "__main__":
    main()
