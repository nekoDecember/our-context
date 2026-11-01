import copy
import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from .gitea import Gitea
from .llm import LLM, READ_TOOLS, function
from .language import assert_plain_japanese
from .store import dumps, uid
from .wiki import Wiki


PAGE_FORMAT = """
ページは {id,type,title,summary,body,data,evidence,links}。id は英数字・_・-で既存 ID を維持。
type は project/team/person/task/context/knowledge/analysis。
evidence は [{source_id,quote}]。quote は原文から正確に引用する。
links は関連ページの ID 配列。body は Markdown。参照は [名前](page_id.md)。
task.data は {project_id,assignee_ids,status,plan_start,plan_end,deadline,dependency_ids,ticket,completion_criteria}。
日付は YYYY-MM-DD または null。status は unknown/todo/in_progress/blocked/done/cancelled。
context.data は {kind,subject_ids,speaker_id,recorder_id,attribution,occurred_at,confirmation_state,resolution_state}。
project.data は {purpose,team_ids,member_ids}、team.data は {member_ids,project_ids}、person.data は {role,team_ids,project_ids}。
原文に明示された役割と所属を記録する。役割が未記載なら null、所属が未記載なら ID の配列を空にする。
kind は blocker/impression/decision/constraint/note。attribution は self_report/reported_statement/observation/proposal/analysis。
resolution_state は open/resolved/unknown。confirmation_state は confirmed/unconfirmed。
作業の出来事は data.events:[{date,description,source_id}] に記録する。
人物の話者・対象・記録者を区別し、明示された日付を使う。未確認の値は null。
各ページの全ての確定情報を evidence の引用で支える。タスクの予定と締め切りを分ける。
"""

DATE_FIELDS = {"plan_start": "予定開始", "plan_end": "予定終了", "deadline": "締め切り"}


