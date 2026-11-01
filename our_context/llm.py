import copy
import json
import re
import time

import httpx

from .store import dumps
from .language import assert_plain_japanese


def extract_json(text):
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError as original:
        candidates = []
        for block in re.findall(r"```(?:json)?\s*\n([\s\S]*?)```", text):
            try:
                candidates.append(json.loads(block))
            except ValueError:
                continue
        for candidate in reversed(candidates):
            if isinstance(candidate, dict) and any(
                key in candidate for key in ("pages", "answer", "issues")
            ):
                return candidate
        if len(candidates) == 1:
            return candidates[0]
        raise original


class LLM:
    def __init__(self, settings):
        self.settings = settings
        self.usage = {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "schema_fallbacks": 0,
        }

    def completion(
        self, messages, tools=None, json_mode=False, model=None, output_schema=None
    ):
        s = self.settings
        if not s.llm_url or not (model or s.ingest_model):
            raise ValueError("設定画面で LiteLLM の URL とモデル名を確認してください。")
        payload = {
            "model": model or s.ingest_model,
            "messages": messages,
            "max_tokens": 8000,
        }
        if tools:
            payload["tools"] = tools
        if output_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "result",
                    "strict": True,
                    "schema": output_schema,
                },
            }
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}
        if sum(len(dumps(m)) for m in messages) > s.max_input:
            raise ValueError(
                "今回の入力が処理上限に達しました。原資料の範囲を分けて再実行してください。"
            )
        if self.usage["calls"] >= s.max_rounds + 2:
            raise ValueError("モデル呼び出し回数が上限に達しました。")
        headers = {"Authorization": "Bearer " + s.llm_key} if s.llm_key else {}
        for attempt in range(3):
            self.usage["calls"] += 1
            try:
                response = httpx.post(
                    s.llm_url + "/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=s.llm_timeout,
                )
                if (
                    response.status_code in (400, 422)
                    and payload.get("response_format", {}).get("type") == "json_schema"
                    and attempt < 2
                ):
                    self.usage["schema_fallbacks"] += 1
                    payload["response_format"] = {"type": "json_object"}
                    continue
                if response.status_code in (429, 502, 503, 504) and attempt < 2:
                    time.sleep((2, 5)[attempt])
                    continue
                if response.is_error:
                    detail = response.text[:700]
                    if s.llm_key:
                        detail = detail.replace(s.llm_key, "[key]")
                    raise ValueError(f"LiteLLM HTTP {response.status_code}: {detail}")
                result = response.json()
                usage = result.get("usage") or {}
                for key in ("prompt_tokens", "completion_tokens"):
                    self.usage[key] += usage.get(key) or 0
                if (
                    sum(self.usage[k] for k in ("prompt_tokens", "completion_tokens"))
                    > 150000
                ):
                    raise ValueError("トークン予算に達しました。")
                choice = result["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise ValueError(
                        "モデルの出力上限に達しました。入力の範囲を分けてください。"
                    )
                return choice["message"]
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == 2:
                    raise ValueError(
                        "LiteLLM に接続できません。URL と Docker からの到達経路を確認してください。"
                    ) from exc
                time.sleep((2, 5)[attempt])

    def run(
        self,
        system,
        user,
        tools,
        handler,
        model=None,
        response_schema=None,
        validator=None,
        repair_policy=None,
        final_tools=None,
        structured=False,
    ):
        base_system = system
        if response_schema and not structured:
            tools = [
                *tools,
                {
                    "type": "function",
                    "function": {
                        "name": "finish_result",
                        "description": "調査と整理を終え、最終結果を保存する。検査で修正点が返ったら修正して再度呼び出す。",
                        "parameters": response_schema,
                    },
                },
            ]
            system += "\n最終結果は finish_result ツールの引数へ記載する。見出し・要約・本文・質問は日本語で記載する。"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        repairing = structured
        repair_hint = "最終結果の JSON オブジェクトだけを返してください。"
        if response_schema:
            repair_hint += "必須キー：" + ", ".join(response_schema.get("required", []))
        references = []
        repair_page_ids = None
        repair_schema = response_schema

        def scoped(value):
            if repair_page_ids is not None:
                value["pages"] = [
                    p for p in value.get("pages", []) if p.get("id") in repair_page_ids
                ]
            return value

        def repair_context(value, error):
            nonlocal repair_page_ids, repair_schema
            repair_page_ids = getattr(error, "repair_page_ids", None)
            repair_schema = (
                json.loads(dumps(response_schema)) if response_schema else None
            )
            page_index = getattr(error, "page_index", [])
            if (
                repair_schema
                and repair_page_ids is not None
                and "pages" in repair_schema.get("properties", {})
            ):
                page_array = repair_schema["properties"]["pages"]
                kinds = {p["id"]: p.get("type") for p in value.get("pages", [])}
                variants = page_array["items"].get("anyOf", [])
                narrowed = []
                for page_id in repair_page_ids:
                    kind = kinds.get(page_id) or page_id.split("_", 1)[0]
                    matching = [
                        v
                        for v in variants
                        if v["properties"]["type"].get("enum") == [kind]
                    ] or variants
                    for variant in matching:
                        item = copy.deepcopy(variant)
                        item["properties"]["id"] = {"type": "string", "enum": [page_id]}
                        known = {p["id"]: p.get("type") for p in page_index}
                        known.update(
                            {
                                p: p.split("_", 1)[0]
                                for p in getattr(error, "missing_page_ids", [])
                            }
                        )
                        groups = {
                            "project_id": {"project"},
                            "project_ids": {"project"},
                            "team_ids": {"team"},
                            "member_ids": {"person"},
                            "assignee_ids": {"person", "team"},
                            "dependency_ids": {"task"},
                            "speaker_id": {"person"},
                            "recorder_id": {"person"},
                            "subject_ids": set(known.values()),
                        }
                        if page_index:
                            item["properties"]["links"]["items"] = {
                                "type": "string",
                                "enum": sorted(known),
                            }
                            for field, field_schema in item["properties"]["data"][
                                "properties"
                            ].items():
                                if field not in groups:
                                    continue
                                allowed = sorted(
                                    p
                                    for p, kind in known.items()
                                    if kind in groups[field]
                                )
                                if field in {
                                    "speaker_id",
                                    "recorder_id",
                                    "assignee_ids",
                                }:
                                    allowed.append("owner")
                                if field_schema.get("type") == "array":
                                    if allowed:
                                        field_schema["items"] = {
                                            "type": "string",
                                            "enum": allowed,
                                        }
                                    else:
                                        field_schema["maxItems"] = 0
                                else:
                                    field_schema["enum"] = [*allowed, None]
                        narrowed.append(item)
                if narrowed:
                    page_array["items"] = (
                        {"anyOf": narrowed} if len(narrowed) > 1 else narrowed[0]
                    )
                    page_array["maxItems"] = len(repair_page_ids)
                else:
                    page_array.update(minItems=0, maxItems=0)
            value = scoped({**value})
            try:
                request = json.loads(user)
            except ValueError:
                request = user
            return [
                {
                    "role": "system",
                    "content": (repair_policy or base_system)
                    + "\n修正段階です。"
                    + repair_hint
                    + "\npages_to_add の各 ID を持つページを pages 配列へ追加してください。修正対象のページは全体を返し、修正対象外のページは省略できます。",
                },
                {
                    "role": "user",
                    "content": dumps(
                        {
                            "request": request,
                            "references": references,
                            "result_to_correct": value,
                            "validation_errors": str(error).splitlines(),
                            "page_index": page_index,
                            "pages_to_add": [
                                {"id": page_id}
                                for page_id in getattr(error, "missing_page_ids", [])
                            ],
                        }
                    ),
                },
            ]

        for _ in range(self.settings.max_rounds):
            message = self.completion(
                messages,
                tools=None if repairing else tools,
                json_mode=repairing,
                model=model,
                output_schema=repair_schema if repairing else None,
            )
            calls = message.get("tool_calls") or []
            if not calls:
                raw = message.get("content") or ""
                try:
                    result = extract_json(raw)
                    if not isinstance(result, dict):
                        raise ValueError("JSON object required")
                except (ValueError, TypeError):
                    fixed = self.completion(
                        [
                            *messages,
                            {"role": "assistant", "content": raw},
                            {
                                "role": "user",
                                "content": "最終結果を、指定した構造の JSON オブジェクトだけで返してください。説明文は JSON の値へ記載します。ID・日付・evidence.quote を原資料と照合し、元の情報を保って整えてください。",
                            },
                        ],
                        json_mode=True,
                        model=model,
                        output_schema=response_schema,
                    )
                    try:
                        result = extract_json(fixed.get("content") or "")
                    except (ValueError, TypeError) as exc:
                        raise ValueError(
                            "モデルの JSON 応答を確認してください。処理履歴から再実行できます。"
                        ) from exc
                    if not isinstance(result, dict):
                        raise ValueError(
                            "モデルの最終結果を JSON オブジェクトで取得してください。"
                        )
                result = scoped(result)
                try:
                    for page in result.get("pages", []):
                        assert_plain_japanese(page.get("body", ""))
                        assert_plain_japanese(page.get("summary", ""))
                    assert_plain_japanese(result.get("answer", ""))
                    for item in result.get("questions", []):
                        for key in ("question", "reason", "impact"):
                            assert_plain_japanese(item.get(key, ""))
                except ValueError:
                    fixed = self.completion(
                        [
                            {
                                "role": "system",
                                "content": "JSON の構造・ID・evidence.quote を維持し、本文・要約・質問・回答を簡潔な肯定文へ整える。原文の引用とコードを保つ。JSON のみ返す。",
                            },
                            {"role": "user", "content": dumps(result)},
                        ],
                        json_mode=True,
                        model=model,
                        output_schema=repair_schema,
                    )
                    result = extract_json(fixed.get("content") or "{}")
                if validator:
                    try:
                        validator(result)
                    except (ValueError, KeyError, TypeError) as exc:
                        repairing = True
                        messages = repair_context(result, exc)
                        continue
                return result
            messages.append(
                {
                    k: v
                    for k, v in message.items()
                    if k in ("role", "content", "tool_calls")
                }
            )
            direct_answers = []
            for call in calls:
                name = call.get("function", {}).get("name")
                args = {}
                try:
                    args = json.loads(call["function"]["arguments"])
                    if name == "finish_result" and response_schema:
                        args = scoped(args)
                        if validator:
                            validator(args)
                        return args
                    result = handler(name, args)
                    if name in (final_tools or {}):
                        direct_answers.append(final_tools[name])
                    references.append(result)
                except (ValueError, KeyError, TypeError) as exc:
                    if name == "finish_result":
                        repairing = True
                        messages = repair_context(args, exc)
                        break
                    result = {"error": str(exc)}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": dumps(result),
                    }
                )
            if direct_answers:
                return {
                    "answer": "\n\n".join(dict.fromkeys(direct_answers)),
                    "citations": [],
                    "questions": [],
                    "insufficient": False,
                }
        raise ValueError("調査の回数が上限に達しました。質問の範囲を絞ってください。")


def function(name, description, properties):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


READ_TOOLS = [
    function(
        "search_wiki",
        "Wiki を検索する。空文字列で索引を得る。",
        {"query": {"type": "string"}},
    ),
    function(
        "read_pages",
        "ページの本文と関連情報を読む。",
        {"ids": {"type": "array", "items": {"type": "string"}}},
    ),
    function(
        "read_sources",
        "原資料を行番号付きで読む。",
        {
            "id": {"type": "string"},
            "start_line": {"type": "integer"},
            "line_count": {"type": "integer"},
        },
    ),
]
