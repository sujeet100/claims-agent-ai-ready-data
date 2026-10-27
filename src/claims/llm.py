"""Model calls made by the pipeline itself (AI for Data), with record and replay.

Every structured call is keyed by a hash of its prompt and recorded under data/recorded/.
With no API key, or with CLAIMS_REPLAY=1, the recording is used instead, so a build or a
stage demo never depends on the network.
"""

import hashlib
import json
import os

from pydantic import BaseModel
from pydantic_ai import Agent

from claims import config


def _path(task: str):
    config.RECORDED.mkdir(parents=True, exist_ok=True)
    return config.RECORDED / f"{task}.json"


def structured[T: BaseModel](task: str, instructions: str, prompt: str, output_type: type[T]) -> tuple[T | None, str]:
    """Returns (result, how) where how is 'live', 'replayed' or 'unavailable'."""
    path = _path(task)
    cache = json.loads(path.read_text()) if path.exists() else {}
    key = hashlib.sha256((instructions + "\n" + prompt).encode()).hexdigest()[:16]
    replay_only = os.getenv("CLAIMS_REPLAY") == "1" or not config.llm_available()
    if key in cache and (replay_only or os.getenv("CLAIMS_REFRESH") != "1"):
        return output_type.model_validate(cache[key]), "replayed"
    if replay_only:
        return None, "unavailable"
    agent = Agent(config.default_model(), instructions=instructions, output_type=output_type)
    result = agent.run_sync(prompt).output
    cache[key] = result.model_dump(mode="json")
    path.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    return result, "live"
