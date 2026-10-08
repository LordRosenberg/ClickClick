"""Isolated test server deliberately blocking its event loop, without a phone."""

import os
from pathlib import Path
import time

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn


async def identity(request):
    return JSONResponse({"service": "clickclick", "assistant_api_version": 1,
        "workspace": str(Path.cwd()), "data_dir": os.environ["CLICKCLICK_DATA_DIR"], "pid": os.getpid()})


async def status(request):
    return JSONResponse({"ready": True})


async def block(request):
    Path(os.environ["CLICKCLICK_TEST_BLOCK_FILE"]).touch()
    time.sleep(4)
    return JSONResponse({"unblocked": True})


if __name__ == "__main__":
    app = Starlette(routes=[Route("/api/assistant/identity", identity),
                           Route("/api/assistant/status", status), Route("/block", block)])
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["CLICKCLICK_API_PORT"]), log_level="warning")
