import fcntl
import logging
import threading
import time

from .config import Settings
from .engine import Engine
from .store import Store


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    s = Settings()
    store = Store(s.root)
    with (s.root / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        engine = Engine(s, store)

        def heartbeat():
            while True:
                store.set("worker_heartbeat", time.time())
                time.sleep(5)

        threading.Thread(target=heartbeat, daemon=True).start()
        with store.db() as c:
            c.execute(
                "UPDATE jobs SET status='queued',stage='再開しています' WHERE status='running'"
            )
            c.execute(
                "UPDATE questions SET status='open' WHERE status='deferred' AND snooze_until IS NOT NULL AND snooze_until<=date('now')"
            )
        last_sync = 0
        while True:
            store.set("worker_heartbeat", time.time())
            job = store.claim()
            if job:
                logging.info("job=%s kind=%s started", job["id"], job["kind"])
                try:
                    result = engine.process(job)
                    store.update_job(job["id"], result=result)
                    logging.info("job=%s succeeded", job["id"])
                except Exception as exc:
                    message = str(exc)
                    for secret in (
                        s.llm_key,
                        s.gitea_token,
                        s.password,
                        s.webhook_secret,
                    ):
                        if secret:
                            message = message.replace(secret, "[key]")
                    store.update_job(job["id"], error=message)
                    if job["payload"].get("question_id"):
                        with store.db() as c:
                            c.execute(
                                "UPDATE questions SET status='answer_failed',updated=? WHERE id=?",
                                (time.time(), job["payload"]["question_id"]),
                            )
                    logging.info("job=%s failed", job["id"])
            else:
                if (
                    s.gitea_url
                    and s.gitea_repo
                    and s.ingest_model
                    and time.time() - last_sync >= s.sync_interval
                ):
                    store.enqueue(
                        "sync",
                        {},
                        "reconcile:"
                        + str(int(time.time() // max(s.sync_interval, 60))),
                    )
                    last_sync = time.time()
                time.sleep(1)


if __name__ == "__main__":
    main()
