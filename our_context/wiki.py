import copy
import fcntl
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path

from .store import dumps
from .language import assert_plain_japanese


def git(directory, *args):
    result = subprocess.run(
        ["git", "-C", str(directory), *args], capture_output=True, timeout=120
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[:1000])
    return result.stdout


class Wiki:
    def __init__(self, store):
        self.store = store
        self.path = store.root / "knowledge"
        self.path.mkdir(exist_ok=True)
        self.cache = {}
        with (store.root / "publish.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not (self.path / ".git").exists():
                git(self.path, "init", "-b", "main")
                git(self.path, "config", "user.name", "Our Context")
                git(self.path, "config", "user.email", "our-context@localhost")
            if not store.get("revision"):
                (self.path / "wiki").mkdir(exist_ok=True)
                (self.path / "wiki/index.md").write_text(
                    "# Wiki\n\n業務の記録を追加すると、ここにページが並びます。\n"
                )
                (self.path / "wiki/log.md").write_text("# 更新履歴\n")
                (self.path / "manifest.json").write_text(
                    dumps({"source_ids": [], "source_current": {}, "gitea": {}})
                )
                policy = Path(__file__).resolve().parent.parent / "wiki-policy.md"
                (self.path / "AGENTS.md").write_text(
                    policy.read_text()
                    if policy.exists()
                    else "業務の文脈を出典付きで維持する。"
                )
                git(self.path, "add", ".")
                git(self.path, "commit", "-m", "Initialize wiki")
                store.set(
                    "revision", git(self.path, "rev-parse", "HEAD").decode().strip()
                )

    def snapshot(self, revision=None):
        revision = revision or self.store.get("revision")
        if not re.fullmatch(r"[a-f0-9]{40,64}", revision or ""):
            raise ValueError("参照版の形式を確認してください。")
        if revision not in self.cache:
            data = git(self.path, "archive", revision, "wiki", "manifest.json")
            pages = {}
            manifest = {}
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                for member in archive:
                    if not member.isfile():
                        continue
                    text = archive.extractfile(member).read().decode()
                    if member.name == "manifest.json":
                        manifest = json.loads(text)
                    elif member.name.startswith("wiki/") and text.startswith("---\n"):
                        header, body = text[4:].split("\n---\n", 1)
                        page = json.loads(header)
                        pages[page["id"]] = {**page, "body": body}
            self.cache[revision] = {
                "revision": revision,
                "pages": pages,
                "manifest": manifest,
            }
            if len(self.cache) > 8:
                del self.cache[next(iter(self.cache))]
        return copy.deepcopy(self.cache[revision])

    def validate(self, pages, source_ids):
        statuses = {"unknown", "todo", "in_progress", "blocked", "done", "cancelled"}
        errors = []
        missing_pages = set()
        invalid_pages = set()
        for key, page in pages.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", key) or key in {
                "index",
                "log",
            }:
                raise ValueError("ページ ID の形式を確認してください。")
            if not isinstance(page.get("title"), str) or not isinstance(
                page.get("body"), str
            ):
                raise ValueError("ページの見出しと本文を確認してください。")
            assert_plain_japanese(page["body"])
            assert_plain_japanese(page.get("summary", ""))
            evidence = page.get("evidence", [])
            if not evidence:
                invalid_pages.add(page["id"])
                errors.append(
                    f"{page['id']}: evidence に原資料 ID と原文の引用を設定してください。"
                )
            for item in evidence:
                if item.get("source_id") not in source_ids:
                    invalid_pages.add(page["id"])
                    errors.append(f"{page['id']}: 出典の原資料 ID を確認してください。")
                    continue
                raw = self.store.read_source(item["source_id"])["body"]
                quote = item.get("quote", "")
                normalize = lambda x: re.sub(r"\s+", " ", x).strip()
                if not normalize(quote) or normalize(quote) not in normalize(raw):
                    invalid_pages.add(page["id"])
                    import difflib

                    closest = difflib.get_close_matches(
                        quote, raw.splitlines(), n=1, cutoff=0.1
                    )
                    errors.append(
                        f"{page['id']}: 引用を原文から正確にコピーしてください。source_id={item['source_id']}、現在の引用={quote}、原文の候補={closest}"
                    )
            for target in page.get("links") or []:
                if target not in pages:
                    missing_pages.add(target)
                    errors.append(
                        f"{page['id']}: 関連ページ {target} を作成してください。"
                    )
            fields = page.get("data") or {}
            page["data"] = fields
            page["links"] = page.get("links") or []
            if page.get("type") == "context":
                choices = {
                    "kind": {"blocker", "impression", "decision", "constraint", "note"},
                    "attribution": {
                        "self_report",
                        "reported_statement",
                        "observation",
                        "proposal",
                        "analysis",
                    },
                }
                for field, allowed in choices.items():
                    if fields.get(field) not in allowed:
                        invalid_pages.add(page["id"])
                        errors.append(
                            f"{page['id']}.data.{field}: 原資料に合う値を設定してください。選択肢={sorted(allowed)}。他者の発言を記録した場合は attribution=reported_statement とします。"
                        )
                if fields.get("attribution") == "reported_statement" and not fields.get(
                    "speaker_id"
                ):
                    invalid_pages.add(page["id"])
                    errors.append(
                        f"{page['id']}: 発言者の person ID を speaker_id に設定してください。"
                    )
                fields.setdefault("resolution_state", "unknown")
                fields.setdefault("confirmation_state", "unconfirmed")
            reference_fields = (
                "project_id",
                "assignee_ids",
                "member_ids",
                "project_ids",
                "team_ids",
                "speaker_id",
                "recorder_id",
                "subject_ids",
            )
            for field in reference_fields:
                targets = fields.get(field) or []
                for target in targets if isinstance(targets, list) else [targets]:
                    if target != "owner" and target not in pages:
                        missing_pages.add(target)
                        errors.append(
                            f"{page['id']}.data.{field}: 参照先 {target} のページを作成してください。"
                        )
            if page.get("type") == "task":
                for key in ("assignee_ids", "dependency_ids"):
                    fields[key] = fields.get(key) or []
                    if not isinstance(fields[key], list) or not all(
                        isinstance(v, str) for v in fields[key]
                    ):
                        raise ValueError("担当と依存先を ID の一覧で指定してください。")
                fields["status"] = fields.get("status") or "unknown"
                if fields.get("status", "unknown") not in statuses:
                    raise ValueError("タスクの状態を確認してください。")
                for field in ("plan_start", "plan_end", "deadline"):
                    if fields.get(field):
                        from datetime import date

                        date.fromisoformat(fields[field])
                if (
                    fields.get("plan_start")
                    and fields.get("plan_end")
                    and fields["plan_start"] > fields["plan_end"]
                ):
                    raise ValueError("予定の開始日と終了日を確認してください。")
                if fields["status"] == "unknown":
                    stopped = [
                        p
                        for p in pages.values()
                        if p.get("type") == "context"
                        and page["id"] in (p.get("data", {}).get("subject_ids") or [])
                        and p.get("data", {}).get("resolution_state") == "open"
                        and "停止" in p.get("body", "")
                    ]
                    if stopped:
                        invalid_pages.add(page["id"])
                        errors.append(
                            f"{page['id']}: 関連記録に作業停止が明記されています。最新の記録を確認して status を設定してください。"
                        )
        if errors:
            error = ValueError("\n".join(errors[:12]))
            error.missing_page_ids = sorted(missing_pages)
            error.repair_page_ids = sorted(invalid_pages | missing_pages)
            error.page_index = [
                {"id": p["id"], "type": p.get("type"), "title": p["title"]}
                for p in pages.values()
            ]
            raise error

    def publish(self, snapshot, job_id, resolved_question=None, proposal_id=None):
        with (self.store.root / "publish.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.store.get("revision") != snapshot["revision"]:
                raise ValueError(
                    "参照版が更新されました。最新の内容で再実行してください。"
                )
            pages, manifest = snapshot["pages"], snapshot["manifest"]
            manifest.setdefault("applied_jobs", [])
            if job_id not in manifest["applied_jobs"]:
                manifest["applied_jobs"].append(job_id)
            self.validate(pages, set(manifest["source_ids"]))
            git(self.path, "reset", "--hard", snapshot["revision"])
            git(self.path, "clean", "-fd", "wiki", "raw")
            for path in (self.path / "wiki").glob("*.md"):
                if path.name != "log.md":
                    path.unlink()
            for page_id, page in pages.items():
                meta = {k: v for k, v in page.items() if k != "body"}
                (self.path / "wiki" / f"{page_id}.md").write_text(
                    "---\n" + dumps(meta) + "\n---\n" + page["body"] + "\n"
                )
            index = (
                "# Wiki\n\n"
                + "\n".join(
                    f"- [{p['title']}]({key}.md) — {p.get('summary', '')}"
                    for key, p in sorted(pages.items())
                )
                + "\n"
            )
            (self.path / "wiki/index.md").write_text(index)
            from datetime import datetime, timezone

            with (self.path / "wiki/log.md").open("a") as f:
                f.write(
                    f"\n## [{datetime.now(timezone.utc).isoformat()}] update | {job_id}\n\n更新後のページ数：{len(pages)}\n"
                )
            raw_dir = self.path / "raw"
            raw_dir.mkdir(exist_ok=True)
            for source_id in manifest["source_ids"]:
                path = raw_dir / f"{source_id}.md"
                if not path.exists():
                    path.write_text(self.store.read_source(source_id)["body"])
            (self.path / "manifest.json").write_text(dumps(manifest))
            git(self.path, "add", ".")
            git(self.path, "commit", "--allow-empty", "-m", f"Wiki update {job_id}")
            revision = git(self.path, "rev-parse", "HEAD").decode().strip()
            git(self.path, "update-ref", f"refs/runs/{job_id}", revision)
            with self.store.db() as c:
                if self.store.get("revision") != snapshot["revision"]:
                    raise ValueError("参照版が更新されました。")
                self.store.set("revision", revision, c)
                self.store.set(
                    "last_publish", datetime.now(timezone.utc).isoformat(), c
                )
                if resolved_question:
                    c.execute(
                        "UPDATE questions SET status='answered',version=version+1,updated=? WHERE id=?",
                        (__import__("time").time(), resolved_question),
                    )
                if proposal_id:
                    c.execute(
                        "UPDATE proposals SET status='applied' WHERE id=?",
                        (proposal_id,),
                    )
            return revision

    def scope_ids(self, snapshot, scope):
        ids = {scope}
        page = snapshot["pages"].get(scope, {})
        if page.get("type") == "team":
            references = dumps(
                {"links": page.get("links", []), "data": page.get("data", {})}
            )
            for related in snapshot["pages"].values():
                if related.get("type") in ("project", "person") and (
                    related["id"] in references or scope in dumps(related)
                ):
                    ids.add(related["id"])
        return ids

    def in_scope(self, snapshot, item, scope):
        return not scope or any(
            ref in dumps(item) for ref in self.scope_ids(snapshot, scope)
        )

    def search(self, snapshot, query="", scope=""):
        terms = [q.lower() for q in query.split() if q]
        found = []
        for page in snapshot["pages"].values():
            blob = dumps(page).lower()
            if not self.in_scope(snapshot, page, scope):
                continue
            score = sum(blob.count(term) for term in terms) if terms else 1
            if score:
                found.append((score, page))
        return [p for _, p in sorted(found, key=lambda pair: pair[0], reverse=True)]
