"""Authenticated local OpenAI-compatible endpoint for the official user agent.

The upstream simulator supplies its own system prompt. Do not log it or expose it
to ClickClick's task executor. Credentials are process/environment configuration.
"""

import os
import secrets
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from shared.config import get_settings
from shared.llm_gateway import complete

MODEL = "chatgpt/gpt-5.6-sol"
app = FastAPI()


class Request(BaseModel):
    model: str
    messages: list[dict]
    stream: bool = False


@app.get("/health")
def health():
    return {"ready": True, "model": MODEL, "scope": "official_user_simulator"}


@app.post("/v1/chat/completions")
async def chat(request: Request, authorization: str = Header(default="")):
    token = os.environ.get("CLICKCLICK_USER_AGENT_GATEWAY_TOKEN", "")
    if not token or not secrets.compare_digest(authorization, "Bearer " + token):
        raise HTTPException(401, "unauthorized")
    if request.model != MODEL or request.stream:
        raise HTTPException(400, "unsupported model or streaming")
    if any(m.get("role") not in {"system", "user", "assistant"} or
           not isinstance(m.get("content"), str) for m in request.messages):
        raise HTTPException(400, "text conversation required")
    settings = get_settings()
    if settings.jev_mode != "off":
        raise HTTPException(503, "Jev must be disabled for this run")
    try:
        response = await complete(MODEL, request.messages, settings=settings,
                                  max_retries=0, max_output_tokens=2048)
    except Exception:
        # Never echo provider errors or the simulator's hidden system prompt.
        raise HTTPException(502, "user model unavailable") from None
    return {
        "id": "user-sim-" + secrets.token_hex(12), "object": "chat.completion",
        "created": int(time.time()), "model": response.model or MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": response.content},
                     "finish_reason": "stop"}],
    }
