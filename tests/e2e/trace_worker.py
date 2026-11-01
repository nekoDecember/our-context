"""検証用ワーカー。実モデルとの会話を専用の記録へ保存する。"""

import json
from pathlib import Path
import sys
import time

sys.path.insert(0, "/app")

from our_context.engine import Engine
from our_context.llm import LLM
from our_context.worker import main

original_completion = LLM.completion
original_process = Engine.process
current = {"job_id": None}


def process(self, job):
    current["job_id"] = job["id"]
    return original_process(self, job)


def completion(self, messages, *args, **kwargs):
    response = original_completion(self, messages, *args, **kwargs)
    record = {
        "job_id": current["job_id"],
        "time": time.time(),
        "messages": messages,
        "response": response,
    }
    with Path("/evidence/model-calls.ndjson").open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return response


Engine.process = process
LLM.completion = completion
main()
