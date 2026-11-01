import contextlib
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path


def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex[:20]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.sqlite3"
        with self.db() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,logical_key TEXT NOT NULL,hash TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,metadata TEXT NOT NULL,created REAL NOT NULL,UNIQUE(logical_key,hash));
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,stage TEXT NOT NULL DEFAULT '',result TEXT,error TEXT,created REAL NOT NULL,updated REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,dedupe TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS questions(id TEXT PRIMARY KEY,semantic_key TEXT UNIQUE NOT NULL,data TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'open',version INTEGER NOT NULL DEFAULT 1,answer_source TEXT,snooze_until TEXT,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,conversation TEXT NOT NULL,role TEXT NOT NULL,body TEXT NOT NULL,data TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY,revision TEXT NOT NULL,data TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'open',created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,hash TEXT NOT NULL,job_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS idempotency(key TEXT PRIMARY KEY,hash TEXT NOT NULL,result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS exports(id TEXT PRIMARY KEY,data TEXT NOT NULL,path TEXT,created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS job_queue ON jobs(status,created);
            """)

    @contextlib.contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=15000")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def get(self, key, default=None):
        with self.db() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value, connection=None):
        if connection:
            connection.execute(
                "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, dumps(value)),
            )
        else:
            with self.db() as c:
                self.set(key, value, c)

    def source(self, title, body, kind="note", logical_key=None, metadata=None):
        logical_key = logical_key or uid("web")
        digest = hashlib.sha256(body.encode()).hexdigest()
        with self.db() as c:
            existing = c.execute(
                "SELECT * FROM sources WHERE logical_key=? AND hash=?",
                (logical_key, digest),
            ).fetchone()
            if existing:
                return dict(existing)
            source_id = uid("src")
            meta = {"kind": kind, **(metadata or {})}
            directory = self.root / "raw" / source_id
            directory.mkdir(parents=True, exist_ok=True)
            for name, content in [
                ("content.md", body),
                (
                    "metadata.json",
                    dumps(
                        {
                            "id": source_id,
                            "title": title,
                            "logical_key": logical_key,
                            "hash": digest,
                            **meta,
                        }
                    ),
                ),
            ]:
                temporary = directory / (name + ".tmp")
                with temporary.open("w") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                temporary.replace(directory / name)
            c.execute(
                "INSERT INTO sources VALUES(?,?,?,?,?,?,?)",
                (source_id, logical_key, digest, title, body, dumps(meta), time.time()),
            )
            return dict(
                c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            )

    def read_source(self, source_id):
        with self.db() as c:
            row = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if row is None:
                raise ValueError("原資料の ID を確認してください。")
            return {**dict(row), "metadata": json.loads(row["metadata"])}

    def enqueue(self, kind, payload, dedupe=None, connection=None):
        if connection is None:
            with self.db() as c:
                return self.enqueue(kind, payload, dedupe, c)
        if dedupe:
            row = connection.execute(
                "SELECT id FROM jobs WHERE dedupe=?", (dedupe,)
            ).fetchone()
            if row:
                return row[0]
        job_id = uid("job")
        now = time.time()
        connection.execute(
            "INSERT INTO jobs(id,kind,payload,status,created,updated,dedupe) VALUES(?,?,?,'queued',?,?,?)",
            (job_id, kind, dumps(payload), now, now, dedupe),
        )
        return job_id

    def claim(self):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created,id LIMIT 1"
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE jobs SET status='running',updated=?,attempts=attempts+1 WHERE id=?",
                    (time.time(), row["id"]),
                )
                return {**dict(row), "payload": json.loads(row["payload"])}

    def job(self, job_id):
        with self.db() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("ジョブの ID を確認してください。")
            return {
                **dict(row),
                "payload": json.loads(row["payload"]),
                "result": json.loads(row["result"]) if row["result"] else None,
            }

    def update_job(self, job_id, stage=None, result=None, error=None):
        with self.db() as c:
            if error:
                c.execute(
                    "UPDATE jobs SET status='failed',error=?,updated=? WHERE id=?",
                    (str(error)[:2000], time.time(), job_id),
                )
            elif result is not None:
                c.execute(
                    "UPDATE jobs SET status='succeeded',result=?,updated=? WHERE id=?",
                    (dumps(result), time.time(), job_id),
                )
            else:
                c.execute(
                    "UPDATE jobs SET stage=?,updated=? WHERE id=?",
                    (stage, time.time(), job_id),
                )