def answer_date(text):
    values = set(re.findall(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", text))
    if len(values) != 1:
        raise ValueError("日付を YYYY-MM-DD 形式で1つ入力してください。")
    try:
        return date.fromisoformat(values.pop()).isoformat()
    except ValueError as exc:
        raise ValueError(
            "カレンダーにある日付を YYYY-MM-DD 形式で入力してください。"
        ) from exc


def object_schema(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TEXT = {"type": "string"}
NULLABLE_TEXT = {"type": ["string", "null"]}
IDS = {"type": "array", "items": TEXT}
EVIDENCE_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "items": object_schema({"source_id": TEXT, "quote": TEXT}),
}
TASK_FIELDS = {
    "project_id": NULLABLE_TEXT,
    "assignee_ids": IDS,
    "status": {
        "type": "string",
        "enum": ["unknown", "todo", "in_progress", "blocked", "done", "cancelled"],
    },
    **{
        key: NULLABLE_TEXT
        for key in (
            "plan_start",
            "plan_end",
            "deadline",
            "ticket",
            "completion_criteria",
        )
    },
    "dependency_ids": IDS,
    "events": {
        "type": "array",
        "items": object_schema({"date": TEXT, "description": TEXT, "source_id": TEXT}),
    },
}
CONTEXT_FIELDS = {
    "kind": {
        "type": "string",
        "enum": ["blocker", "impression", "decision", "constraint", "note"],
    },
    "subject_ids": IDS,
    "speaker_id": NULLABLE_TEXT,
    "recorder_id": NULLABLE_TEXT,
    "attribution": {
        "type": "string",
        "enum": [
            "self_report",
            "reported_statement",
            "observation",
            "proposal",
            "analysis",
        ],
    },
    "occurred_at": NULLABLE_TEXT,
    "confirmation_state": {"type": "string", "enum": ["confirmed", "unconfirmed"]},
    "resolution_state": {"type": "string", "enum": ["open", "resolved", "unknown"]},
}
PAGE_DATA = {
    "project": {"purpose": NULLABLE_TEXT, "team_ids": IDS, "member_ids": IDS},
    "team": {"member_ids": IDS, "project_ids": IDS},
    "person": {"role": NULLABLE_TEXT, "team_ids": IDS, "project_ids": IDS},
    "task": TASK_FIELDS,
    "context": CONTEXT_FIELDS,
    "knowledge": {},
    "analysis": {},
}
PAGE_SCHEMAS = [
    object_schema(
        {
            "id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,120}$"},
            "type": {"type": "string", "enum": [kind]},
            "title": TEXT,
            "summary": TEXT,
            "body": TEXT,
            "data": object_schema(fields),
            "evidence": EVIDENCE_SCHEMA,
            "links": IDS,
        }
    )
    for kind, fields in PAGE_DATA.items()
]
WIKI_RESULT_SCHEMA = object_schema(
    {
        "pages": {"type": "array", "minItems": 1, "items": {"anyOf": PAGE_SCHEMAS}},
        "questions": {
            "type": "array",
            "items": object_schema(
                {
                    **{
                        key: TEXT
                        for key in (
                            "semantic_key",
                            "subject_id",
                            "field",
                            "question",
                            "reason",
                            "impact",
                            "priority",
                        )
                    },
                    "evidence": EVIDENCE_SCHEMA,
                }
            ),
        },
        "resolved_questions": IDS,
    }
)


class Engine:
    def __init__(self, settings, store, llm_factory=LLM):
        self.s, self.store = settings, store
        self.wiki = Wiki(store)
        self.llm_factory = llm_factory

    def policy(self):
        path = Path(__file__).resolve().parent.parent / "wiki-policy.md"
        return (
            path.read_text()
            if path.exists()
            else "業務の文脈を出典付きで整理し、日本語の肯定文で簡潔に答える。"
        )

    def questions(self):
        with self.store.db() as c:
            return [
                {**dict(row), "data": json.loads(row["data"])}
                for row in c.execute("SELECT * FROM questions ORDER BY updated DESC")
            ]

    def capture_questions(self, items, source_ids):
        for item in items[:30]:
            if not item.get("question") or not item.get("reason"):
                continue
            for field in ("question", "reason", "impact"):
                assert_plain_japanese(item.get(field, ""))
            evidence = item.get("evidence", [])
            if not evidence:
                continue
            self.wiki.validate(
                {
                    "question_check": {
                        "id": "question_check",
                        "type": "knowledge",
                        "title": item["question"],
                        "body": "",
                        "evidence": evidence,
                        "links": [],
                    }
                },
                set(source_ids),
            )
            semantic = (
                (str(item["subject_id"]) + ":" + str(item["field"]))
                if item.get("subject_id") and item.get("field")
                else item.get("semantic_key")
                or (
                    str(item.get("subject_id", ""))
                    + ":"
                    + str(item.get("field", ""))
                    + ":"
                    + item["question"]
                )
            )
            fingerprint = hashlib.sha256(
                dumps([(e["source_id"], e["quote"]) for e in evidence]).encode()
            ).hexdigest()
            item = {**item, "fingerprint": fingerprint}
            with self.store.db() as c:
                old = c.execute(
                    "SELECT * FROM questions WHERE semantic_key=?", (semantic,)
                ).fetchone()
                if old:
                    old_data = json.loads(old["data"])
                    if old["status"] in ("answering", "answer_failed", "deferred"):
                        continue
                    if old["status"] in ("answered", "dismissed"):
                        # 同じ引用内容は原資料の版が変わっても回答済みとして扱う。
                        old_quotes = [
                            e.get("quote") for e in old_data.get("evidence", [])
                        ]
                        if old_quotes == [
                            e.get("quote") for e in evidence
                        ] or not item.get("reopen_reason"):
                            continue
                    c.execute(
                        "UPDATE questions SET data=?,status='open',version=version+1,updated=? WHERE id=?",
                        (dumps(item), time.time(), old["id"]),
                    )
                else:
                    c.execute(
                        "INSERT INTO questions(id,semantic_key,data,updated) VALUES(?,?,?,?)",
                        (uid("q"), semantic, dumps(item), time.time()),
                    )

    def reader(self, snapshot, extra_sources=None, scope=""):
        allowed = set(snapshot["manifest"]["source_ids"]) | set(extra_sources or [])
        read_evidence = {}

        def handle(name, args):
            if name == "search_wiki":
                found = self.wiki.search(snapshot, args.get("query", ""), scope)
                return [
                    {
                        "id": p["id"],
                        "title": p["title"],
                        "type": p.get("type"),
                        "summary": p.get("summary", ""),
                    }
                    for p in found[:60]
                ]
            if name == "read_pages":
                result = []
                for page_id in args.get("ids", [])[:8]:
                    page = snapshot["pages"].get(page_id)
                    if not page:
                        continue
                    result.append(page)
                    for evidence in page.get("evidence", []):
                        read_evidence.setdefault(evidence["source_id"], []).append(
                            evidence["quote"]
                        )
                return result
            if name == "read_sources":
                source_id = args["id"]
                if source_id not in allowed:
                    raise ValueError("参照版に含まれる原資料を指定してください。")
                source = self.store.read_source(source_id)
                start = max(0, int(args.get("start_line", 1)) - 1)
                count = max(1, min(int(args.get("line_count", 100)), 200))
                lines = source["body"].splitlines()[start : start + count]
                text = "\n".join(lines)
                read_evidence.setdefault(source_id, []).append(text)
                return {
                    "id": source_id,
                    "title": source["title"],
                    "metadata": source["metadata"],
                    "lines": [
                        {"line": start + i + 1, "text": line}
                        for i, line in enumerate(lines)
                    ],
                }
            raise ValueError("ツール名を確認してください。")

        return handle, read_evidence

    def deadline_questions(self, pages, questions):
        result = list(questions)
        for page in pages.values():
            data = page.get("data") or {}
            if (
                page.get("type") != "task"
                or not data.get("plan_end")
                or data.get("deadline")
                or data.get("status") in ("done", "cancelled")
            ):
                continue
            if any(
                q.get("subject_id") == page["id"]
                and (q.get("field") == "deadline" or "締" in q.get("question", ""))
                for q in result
            ):
                continue
            result.append(
                {
                    "semantic_key": page["id"] + ":deadline",
                    "subject_id": page["id"],
                    "field": "deadline",
                    "question": f"「{page['title']}」の締め切りはいつですか？",
                    "reason": "作業予定があり、締め切りの確認が必要です。",
                    "impact": "締め切りを登録すると、予定との差と遅延への影響を確認できます。",
                    "priority": "normal",
                    "evidence": page["evidence"],
                }
            )
        return result

    def confirm_date(self, snapshot, sources, job_id, question_id):
        if not question_id:
            return None
        with self.store.db() as c:
            row = c.execute(
                "SELECT * FROM questions WHERE id=?", (question_id,)
            ).fetchone()
        if not row:
            return None
        question = json.loads(row["data"])
        field = question.get("field")
        page = snapshot["pages"].get(question.get("subject_id"))
        if field not in DATE_FIELDS or not page or page.get("type") != "task":
            return None
        source = next(
            (s for s in sources if s["metadata"].get("question_id") == question_id),
            None,
        )
        if not source:
            return None
        if row["answer_source"] and row["answer_source"] != source["id"]:
            raise ValueError("回答が更新されました。最新の回答を処理してください。")
        text = source["body"].split("\n回答：", 1)[-1]
        value = answer_date(text)
        page["data"][field] = value
        item = {"source_id": source["id"], "quote": source["body"]}
        if item not in page["evidence"]:
            page["evidence"].append(item)
        sentence = f"{DATE_FIELDS[field]}：{value}。"
        if sentence not in page["body"]:
            page["body"] += "\n\n## 確認への回答\n\n" + sentence + "\n"
        page["updated_at"] = datetime.now(timezone.utc).isoformat()
        manifest = snapshot["manifest"]
        if source["id"] not in manifest["source_ids"]:
            manifest["source_ids"].append(source["id"])
        manifest["source_current"][source["logical_key"]] = source["id"]
        self.store.update_job(job_id, stage="確認への回答を予定に反映しています")
        revision = self.wiki.publish(snapshot, job_id, resolved_question=question_id)
        return {
            "revision": revision,
            "pages": len(snapshot["pages"]),
            "sources": [source["id"]],
            "confirmed_field": field,
            "value": value,
            "usage": {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "schema_fallbacks": 0,
            },
        }

    def preserve_dates(self, page, previous, added_text):
        if page.get("type") != "task" or not previous:
            return
        labels = {
            "deadline": r"締め切り|締切|期限|deadline",
            "plan_start": r"予定開始|開始日|plan_start",
            "plan_end": r"予定終了|終了日|plan_end",
        }
        for field, label in DATE_FIELDS.items():
            old = (previous.get("data") or {}).get(field)
            clear = any(
                re.search(labels[field], line, re.I)
                and re.search(r"未定|取り消|取消|未設定|null", line, re.I)
                for line in added_text.splitlines()
            )
            if old and not page.get("data", {}).get(field) and not clear:
                page.setdefault("data", {})[field] = old
                page.setdefault("evidence", [])
                for item in previous.get("evidence", []):
                    if item not in page["evidence"]:
                        page["evidence"].append(item)
                if old not in page["body"]:
                    page["body"] += f"\n\n## 登録済みの予定\n\n{label}：{old}。\n"

    def integrate_delta(self, snapshot, sources, source_input, llm, handler):
        if not snapshot["pages"] or not all(item["is_diff"] for item in source_input):
            return None
        working = copy.deepcopy(snapshot["pages"])
        changed, questions, resolved = {}, [], []
        for source, incoming in zip(sources, source_input):
            added = "\n".join(
                line[1:]
                for line in incoming["changes"].splitlines()
                if line.startswith("+") and not line.startswith("+++")
            )
            quotes = list(
                dict.fromkeys(
                    line.strip()[:2000]
                    for line in added.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                )
            )
            if not quotes:
                continue
            headings = re.findall(r"(?m)^#{1,6}\s*(\d{4}-\d{2}-\d{2})", source["body"])
            recorded = (
                headings[-1]
                if headings
                else datetime.fromtimestamp(source["created"], timezone.utc)
                .date()
                .isoformat()
            )
            variants = []
            for page in working.values():
                ref_types = {
                    "project_id": {"project"},
                    "project_ids": {"project"},
                    "team_ids": {"team"},
                    "member_ids": {"person"},
                    "assignee_ids": {"person", "team"},
                    "dependency_ids": {"task"},
                    "speaker_id": {"person"},
                    "recorder_id": {"person"},
                    "subject_ids": set(PAGE_DATA),
                }
                fields = {}
                for key, descriptor in PAGE_DATA.get(page.get("type"), {}).items():
                    if key == "events":
                        continue
                    value = copy.deepcopy(descriptor)
                    if key in ref_types:
                        ids = [
                            p["id"]
                            for p in working.values()
                            if p.get("type") in ref_types[key]
                        ]
                        if key in {"speaker_id", "recorder_id", "assignee_ids"}:
                            ids.append("owner")
                        if value.get("type") == "array":
                            if ids:
                                value["items"] = {"type": "string", "enum": ids}
                            else:
                                value["maxItems"] = 0
                        else:
                            value["enum"] = [*ids, None]
                    fields[key] = {"anyOf": [value, {"type": "null"}]}
                clear = (
                    {"type": "array", "items": {"type": "string", "enum": list(fields)}}
                    if fields
                    else {"type": "array", "items": TEXT, "maxItems": 0}
                )
                variants.append(
                    object_schema(
                        {
                            "id": {"type": "string", "enum": [page["id"]]},
                            "data": object_schema(fields),
                            "append": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 1200,
                            },
                            "quote": {"type": "string", "enum": quotes},
                            "clear_fields": clear,
                        }
                    )
                )
            evidence = {
                "type": "array",
                "minItems": 1,
                "items": object_schema(
                    {
                        "source_id": {"type": "string", "enum": [source["id"]]},
                        "quote": {"type": "string", "enum": quotes},
                    }
                ),
            }
            question_schema = copy.deepcopy(
                WIKI_RESULT_SCHEMA["properties"]["questions"]
            )
            question_schema["items"]["properties"]["evidence"] = evidence
            schema = object_schema(
                {
                    "updates": {
                        "type": "array",
                        "maxItems": len(working),
                        "items": {"anyOf": variants},
                    },
                    "needs_new_pages": {"type": "boolean"},
                    "questions": question_schema,
                    "resolved_questions": IDS,
                }
            )
            index = [
                {
                    "id": p["id"],
                    "type": p.get("type"),
                    "title": p["title"],
                    "relations": {
                        k: v
                        for k, v in p.get("data", {}).items()
                        if k.endswith("_id") or k.endswith("_ids")
                    },
                    "known_dates": {
                        k: p.get("data", {}).get(k)
                        for k in DATE_FIELDS
                        if p.get("data", {}).get(k)
                    },
                }
                for p in working.values()
            ]
            result = llm.run(
                self.policy()
                + """\n既存ノートの差分を統合する。sources.changes の追記された事実だけから、情報が変わる既存ページの updates を返す。
同じ作業や困りごとの進捗は既存 ID を使う。新しい人物・作業・案件・別の話題のページが必要な場合は needs_new_pages=true とする。
未記載の項目は data の値を null にする。日付見出しは記録日である。予定日や締め切りは明示された変更だけを反映する。known_dates を維持する。
進行中・着手済みの作業は status=in_progress、解決した困りごとは resolution_state=resolved とする。過去の状態をコピーしない。
append には最新の事実を日本語の短い肯定文で記載する。quote は追記の原文から選ぶ。明示的な取り消しだけ clear_fields に入れる。JSON のみ返す。
""",
                dumps(
                    {
                        "sources": [
                            {**incoming, "changes": added, "recorded_date": recorded}
                        ],
                        "index": index,
                        "questions": [
                            q
                            for q in self.questions()
                            if q["status"] in ("open", "deferred")
                        ],
                    }
                ),
                [],
                handler,
                response_schema=schema,
                structured=True,
            )
            if "updates" not in result or result.get("needs_new_pages"):
                return None
            questions.extend(result.get("questions", []))
            resolved.extend(result.get("resolved_questions", []))
            for update in result["updates"]:
                if update.get("id") not in working or update.get("quote") not in quotes:
                    return None
                page = working[update["id"]]
                assert_plain_japanese(update["append"])
                for field, value in update["data"].items():
                    if value not in (None, "", []):
                        page["data"][field] = value
                for field in update["clear_fields"]:
                    page["data"][field] = (
                        [] if isinstance(page["data"].get(field), list) else None
                    )
                page["body"] += f"\n\n## {recorded} の追記\n\n{update['append']}\n"
                page["summary"] = update["append"][:300]
                item = {"source_id": source["id"], "quote": update["quote"]}
                if item not in page["evidence"]:
                    page["evidence"].append(item)
                if page["type"] == "task":
                    page["data"].setdefault("events", []).append(
                        {
                            "date": recorded,
                            "description": update["append"],
                            "source_id": source["id"],
                        }
                    )
                if page["type"] == "context":
                    page["data"]["occurred_at"] = recorded
                changed[page["id"]] = page
        return {
            "pages": list(changed.values()),
            "questions": questions,
            "resolved_questions": resolved,
        }

    def ingest(self, source_ids, job_id, checkpoint=None, question_id=None):
        snapshot = self.wiki.snapshot()
        manifest = snapshot["manifest"]
        new_sources = [self.store.read_source(sid) for sid in source_ids]
        confirmed = self.confirm_date(snapshot, new_sources, job_id, question_id)
        if confirmed:
            return confirmed
        handler, _ = self.reader(snapshot, source_ids)
        summaries = [
            {"id": p["id"], "title": p["title"], "summary": p.get("summary", "")}
            for p in snapshot["pages"].values()
        ]
        source_input = []
        import difflib

        for source in new_sources:
            old_id = manifest["source_current"].get(source["logical_key"])
            body = source["body"]
            if old_id:
                old = self.store.read_source(old_id)["body"]
                changes = "\n".join(
                    difflib.unified_diff(old.splitlines(), body.splitlines(), n=8)
                )
            else:
                changes = body
            source_input.append(
                {
                    "id": source["id"],
                    "title": source["title"],
                    "metadata": source["metadata"],
                    "changes": changes[:24000],
                    "is_diff": bool(old_id),
                    "line_count": len(body.splitlines()),
                    "read_hint": "全文は read_sources で参照する",
                }
            )
        llm = self.llm_factory(self.s)
        added_text = "\n".join(
            "\n".join(
                line[1:]
                for line in item["changes"].splitlines()
                if line.startswith("+") and not line.startswith("+++")
            )
            if item["is_diff"]
            else item["changes"]
            for item in source_input
        )
        if new_sources:
            prompt = (
                self.policy()
                + PAGE_FORMAT
                + """
新しい原資料を Wiki に統合する。まず関連する既存ページを読んでから更新する。
changes が unified diff の場合は + 行を今回の追記として読み、文脈行の過去の記録と区別する。
原資料に登場する実体ごとにページを整理する。案件は project、チームは team、人物は person、個々の作業は task、発言・困りごと・所感・判断は context の独立したページへまとめる。
手順や一般的な業務ルールは knowledge にまとめる。各ページの body に実体の目的・役割・経緯を残し、links で関係する実体をつなぐ。
ページ ID は project_search、team_platform、person_tanaka、task_search_test、context_database_access のように実体を表す英数字を付ける。原資料 ID は evidence.source_id に記載する。
チームの data には member_ids と project_ids を持たせる。担当する人物・案件の ID を記載し、関連ページも links に含める。
作業の担当・状態・予定・締め切りは task.data に記載する。明示された作業をタスクへ整理し、予定の開始と終了を YYYY-MM-DD で記録する。
停止中・権限不足で着手できない作業は status=blocked、作業開始・実施中は in_progress。明示された状態を選ぶ。完了条件などの未記載の値は null とする。
業務に必要な締め切り・担当・前提が未定なら、対象の task ID と field を付けて確認事項を作成する。理由と業務への影響、原文の引用を含める。
人物の発言は context.data.attribution=reported_statement とし、speaker_id に話者の person ID、recorder_id に記録者の ID、occurred_at にノートの日付を残す。
確認事項への回答と利用者が適用した変更は確定情報として保持する。今回の変更箇所と記録日を照合し、明示された訂正を反映する。
困りごとの解決が明記されたら、対応する context の resolution_state を resolved に更新し、本文と要約にも解決の経緯を残す。
既存情報、過去の判断、リンクを保ち、訂正と新情報を反映する。必要なページを複数作成・更新する。
ページ内容は各ページの現在の全体を返す。既存 ID を再利用する。日付見出しと記録の時点を使う。
検査で修正点が返った場合、取り込み処理は前回のページ案を保持する。修正・追加するページの全体だけを再送する。
記録の削除・撤回は、支持情報の変化として整理する。タスクの完了・解決は明示された記録で判断する。
最終出力は JSON: {"pages":[ページ],"questions":[{semantic_key,subject_id,field,question,reason,impact,priority,evidence}],"resolved_questions":[]}。
questions は業務に影響する不足を優先する。全ての evidence は原資料の正確な引用。
保存前に次を照合する。原資料に案件・チーム・人物・作業・発言が登場する場合は、それぞれ project・team・person・task・context ページを作成する。全ての参照 ID のページを含める。チームの担当案件とメンバー、作業の担当・現在の状態・予定、発言者・記録者を確認する。
"""
            )
            self.store.update_job(job_id, stage="Wiki を整理しています")
            candidates = {}

            def validate_result(value):
                if not value.get("pages") and not candidates:
                    raise ValueError(
                        "原資料の実体を整理した pages を作成してください。"
                    )
                for page in value.get("pages", []):
                    self.preserve_dates(
                        page, snapshot["pages"].get(page["id"]), added_text
                    )
                candidates.update({p["id"]: p for p in value["pages"]})
                proposed = {**snapshot["pages"], **candidates}
                missing_fields = {
                    p["id"]: sorted(
                        set(PAGE_DATA.get(p.get("type"), {})) - set(p.get("data", {}))
                    )
                    for p in candidates.values()
                }
                missing_fields = {
                    key: fields for key, fields in missing_fields.items() if fields
                }
                if missing_fields:
                    error = ValueError(
                        "各ページの data の必須項目を記載してください。未確認は null または空の配列を使います。"
                        + dumps(missing_fields)
                    )
                    error.repair_page_ids = list(missing_fields)
                    error.page_index = [
                        {"id": p["id"], "type": p.get("type"), "title": p["title"]}
                        for p in proposed.values()
                    ]
                    raise error
                allowed = set(manifest["source_ids"]) | set(source_ids)
                self.wiki.validate(proposed, allowed)
                for item in value.get("questions", []):
                    try:
                        for field in ("question", "reason", "impact"):
                            assert_plain_japanese(item.get(field, ""))
                        self.wiki.validate(
                            {
                                "question_check": {
                                    "id": "question_check",
                                    "title": item["question"],
                                    "body": "",
                                    "evidence": item.get("evidence", []),
                                }
                            },
                            allowed,
                        )
                    except ValueError as exc:
                        error = ValueError("確認事項を修正してください。" + str(exc))
                        error.repair_page_ids = []
                        raise error from exc
                value["pages"] = list(candidates.values())

            result = self.integrate_delta(
                snapshot, new_sources, source_input, llm, handler
            )
            incremental = result is not None
            if result is None:
                cold = not snapshot["pages"] and all(
                    len(source["body"]) <= 24000 for source in new_sources
                )
                if cold:
                    prompt += "\n初回の整理である。原資料の全文は sources.changes にある。全文から全ての実体を整理し、指定された JSON を直接返す。作業の project_id と assignee_ids、予定、発言の subject_ids・speaker_id・recorder_id を関連ページの ID で記録する。確認事項は questions に記載する。"
                output_schema = json.loads(dumps(WIKI_RESULT_SCHEMA))
                if cold:
                    quotes = []
                    if len(new_sources) == 1:
                        quotes = list(
                            dict.fromkeys(
                                line.strip()
                                for line in new_sources[0]["body"].splitlines()
                                if line.strip()
                            )
                        )
                        if len(quotes) > 80 or any(
                            len(quote) > 2000 for quote in quotes
                        ):
                            quotes = []
                    for variant in output_schema["properties"]["pages"]["items"][
                        "anyOf"
                    ]:
                        variant["properties"]["evidence"]["items"]["properties"][
                            "source_id"
                        ]["enum"] = source_ids
                        if quotes:
                            variant["properties"]["evidence"]["items"]["properties"][
                                "quote"
                            ]["enum"] = quotes
                    output_schema["properties"]["questions"]["items"]["properties"][
                        "evidence"
                    ]["items"]["properties"]["source_id"]["enum"] = source_ids
                    if quotes:
                        output_schema["properties"]["questions"]["items"]["properties"][
                            "evidence"
                        ]["items"]["properties"]["quote"]["enum"] = quotes
                result = llm.run(
                    prompt,
                    dumps(
                        {
                            "index": summaries,
                            "sources": source_input,
                            "questions": self.questions()[:40],
                        }
                    ),
                    READ_TOOLS,
                    handler,
                    response_schema=output_schema,
                    validator=validate_result,
                    repair_policy=self.policy() + PAGE_FORMAT,
                    structured=cold,
                )
            for page in result.get("pages", []):
                self.preserve_dates(page, snapshot["pages"].get(page["id"]), added_text)
                page.setdefault("data", {})
                page.setdefault("links", [])
                page["updated_at"] = datetime.now(timezone.utc).isoformat()
                snapshot["pages"][page["id"]] = page
            for source in new_sources:
                if source["id"] not in manifest["source_ids"]:
                    manifest["source_ids"].append(source["id"])
                manifest["source_current"][source["logical_key"]] = source["id"]
            if not result.get("pages") and not incremental:
                raise ValueError(
                    "更新する Wiki ページが空です。原資料を確認して再実行してください。"
                )
        else:
            result = {"questions": []}
        if checkpoint is not None:
            manifest["gitea"] = checkpoint
        self.store.update_job(job_id, stage="出典を確認しています")
        self.wiki.validate(snapshot["pages"], set(manifest["source_ids"]))
        result["questions"] = self.deadline_questions(
            snapshot["pages"], result.get("questions", [])
        )
        # 確認事項も公開前に検査する。
        for item in result.get("questions", []):
            for field in ("question", "reason", "impact"):
                assert_plain_japanese(item.get(field, ""))
            if item.get("evidence"):
                self.wiki.validate(
                    {
                        "question_check": {
                            "id": "question_check",
                            "title": item.get("question", "確認事項"),
                            "body": "",
                            "evidence": item["evidence"],
                        }
                    },
                    set(manifest["source_ids"]),
                )
        revision = self.wiki.publish(snapshot, job_id, resolved_question=question_id)
        self.capture_questions(result.get("questions", []), manifest["source_ids"])
        with self.store.db() as c:
            for qid in result.get("resolved_questions", []):
                c.execute(
                    "UPDATE questions SET status='obsolete',updated=? WHERE id=? AND status IN ('open','deferred')",
                    (time.time(), qid),
                )
        return {
            "revision": revision,
            "pages": len(snapshot["pages"]),
            "sources": source_ids,
            "usage": llm.usage,
        }

    def sync(self, job):
        self.store.update_job(job["id"], stage="Gitea から取得しています")
        snapshot = self.wiki.snapshot()
        old = snapshot["manifest"].get("gitea", {})
        gitea = Gitea(self.s)
        commit, files = gitea.fetch(
            job["payload"].get("commit"),
            old.get("commit"),
            job["payload"].get("force", False),
        )
        if files is None:
            return {"revision": snapshot["revision"], "message": "反映済みです。"}
        old_files = old.get("files", {})
        source_ids = []
        records = {}
        removed = set(old_files) - set(files)
        for path, blob in files.items():
            previous = old_files.get(path)
            if previous and previous["blob"] == blob:
                records[path] = previous
                continue
            matches = [p for p in removed if old_files[p]["blob"] == blob]
            if len(matches) == 1:
                prior = old_files[matches[0]]
                records[path] = {**prior, "blob": blob}
                removed.remove(matches[0])
                continue
            logical = (
                previous["logical"]
                if previous
                else "gitea:" + self.s.gitea_repo + ":" + path
            )
            source = self.store.source(
                path,
                gitea.read(commit, path),
                "gitea",
                logical,
                {"commit": commit, "path": path, "repo": self.s.gitea_repo},
            )
            source_ids.append(source["id"])
            records[path] = {
                "blob": blob,
                "logical": logical,
                "source_id": source["id"],
            }
        for path in removed:
            previous = old_files[path]
            body = f"入力元のファイル {path} が commit {commit} で削除された。以前の原資料 ID は {previous['source_id']}。内容の明示的な撤回状況は未確認。"
            source = self.store.source(
                "入力元の変更：" + path,
                body,
                "withdrawal",
                "withdrawal:" + path + ":" + commit,
            )
            source_ids.append(source["id"])
        if not source_ids and old.get("commit") == commit:
            return {"revision": snapshot["revision"], "message": "最新の状態です。"}
        return self.ingest(source_ids, job["id"], {"commit": commit, "files": records})

    def task_list(self, snapshot, filters=None):
        filters = filters or {}
        tasks = [
            {
                "id": p["id"],
                "title": p["title"],
                "evidence": p.get("evidence", []),
                **p.get("data", {}),
            }
            for p in snapshot["pages"].values()
            if p.get("type") == "task"
        ]
        for key in ("project_id", "status"):
            if filters.get(key):
                tasks = [t for t in tasks if t.get(key) == filters[key]]
        if filters.get("assignee"):
            tasks = [
                t for t in tasks if filters["assignee"] in t.get("assignee_ids", [])
            ]
        if filters.get("scope"):
            tasks = [
                t for t in tasks if self.wiki.in_scope(snapshot, t, filters["scope"])
            ]
        if filters.get("start"):
            tasks = [
                t
                for t in tasks
                if not t.get("plan_end") or t["plan_end"] >= filters["start"]
            ]
        if filters.get("end"):
            tasks = [
                t
                for t in tasks
                if not t.get("plan_start") or t["plan_start"] <= filters["end"]
            ]
        return tasks

    def simulate(self, snapshot, operations):
        tasks = {t["id"]: dict(t) for t in self.task_list(snapshot)}
        changed = {}
        for op in operations:
            if op["task_id"] not in tasks:
                raise ValueError("対象タスクを確認してください。")
            changed[op["task_id"]] = op.get("set", {})
            tasks[op["task_id"]].update(op.get("set", {}))
        impacts, warnings = [], []
        visiting, visited = set(), set()

        def visit(task_id):
            if task_id in visited:
                return
            if task_id in visiting:
                raise ValueError("依存関係が循環しています。関係を確認してください。")
            visiting.add(task_id)
            task = tasks[task_id]
            for dep_id in task.get("dependency_ids", []):
                if dep_id not in tasks:
                    warnings.append("依存先が未確認：" + dep_id)
                    continue
                visit(dep_id)
                end = tasks[dep_id].get("plan_end")
                if end and task.get("plan_start") and task["plan_start"] <= end:
                    start = date.fromisoformat(end) + timedelta(days=1)
                    if not task.get("plan_end"):
                        warnings.append("作業期間が未確認：" + task["title"])
                        continue
                    duration = date.fromisoformat(
                        task["plan_end"]
                    ) - date.fromisoformat(task["plan_start"])
                    task.update(
                        plan_start=start.isoformat(),
                        plan_end=(start + duration).isoformat(),
                    )
                    changed[task_id] = {
                        **changed.get(task_id, {}),
                        "plan_start": task["plan_start"],
                        "plan_end": task["plan_end"],
                    }
            if (
                task.get("deadline")
                and task.get("plan_end")
                and task["plan_end"] > task["deadline"]
            ):
                warnings.append("締め切り超過：" + task["title"])
            if task_id in changed:
                impacts.append(
                    {
                        "task_id": task_id,
                        "title": task["title"],
                        "set": changed[task_id],
                    }
                )
            visiting.remove(task_id)
            visited.add(task_id)

        for tid in tasks:
            visit(tid)
        return {
            "revision": snapshot["revision"],
            "operations": [{"task_id": i["task_id"], "set": i["set"]} for i in impacts],
            "impacts": impacts,
            "warnings": warnings,
            "assumptions": ["日数は暦日で計算する。"],
        }

    def propose(self, revision, operations, reason="", assumptions=None):
        snapshot = self.wiki.snapshot(revision)
        allowed = {"plan_start", "plan_end", "deadline", "status", "assignee_ids"}
        for op in operations:
            if not op.get("set") or not set(op["set"]) <= allowed:
                raise ValueError("変更する項目を確認してください。")
        simulated = self.simulate(snapshot, operations)
        proposal = {
            **simulated,
            "reason": reason,
            "assumptions": assumptions or simulated["assumptions"],
        }
        # 候補に反映して型・日付を検査する。
        for op in proposal["operations"]:
            snapshot["pages"][op["task_id"]]["data"].update(op["set"])
        self.wiki.validate(snapshot["pages"], set(snapshot["manifest"]["source_ids"]))
        proposal_id = uid("proposal")
        with self.store.db() as c:
            c.execute(
                "INSERT INTO proposals(id,revision,data,created) VALUES(?,?,?,?)",
                (proposal_id, revision, dumps(proposal), time.time()),
            )
        return {"id": proposal_id, **proposal}

    def apply(self, job):
        proposal_id = job["payload"]["proposal_id"]
        with self.store.db() as c:
            row = c.execute(
                "SELECT * FROM proposals WHERE id=?", (proposal_id,)
            ).fetchone()
        if row["status"] == "applied":
            return {"revision": self.store.get("revision")}
        snapshot = self.wiki.snapshot()
        if snapshot["revision"] != row["revision"]:
            raise ValueError(
                "参照版が更新されました。最新の内容で変更案を作成してください。"
            )
        proposal = json.loads(row["data"])
        body = "利用者が次の変更を確定した。\n" + dumps(proposal)
        source = self.store.source(
            "予定・状態の変更", body, "change", "change:" + proposal_id
        )
        snapshot["manifest"]["source_ids"].append(source["id"])
        snapshot["manifest"]["source_current"][source["logical_key"]] = source["id"]
        for op in proposal["operations"]:
            page = snapshot["pages"][op["task_id"]]
            page["updated_at"] = datetime.now(timezone.utc).isoformat()
            page["data"].update(op["set"])
            page["evidence"].append({"source_id": source["id"], "quote": body})
            field_labels = {
                "plan_start": "予定開始",
                "plan_end": "予定終了",
                "deadline": "締め切り",
                "status": "状態",
                "assignee_ids": "担当",
            }
            status_labels = {
                "unknown": "未確認",
                "todo": "予定",
                "in_progress": "進行中",
                "blocked": "困りごとあり",
                "done": "完了",
                "cancelled": "中止",
            }
            changes = []
            for key, value in op["set"].items():
                if key == "status":
                    value = status_labels.get(value, value)
                elif key == "assignee_ids":
                    value = "、".join(
                        snapshot["pages"].get(person_id, {}).get("title", person_id)
                        for person_id in value
                    )
                changes.append(f"{field_labels[key]}：{value or '未定'}")
            page["body"] += "\n\n## 予定・状態の更新\n\n" + "、".join(changes) + "。\n"
        revision = self.wiki.publish(snapshot, job["id"], proposal_id=proposal_id)
        return {"revision": revision}

    def query(self, job):
        payload = job["payload"]
        snapshot = self.wiki.snapshot(payload.get("revision"))
        handler, readable = self.reader(snapshot, scope=payload.get("scope", ""))
        tool_results = []
        project_index = [
            {"id": p["id"], "title": p["title"]}
            for p in snapshot["pages"].values()
            if p.get("type") == "project"
        ]
        project_parameter = {
            "type": "string",
            "enum": ["", *[p["id"] for p in project_index]],
            "description": "projects にある案件 ID。全体は空文字。",
        }
        tools = READ_TOOLS + [
            function(
                "list_tasks",
                "対象範囲のタスク一覧を読む。",
                {"project_id": project_parameter},
            ),
            function(
                "list_context_records",
                "人物や案件の困りごと・所感を読む。",
                {"subject_id": {"type": "string"}},
            ),
            function(
                "show_gantt",
                "ガント表示の絞り込みを返す。",
                {"project_id": project_parameter},
            ),
            function(
                "simulate_schedule",
                "予定の変更を試算する。",
                {"operations": {"type": "array", "items": {"type": "object"}}},
            ),
            function(
                "propose_task_changes",
                "予定・状態の変更案を作成する。",
                {
                    "operations": {"type": "array", "items": {"type": "object"}},
                    "reason": {"type": "string"},
                },
            ),
            function(
                "create_export",
                "利用者が依頼した Excel・報告文を生成する。",
                {
                    "format": {"type": "string", "enum": ["xlsx", "markdown"]},
                    "project_id": project_parameter,
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                },
            ),
        ]

        def project_id(value):
            if not value:
                return ""
            projects = [
                p for p in snapshot["pages"].values() if p.get("type") == "project"
            ]
            if value in {p["id"] for p in projects}:
                return value
            normalize = lambda text: re.sub(r"\s+", "", text).casefold()
            query = normalize(str(value))
            exact = [p for p in projects if normalize(p["title"]) == query]
            matching = exact or [
                p
                for p in projects
                if query in normalize(p["title"]) or normalize(p["title"]) in query
            ]
            if len(matching) == 1:
                return matching[0]["id"]
            raise ValueError(
                "案件の ID を指定してください。候補："
                + dumps([{"id": p["id"], "title": p["title"]} for p in projects])
            )

        def run_tool(name, args):
            if name in {"list_tasks", "show_gantt", "create_export"}:
                args = {**args, "project_id": project_id(args.get("project_id"))}
            if name in {"search_wiki", "read_pages", "read_sources"}:
                return handler(name, args)
            if name == "list_tasks":
                tasks = self.task_list(
                    snapshot,
                    {
                        "project_id": args.get("project_id"),
                        "scope": payload.get("scope"),
                    },
                )
                for task in tasks[:60]:
                    for evidence in task.get("evidence", []):
                        readable.setdefault(evidence["source_id"], []).append(
                            evidence["quote"]
                        )
                return tasks[:60]
            if name == "list_context_records":
                ids = [
                    p["id"]
                    for p in self.wiki.search(snapshot, "", args.get("subject_id", ""))
                    if p.get("type") in ("context", "person", "project", "task", "team")
                ]
                return handler("read_pages", {"ids": ids[:8]})
            if name == "show_gantt":
                result = {
                    "type": "gantt",
                    "project_id": args.get("project_id", ""),
                    "revision": snapshot["revision"],
                }
            elif name == "simulate_schedule":
                result = {
                    "type": "simulation",
                    **self.simulate(snapshot, args["operations"]),
                }
            elif name == "propose_task_changes":
                result = {
                    "type": "proposal",
                    **self.propose(
                        snapshot["revision"], args["operations"], args["reason"]
                    ),
                }
            elif name == "create_export":
                result = {
                    "type": "export",
                    **self.export(
                        snapshot, {"scope": payload.get("scope", ""), **args}
                    ),
                }
            else:
                raise ValueError("ツール名を確認してください。")
            tool_results.append(result)
            return result

        llm = self.llm_factory(self.s)
        prompt = (
            self.policy()
            + """
Wiki を参照して業務の相談に答える。最初に search_wiki と read_pages を使う。
原資料を追加調査できる。提案と確定情報を区別する。不足は「未確認」として必要な確認を示す。
現在や最新の状況は task.data.status と context.data.resolution_state を照合する。in_progress は進行中・着手済みである。質問された作業の現在の状態を回答し、本文の過去の出来事と時点を区別する。
最終出力は JSON: {"answer":"Markdown の回答", "citations":[{"source_id":"ID","quote":"正確な引用"}],"questions":[],"insufficient":false}。
citations には読んだ原資料またはページの出典から引用する。
ガント表示の依頼は show_gantt、Excel や報告文の出力依頼は create_export を実行してから結果を案内する。
案件の ID は projects から選ぶ。操作の依頼では実際にツールを呼び出す。
情報不足の回答は insufficient:true を付ける。
"""
        )

        def validate_answer(value):
            citations = value.get("citations", [])
            if (
                snapshot["pages"]
                and not citations
                and not value.get("insufficient")
                and not tool_results
            ):
                raise ValueError(
                    "出典付きの回答を作成してください。read_pages で出典を読めます。"
                )
            for item in citations:
                if item.get("source_id") not in readable:
                    raise ValueError(
                        "回答の出典を read_pages または read_sources で読んでください。"
                    )
                normalized = re.sub(r"\s+", " ", item.get("quote", "")).strip()
                if not normalized or not any(
                    normalized in re.sub(r"\s+", " ", text)
                    for text in readable[item["source_id"]]
                ):
                    raise ValueError(
                        "回答の quote を読んだ原資料から正確にコピーしてください。source_id="
                        + item["source_id"]
                    )
            if not value.get("answer"):
                raise ValueError("answer に日本語の回答を記載してください。")
            assert_plain_japanese(value["answer"])

        result = llm.run(
            prompt,
            dumps(
                {
                    "question": payload["text"],
                    "projects": project_index,
                    "scope": payload.get("scope", ""),
                    "date": datetime.now(timezone.utc).isoformat(),
                    "recent_messages": payload.get("history", [])[-8:],
                }
            ),
            tools,
            run_tool,
            model=self.s.query_model,
            response_schema=object_schema(
                {
                    "answer": {"type": "string"},
                    "citations": {**EVIDENCE_SCHEMA, "minItems": 0},
                    "questions": WIKI_RESULT_SCHEMA["properties"]["questions"],
                    "insufficient": {"type": "boolean"},
                }
            ),
            validator=validate_answer,
            final_tools={
                "show_gantt": "ガントを開けます。",
                "create_export": "報告を作成しました。ファイルをダウンロードできます。",
            },
        )
        gantt = next((t for t in tool_results if t["type"] == "gantt"), None)
        if gantt and not result.get("citations"):
            tasks = self.task_list(
                snapshot,
                {"project_id": gantt["project_id"], "scope": payload.get("scope")},
            )
            ids = {task["id"] for task in tasks}
            ids.update(
                task.get("project_id") for task in tasks if task.get("project_id")
            )
            ids.update(
                person for task in tasks for person in task.get("assignee_ids", [])
            )
            contexts = [
                p
                for p in snapshot["pages"].values()
                if p.get("type") == "context"
                and ids.intersection(p.get("data", {}).get("subject_ids", []))
                and self.wiki.in_scope(snapshot, p, payload.get("scope", ""))
            ]
            for page in contexts[:3]:
                result["answer"] += "\n\n" + page["summary"]
                for evidence in page.get("evidence", []):
                    readable.setdefault(evidence["source_id"], []).append(
                        evidence["quote"]
                    )
                    if evidence not in result["citations"]:
                        result["citations"].append(evidence)
        validate_answer(result)
        citations = result.get("citations", [])
        self.capture_questions(
            result.get("questions", []), snapshot["manifest"]["source_ids"]
        )
        answer = {
            "answer": result["answer"],
            "citations": citations,
            "revision": snapshot["revision"],
            "tools": tool_results,
            "usage": llm.usage,
        }
        message_id = uid("msg")
        with self.store.db() as c:
            c.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?)",
                (
                    message_id,
                    payload["conversation"],
                    "assistant",
                    answer["answer"],
                    dumps(answer),
                    time.time(),
                ),
            )
        return {"message_id": message_id, **answer}

    def lint(self, job):
        snapshot = self.wiki.snapshot(job["payload"].get("revision"))
        issues = []
        linked = set()
        for page in snapshot["pages"].values():
            linked.update(page.get("links", []))
            for key, value in page.get("data", {}).items():
                if key.endswith("_ids") and isinstance(value, list):
                    linked.update(value)
                elif key.endswith("_id") and isinstance(value, str):
                    linked.add(value)
        for page in snapshot["pages"].values():
            if page["id"] not in linked and len(snapshot["pages"]) > 1:
                issues.append(
                    {
                        "page_id": page["id"],
                        "message": "他のページからの参照を確認する。",
                    }
                )
        if snapshot["pages"]:
            llm = self.llm_factory(self.s)
            reader, _ = self.reader(snapshot)

            def validate_lint(value):
                for item in value.get("issues", []):
                    if item.get("page_id") not in snapshot["pages"]:
                        raise ValueError(
                            "issues.page_id は index のページ ID を使ってください。"
                        )
                    assert_plain_japanese(item.get("message", ""))
                for item in value.get("questions", []):
                    if not isinstance(item.get("evidence"), list):
                        raise ValueError(
                            "questions.evidence は source_id と quote の配列で記載してください。"
                        )
                    for key in ("question", "reason", "impact"):
                        assert_plain_japanese(item.get(key, ""))
                    self.wiki.validate(
                        {
                            "question_check": {
                                "id": "question_check",
                                "title": item["question"],
                                "body": "",
                                "evidence": item["evidence"],
                            }
                        },
                        set(snapshot["manifest"]["source_ids"]),
                    )

            result = llm.run(
                self.policy()
                + 'Wiki を読み、矛盾・古い記録・必要な不足情報を調べる。現在の状態は task.data と context.data を参照する。本文の過去の出来事と analysis の保存時点を照合する。確認事項は現在のデータに不足している情報を対象とし、evidence に原資料の正確な source_id と quote を記載する。JSON {"issues":[{"page_id":"ID","message":"内容"}],"questions":[]} を返す。',
                dumps(
                    {
                        "index": [
                            {"id": p["id"], "title": p["title"]}
                            for p in snapshot["pages"].values()
                        ]
                    }
                ),
                READ_TOOLS,
                reader,
                response_schema=object_schema(
                    {
                        "issues": {
                            "type": "array",
                            "items": object_schema(
                                {
                                    "page_id": {
                                        "type": "string",
                                        "enum": list(snapshot["pages"]),
                                    },
                                    "message": TEXT,
                                }
                            ),
                        },
                        "questions": WIKI_RESULT_SCHEMA["properties"]["questions"],
                    }
                ),
                validator=validate_lint,
            )
            issues.extend(result.get("issues", []))
            self.capture_questions(
                result.get("questions", []), snapshot["manifest"]["source_ids"]
            )
        return {"revision": snapshot["revision"], "issues": issues}

    def export(self, snapshot, filters):
        tasks = self.task_list(snapshot, filters)
        scope = filters.get("scope") or filters.get("project_id")

        def belongs(item):
            return self.wiki.in_scope(snapshot, item, scope)

        def title(page_id):
            return snapshot["pages"].get(page_id, {}).get("title", page_id) or "未指定"

        statuses = {
            "unknown": "未確認",
            "todo": "予定",
            "in_progress": "進行中",
            "blocked": "困りごとあり",
            "done": "完了",
            "cancelled": "中止",
            "open": "回答待ち",
            "deferred": "保留",
            "resolved": "解決",
        }
        kinds = {
            "blocker": "困りごと",
            "impression": "所感",
            "decision": "判断",
            "constraint": "制約",
            "note": "メモ",
        }
        attributions = {
            "self_report": "本人の記録",
            "reported_statement": "発言の記録",
            "observation": "観察",
            "proposal": "提案",
            "analysis": "分析",
        }
        contexts = [
            p
            for p in snapshot["pages"].values()
            if p.get("type") == "context" and belongs(p)
        ]
        open_questions = [
            q
            for q in self.questions()
            if q["status"] in ("open", "deferred")
            and (
                belongs(q)
                or belongs(snapshot["pages"].get(q["data"].get("subject_id"), {}))
            )
        ]
        start, end = filters.get("start"), filters.get("end")
        events = []
        for page in snapshot["pages"].values():
            if not belongs(page):
                continue
            for event in page.get("data", {}).get("events", []):
                day = event.get("date")
                if day and (not start or day >= start) and (not end or day <= end):
                    events.append({**event, "title": page["title"]})
        export_id = uid("export")
        directory = self.store.root / "exports"
        directory.mkdir(exist_ok=True)
        fmt = filters.get("format", "xlsx")
        lines = [
            "# 業務報告",
            "",
            f"対象期間：{start or '全期間'} ～ {end or '現在'}",
            f"参照版：{snapshot['revision']}",
            "",
            "## 期間内の記録",
            "",
        ]
        lines += [
            f"- {e['date']} {e['title']}：{e.get('description', '')}" for e in events
        ] or ["期間内の出来事は未登録。"]
        lines += ["", "## 現在のタスク", ""] + [
            f"- {t['title']}{'（' + t['ticket'] + '）' if t.get('ticket') else ''}：{statuses.get(t.get('status'), '未確認')}。担当：{'、'.join(title(p) for p in t.get('assignee_ids', [])) or '未定'}。予定 {t.get('plan_start') or '未定'} ～ {t.get('plan_end') or '未定'}。締め切り：{t.get('deadline') or '未定'}。"
            for t in tasks
        ]
        lines += ["", "## 困りごと・所感", ""] + [
            f"- {p['title']}：{p.get('summary', '')}" for p in contexts
        ]
        lines += ["", "## 確認事項", ""] + [
            f"- {title(q['data'].get('subject_id'))}：{q['data']['question']}"
            for q in open_questions
        ]
        text = "\n".join(lines)
        if fmt == "markdown":
            path = directory / (export_id + ".md")
            path.write_text(text)
        else:
            path = directory / (export_id + ".xlsx")
            book = Workbook()
            conditions = book.active
            conditions.title = "出力条件"
            conditions.append(["項目", "値"])
            for key, value in {
                "参照版": snapshot["revision"],
                "開始日": start or "全期間",
                "終了日": end or "現在",
                "対象範囲": title(scope) if scope else "全体",
                "作成日時": datetime.now(timezone.utc).isoformat(),
            }.items():
                conditions.append([key, value])
            sheet = book.create_sheet("タスク")
            sheet.append(
                [
                    "案件",
                    "ID",
                    "件名",
                    "担当",
                    "状態",
                    "予定開始",
                    "予定終了",
                    "締め切り",
                    "依存先",
                    "チケット",
                    "出典",
                ]
            )
            for t in tasks:
                sheet.append(
                    [
                        title(t.get("project_id")),
                        t["id"],
                        t["title"],
                        "、".join(title(p) for p in t.get("assignee_ids", [])),
                        statuses.get(t.get("status"), "未確認"),
                        t.get("plan_start"),
                        t.get("plan_end"),
                        t.get("deadline"),
                        "、".join(title(p) for p in t.get("dependency_ids", [])),
                        t.get("ticket"),
                        ", ".join(e["source_id"] for e in t["evidence"]),
                    ]
                )
            progress = book.create_sheet("進捗")
            progress.append(["日付", "対象", "実施内容", "出典"])
            for event in events:
                progress.append(
                    [
                        event["date"],
                        event["title"],
                        event.get("description", ""),
                        event.get("source_id", ""),
                    ]
                )
            questions = book.create_sheet("困りごと・確認事項")
            questions.append(["種別", "対象", "内容", "記録日", "性質", "状態", "出典"])
            for p in contexts:
                data = p.get("data", {})
                questions.append(
                    [
                        kinds.get(data.get("kind"), "メモ"),
                        "、".join(title(p) for p in data.get("subject_ids", [])),
                        p.get("summary", p["title"]),
                        data.get("occurred_at"),
                        attributions.get(data.get("attribution"), "未確認"),
                        {"open": "対応中", "resolved": "解決"}.get(
                            data.get("resolution_state"), "未確認"
                        ),
                        ", ".join(e["source_id"] for e in p.get("evidence", [])),
                    ]
                )
            for q in open_questions:
                questions.append(
                    [
                        "確認事項",
                        title(q["data"].get("subject_id")),
                        q["data"]["question"],
                        "",
                        "",
                        statuses[q["status"]],
                        ", ".join(
                            e["source_id"] for e in q["data"].get("evidence", [])
                        ),
                    ]
                )
            for ws in book:
                ws.freeze_panes = "A2"
                ws.auto_filter.ref = ws.dimensions
                for cell in ws[1]:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill("solid", fgColor="285B51")
                for row in ws.iter_rows():
                    for cell in row:
                        if isinstance(cell.value, str):
                            cell.data_type = "s"
                for column in ws.columns:
                    ws.column_dimensions[column[0].column_letter].width = min(
                        65, max(14, max(len(str(c.value or "")) for c in column) + 3)
                    )
            book.save(path)
        meta = {
            "revision": snapshot["revision"],
            "filters": filters,
            "format": fmt,
            "text": text,
        }
        with self.store.db() as c:
            c.execute(
                "INSERT INTO exports VALUES(?,?,?,?)",
                (export_id, dumps(meta), str(path), time.time()),
            )
        return {"id": export_id, "url": f"/api/v1/exports/{export_id}/download", **meta}

    def process(self, job):
        kind = job["kind"]
        if kind in {"sync", "ingest", "apply", "save_message"}:
            snapshot = self.wiki.snapshot()
            if job["id"] in snapshot["manifest"].get("applied_jobs", []):
                return {"revision": snapshot["revision"], "message": "反映済みです。"}
        if kind == "sync":
            return self.sync(job)
        if kind == "ingest":
            return self.ingest(
                job["payload"]["source_ids"],
                job["id"],
                question_id=job["payload"].get("question_id"),
            )
        if kind == "apply":
            return self.apply(job)
        if kind == "query":
            return self.query(job)
        if kind == "lint":
            return self.lint(job)
        if kind == "export":
            return self.export(
                self.wiki.snapshot(job["payload"].get("revision")), job["payload"]
            )
        if kind == "save_message":
            snapshot = self.wiki.snapshot()
            with self.store.db() as c:
                row = c.execute(
                    "SELECT * FROM messages WHERE id=? AND role='assistant'",
                    (job["payload"]["message_id"],),
                ).fetchone()
            if row is None:
                raise ValueError("保存する回答を確認してください。")
            data = json.loads(row["data"])
            if not data.get("citations"):
                raise ValueError("出典付きの回答を選んでください。")
            page_id = "analysis_" + row["id"]
            snapshot["pages"][page_id] = {
                "id": page_id,
                "type": "analysis",
                "title": row["body"][:50].replace("#", "").strip(),
                "summary": "相談から保存した分析。",
                "body": row["body"],
                "data": {"attribution": "analysis"},
                "evidence": data["citations"],
                "links": [],
            }
            return {
                "revision": self.wiki.publish(snapshot, job["id"]),
                "page_id": page_id,
            }
        raise ValueError("ジョブの種類を確認してください。")
