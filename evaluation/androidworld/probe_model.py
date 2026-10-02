"""Check configured model availability outside scored task execution."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "runner"))


async def main():
    from portable import env_file
    from shared.config import Settings
    from shared.llm_gateway import complete
    model = os.environ.get("CLICKCLICK_EVAL_MODEL", "chatgpt/gpt-5.6-sol")
    settings = Settings(_env_file=env_file(ROOT.parents[1]))
    providers = settings.model_providers()
    template_id = os.environ.get("CLICKCLICK_EVAL_PROVIDER_TEMPLATE")
    if model not in providers and template_id:
        template = providers.get(template_id)
        if template is None:
            raise ValueError("Missing requested provider template")
        providers[model] = copy.deepcopy(template)
        settings.models_json = json.dumps(providers)
    try:
        await complete(model, [{"role": "user", "content": "Reply with exactly OK."}],
                       settings=settings,
                       max_retries=0, max_output_tokens=64)
        result = {"available": True, "model": model}
    except Exception as error:
        # Provider messages may contain endpoints or credentials; keep only type.
        result = {"available": False, "model": model, "error_type": type(error).__name__}
    (ROOT / "model-probe.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["available"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
