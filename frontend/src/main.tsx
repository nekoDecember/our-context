import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Markdown } from "./markdown";
import "./style.css";

let csrf = "";
async function api(path: string, body?: any, method?: string) {
  const response = await fetch("/api/v1" + path, {
    method: method || (body === undefined ? "GET" : "POST"),
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrf,
      "Idempotency-Key": crypto.randomUUID
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random()}`,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const raw = await response.text();
  let data: any;
  try {
    data = JSON.parse(raw);
  } catch {
    data = { detail: raw.slice(0, 300) || "接続状況を確認してください。" };
  }
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  return data;
}
const labels: any = {
  unknown: "未確認",
  todo: "予定",
  in_progress: "進行中",
  blocked: "困りごとあり",
  done: "完了",
  cancelled: "中止",
  open: "回答待ち",
  deferred: "保留",
  dismissed: "不要",
  answering: "反映待ち",
  answered: "回答済み",
  answer_failed: "反映失敗",
  obsolete: "解決済み",
  self_report: "本人の発言",
  reported_statement: "発言の記録",
  observation: "観察",
  proposal: "提案",
  analysis: "分析",
  blocker: "困りごと",
  impression: "所感",
  decision: "判断",
  constraint: "制約",
  note: "メモ",
  project: "案件",
  team: "チーム",
  person: "人物",
  task: "タスク",
  knowledge: "業務知識",
  context: "文脈",
  queued: "待機中",
  running: "処理中",
  succeeded: "完了",
  failed: "失敗",
  sync: "Gitea 同期",
  ingest: "メモの整理",
  query: "相談への回答",
  apply: "予定・状態の反映",
  export: "報告の作成",
  lint: "Wiki の点検",
  save_message: "相談の Wiki 保存",
};
const nav = [
  ["home", "ホーム", "⌂"],
  ["wiki", "Wiki", "▤"],
  ["tasks", "タスク・予定", "▦"],
  ["questions", "確認事項", "?"],
  ["chat", "相談", "↗"],
  ["reports", "報告", "↓"],
  ["settings", "設定・処理状況", "⚙"],
];
function Badge({ value }: any) {
  return <span className={"badge " + value}>{labels[value] || value}</span>;
}
const fmt = (value: any) =>
  value
    ? new Date(typeof value === "number" ? value * 1000 : value).toLocaleString(
        "ja-JP",
        { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" },
      )
    : "未反映";

function App() {
  const [auth, setAuth] = useState<boolean | null>(null),
    [password, setPassword] = useState(""),
    [error, setError] = useState("");
  const [boot, setBoot] = useState<any>(null),
    [view, setView] = useState("home"),
    [scope, setScope] = useState(""),
    [search, setSearch] = useState(""),
    [type, setType] = useState("");
  const [pageId, setPageId] = useState(""),
    [source, setSource] = useState<any>(null),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const [noteOpen, setNoteOpen] = useState(false),
    [noteTitle, setNoteTitle] = useState("業務メモ"),
    [note, setNote] = useState("");
  const [jobs, setJobs] = useState<any[]>([]),
    [status, setStatus] = useState(""),
    [assignee, setAssignee] = useState(""),
    [taskStart, setTaskStart] = useState(""),
    [taskEnd, setTaskEnd] = useState(""),
    [taskEdit, setTaskEdit] = useState<any>(null),
    [proposal, setProposal] = useState<any>(null);
  const [conversation, setConversation] = useState(
      () => localStorage.getItem("our-context-conversation") || "",
    ),
    [messages, setMessages] = useState<any[]>([]),
    [question, setQuestion] = useState("");
  const [reportStart, setReportStart] = useState(""),
    [reportEnd, setReportEnd] = useState(""),
    [exported, setExported] = useState<any>(null),
    [checks, setChecks] = useState<any>({}),
    [guide, setGuide] = useState(""),
    [lintResult, setLintResult] = useState<any>(null);
  async function refresh() {
    const b = await api("/bootstrap");
    setBoot(b);
    return b;
  }
  useEffect(() => {
    api("/session")
      .then((session) => {
        csrf = session.csrf;
        setAuth(session.authenticated);
        if (session.authenticated) refresh().catch((e) => setError(e.message));
      })
      .catch((e) => {
        setAuth(false);
        setError(e.message);
      });
  }, []);
  useEffect(() => {
    if (auth && view === "settings")
      api("/jobs")
        .then(setJobs)
        .catch((e) => setError(e.message));
  }, [view, auth, notice]);
  useEffect(() => {
    if (conversation && auth)
      api("/conversations/" + conversation)
        .then(setMessages)
        .catch(() => {});
  }, [conversation, auth]);
  async function safe(action: () => Promise<any>) {
    setError("");
    try {
      return await action();
    } catch (e: any) {
      setError(e.message);
      return null;
    }
  }
  async function watch(result: any) {
    if (!result?.job_id) return result;
    setBusy(true);
    try {
      for (let i = 0; i < 1000; i++) {
        const job = await api("/jobs/" + result.job_id);
        setNotice(job.stage || labels[job.status] || job.status);
        if (job.status === "failed")
          throw new Error(job.error || "処理の状況を確認してください。");
        if (job.status === "succeeded") {
          await refresh();
          setNotice("反映しました。");
          return job.result;
        }
        await new Promise((r) => setTimeout(r, 1000));
      }
      throw new Error("処理を継続しています。設定・処理状況で確認できます。");
    } finally {
      setBusy(false);
    }
  }
  async function openSource(id: string) {
    await safe(async () => setSource(await api("/sources/" + id)));
  }
  function openPage(id: string) {
    setSearch("");
    setType("");
    const target = boot?.pages.find((p: any) => p.id === id);
    if (scope && target && !JSON.stringify(target).includes(scope))
      setScope("");
    setPageId(id);
    setView("wiki");
  }
  async function submitNote(e: any) {
    e.preventDefault();
    await safe(async () => {
      await watch(
        await api("/sources", { title: noteTitle, text: note, kind: "note" }),
      );
      setNote("");
      setNoteOpen(false);
    });
  }
  async function ask(e: any) {
    e.preventDefault();
    if (!question.trim()) return;
    await safe(async () => {
      let id = conversation;
      if (!id) {
        id = (await api("/conversations", {})).id;
        setConversation(id);
        localStorage.setItem("our-context-conversation", id);
      }
      const text = question;
      setQuestion("");
      setMessages((old) => [
        ...old,
        { id: "pending", role: "user", body: text, data: {} },
      ]);
      const result = await watch(
        await api("/conversations/" + id + "/queries", {
          text,
          revision: boot.revision,
          scope,
        }),
      );
      setMessages(await api("/conversations/" + id));
      return result;
    });
  }
  async function createReport(format: string) {
    await safe(async () =>
      setExported(
        await watch(
          await api("/exports", {
            revision: boot.revision,
            format,
            scope,
            start: reportStart || null,
            end: reportEnd || null,
          }),
        ),
      ),
    );
  }
  const pages: any[] = boot?.pages || [],
    tasks: any[] = boot?.tasks || [],
    questions: any[] = boot?.questions || [];
  const projects = pages.filter((p) => p.type === "project"),
    people = pages.filter((p) => p.type === "person");
  const name = (id: string) =>
    pages.find((p) => p.id === id)?.title || id || "未指定";
  const inScope = (item: any) => {
    if (!scope) return true;
    const subject = pages.find((p) => p.id === item.data?.subject_id);
    const text = JSON.stringify([item, subject]);
    return (boot?.scope_members?.[scope] || [scope]).some((id: string) =>
      text.includes(id),
    );
  };
  const scopedTasks = tasks
    .filter(inScope)
    .filter((t) => !status || t.status === status)
    .filter((t) => !assignee || t.assignee_ids?.includes(assignee))
    .filter((t) => !taskStart || !t.plan_end || t.plan_end >= taskStart)
    .filter((t) => !taskEnd || !t.plan_start || t.plan_start <= taskEnd);
  const scopedPages = pages
    .filter(inScope)
    .filter((p) => !type || p.type === type)
    .filter(
      (p) =>
        !search ||
        JSON.stringify(p).toLowerCase().includes(search.toLowerCase()),
    );
  const selected = scopedPages.find((p) => p.id === pageId) || scopedPages[0];
  const contexts = pages.filter((p) => p.type === "context" && inScope(p));
  const openQuestions = questions.filter(
    (q) =>
      ["open", "answer_failed", "answering"].includes(q.status) && inScope(q),
  );
  function Evidence({ items }: any) {
    return (
      <div className="evidence">
        {(items || []).map((e: any, i: number) => (
          <button key={i} onClick={() => openSource(e.source_id)}>
            原資料 {i + 1}
            <span>{e.quote?.slice(0, 55)}</span>
          </button>
        ))}
      </div>
    );
  }
  function ToolCard({ tool }: any) {
    return (
      <div className="tool-card">
        {tool.type === "gantt" ? (
          <button
            onClick={() => {
              setScope(tool.project_id || "");
              setView("tasks");
            }}
          >
            ガントを開く
          </button>
        ) : tool.type === "proposal" ? (
          <>
            <strong>予定の変更案</strong>
            <p>{tool.reason}</p>
            <button onClick={() => setProposal(tool)}>変更内容を確認</button>
          </>
        ) : tool.type === "export" ? (
          <a className="button primary" href={tool.url}>
            報告をダウンロード
          </a>
        ) : (
          <>
            <strong>予定の試算</strong>
            {(tool.impacts || []).map((i: any) => (
              <p key={i.task_id}>
                {i.title}：{i.set.plan_start} ～ {i.set.plan_end}
              </p>
            ))}
            {(tool.warnings || []).map((w: string) => (
              <p key={w}>{w}</p>
            ))}
          </>
        )}
      </div>
    );
  }
  if (auth === null) return <div className="loading">読み込んでいます。</div>;
  if (!auth)
    return (
      <main className="login">
        <div className="login-mark">OC</div>
        <h1>Our Context</h1>
        <p>業務の記録と相談</p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            safe(async () => {
              const result = await api("/auth/login", { password });
              csrf = result.csrf;
              setAuth(true);
              setPassword("");
              await refresh();
            });
          }}
        >
          <label>
            共有パスワード
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoFocus
            />
          </label>
          <button className="primary">ログイン</button>
        </form>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <small>パスワードは .env の APP_PASSWORD に保存されています。</small>
      </main>
    );
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span>OC</span>
          <div>
            Our Context<small>業務の記録と相談</small>
          </div>
        </div>
        <nav>
          {nav.map(([key, label, icon]) => (
            <button
              key={key}
              className={view === key ? "active" : ""}
              onClick={() => setView(key)}
            >
              <span className="nav-icon">{icon}</span>
              {label}
              {key === "questions" && openQuestions.length > 0 && (
                <b>{openQuestions.length}</b>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <i className={boot?.worker_online ? "online" : "offline"} />
          {boot?.worker_online ? "ワーカー稼働中" : "ワーカー確認待ち"}
          <button
            onClick={() =>
              safe(async () => {
                await api("/auth/logout", {});
                setAuth(false);
              })
            }
          >
            ログアウト
          </button>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="scope">
            <label htmlFor="scope">参照範囲</label>
            <select
              id="scope"
              value={scope}
              onChange={(e) => setScope(e.target.value)}
            >
              <option value="">全体</option>
              {pages
                .filter(
                  (p) =>
                    ["project", "team", "person"].includes(p.type) ||
                    p.id === scope,
                )
                .map((p) => (
                  <option key={p.id} value={p.id}>
                    {labels[p.type]}：{p.title}
                  </option>
                ))}
            </select>
          </div>
          <div className="top-actions">
            <span className="sync-date">
              最終反映 {fmt(boot?.last_publish)}
            </span>
            <button className="primary" onClick={() => setNoteOpen(true)}>
              ＋ 記録を追加
            </button>
          </div>
        </header>
        <main className="main">
          <div className="page-heading">
            <div>
              <div className="eyebrow">OUR CONTEXT</div>
              <h1>{nav.find((n) => n[0] === view)?.[1]}</h1>
            </div>
            <button onClick={() => safe(refresh)}>更新</button>
          </div>
          {error && (
            <div className="error" role="alert">
              {error}
              <button onClick={() => setError("")}>閉じる</button>
            </div>
          )}
          {notice && (
            <div
              className={"notice " + (busy ? "processing" : "")}
              role="status"
            >
              {busy && <span className="spinner" />}
              {notice}
            </div>
          )}
          {!boot ? (
            <p>データを読み込んでいます。</p>
          ) : (
            <>
              {view === "home" && (
                <>
                  {pages.length === 0 ? (
                    <section className="welcome">
                      <div className="eyebrow">はじめに</div>
                      <h2>チームや案件の記録を追加する</h2>
                      <p>
                        自分の役割、チーム体制、進行中の案件、仕事のルールをメモで登録できます。
                      </p>
                      <div className="actions">
                        <button
                          className="primary"
                          onClick={() => {
                            setNoteTitle("チームと案件の初期情報");
                            setNoteOpen(true);
                          }}
                        >
                          初期情報を追加
                        </button>
                        <button onClick={() => setView("settings")}>
                          接続を設定
                        </button>
                      </div>
                    </section>
                  ) : (
                    <div className="stats">
                      <div>
                        <span>Wiki ページ</span>
                        <strong>{pages.length}</strong>
                      </div>
                      <div>
                        <span>進行中のタスク</span>
                        <strong>
                          {
                            tasks.filter(
                              (t) =>
                                ["todo", "in_progress", "blocked"].includes(
                                  t.status,
                                ) && inScope(t),
                            ).length
                          }
                        </strong>
                      </div>
                      <div>
                        <span>確認事項</span>
                        <strong>{openQuestions.length}</strong>
                      </div>
                    </div>
                  )}
                  <div className="two-column">
                    <section className="panel">
                      <div className="section-heading">
                        <h2>確認したいこと</h2>
                        <button onClick={() => setView("questions")}>
                          一覧へ →
                        </button>
                      </div>
                      {openQuestions.slice(0, 4).map((q) => (
                        <button
                          key={q.id}
                          className="list-row"
                          onClick={() => setView("questions")}
                        >
                          <Badge value={q.status} />
                          <strong>{q.data.question}</strong>
                          <span>{q.data.reason}</span>
                        </button>
                      ))}
                      {!openQuestions.length && (
                        <p className="empty">回答待ちの確認事項は0件です。</p>
                      )}
                    </section>
                    <section className="panel">
                      <div className="section-heading">
                        <h2>案件とチーム</h2>
                        <button onClick={() => setView("wiki")}>
                          Wiki へ →
                        </button>
                      </div>
                      {pages
                        .filter(
                          (p) =>
                            ["project", "team"].includes(p.type) && inScope(p),
                        )
                        .slice(0, 6)
                        .map((p) => (
                          <button
                            key={p.id}
                            className="list-row"
                            onClick={() => openPage(p.id)}
                          >
                            <Badge value={p.type} />
                            <strong>{p.title}</strong>
                            <span>{p.summary}</span>
                          </button>
                        ))}
                      {!projects.length && (
                        <p className="empty">
                          記録から案件やチームの情報を整理します。
                        </p>
                      )}
                    </section>
                  </div>
                  <section className="panel">
                    <div className="section-heading">
                      <h2>困りごと・所感</h2>
                      <button
                        onClick={() => {
                          setType("context");
                          setView("wiki");
                        }}
                      >
                        記録を見る →
                      </button>
                    </div>
                    <div className="context-grid">
                      {contexts.slice(0, 6).map((p) => (
                        <button
                          className="context-card"
                          key={p.id}
                          onClick={() => openPage(p.id)}
                        >
                          <Badge value={p.data?.kind || "note"} />
                          <h3>{p.title}</h3>
                          <p>{p.summary}</p>
                          <small>
                            {p.data?.occurred_at || "記録日未確認"} ·{" "}
                            {labels[p.data?.attribution] || "出所を確認"}
                          </small>
                        </button>
                      ))}
                    </div>
                    {!contexts.length && (
                      <p className="empty">
                        メモに書かれた困りごとや所感をここにまとめます。
                      </p>
                    )}
                  </section>
                </>
              )}
              {view === "wiki" && (
                <>
                  <div className="toolbar">
                    <input
                      aria-label="Wiki を検索"
                      placeholder="Wiki を検索"
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                    />
                    <select
                      aria-label="ページの種類"
                      value={type}
                      onChange={(e) => setType(e.target.value)}
                    >
                      <option value="">全ての種類</option>
                      {[
                        "project",
                        "team",
                        "person",
                        "task",
                        "context",
                        "knowledge",
                        "analysis",
                      ].map((t) => (
                        <option key={t} value={t}>
                          {labels[t]}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="wiki-layout">
                    <section className="page-list">
                      {scopedPages.map((p) => (
                        <button
                          key={p.id}
                          className={selected?.id === p.id ? "selected" : ""}
                          onClick={() => setPageId(p.id)}
                        >
                          <Badge value={p.type} />
                          <strong>{p.title}</strong>
                          <span>{p.summary}</span>
                        </button>
                      ))}
                      {!scopedPages.length && (
                        <p className="empty">条件に合うページは0件です。</p>
                      )}
                    </section>
                    <article className="wiki-body">
                      {selected ? (
                        <>
                          <div className="section-heading">
                            <Badge value={selected.type} />
                            <button
                              onClick={() => {
                                setScope(selected.id);
                                setView("chat");
                              }}
                            >
                              この内容で相談
                            </button>
                          </div>
                          <h2>{selected.title}</h2>
                          <Markdown
                            text={selected.body}
                            onPage={openPage}
                            onSource={openSource}
                          />
                          {selected.data?.attribution && (
                            <p className="attribution">
                              {labels[selected.data.attribution]} ·{" "}
                              {selected.data.occurred_at || "記録日未確認"}
                            </p>
                          )}
                          {selected.links?.length > 0 && (
                            <section>
                              <h3>関連ページ</h3>
                              <div className="actions">
                                {selected.links.map((id: string) => (
                                  <button key={id} onClick={() => openPage(id)}>
                                    {name(id)}
                                  </button>
                                ))}
                              </div>
                            </section>
                          )}
                          <h3>出典</h3>
                          <Evidence items={selected.evidence} />
                        </>
                      ) : (
                        <p className="empty">ページを選択してください。</p>
                      )}
                    </article>
                  </div>
                </>
              )}
              {view === "tasks" && (
                <>
                  <div className="toolbar">
                    <select
                      aria-label="タスクの状態"
                      value={status}
                      onChange={(e) => setStatus(e.target.value)}
                    >
                      <option value="">全ての状態</option>
                      {[
                        "unknown",
                        "todo",
                        "in_progress",
                        "blocked",
                        "done",
                        "cancelled",
                      ].map((s) => (
                        <option key={s} value={s}>
                          {labels[s]}
                        </option>
                      ))}
                    </select>
                    <select
                      aria-label="担当者"
                      value={assignee}
                      onChange={(e) => setAssignee(e.target.value)}
                    >
                      <option value="">全ての担当</option>
                      {people.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.title}
                        </option>
                      ))}
                    </select>
                    <input
                      aria-label="予定の開始範囲"
                      type="date"
                      value={taskStart}
                      onInput={(e) => setTaskStart(e.currentTarget.value)}
                      onChange={(e) => setTaskStart(e.target.value)}
                      style={{ maxWidth: 160 }}
                    />
                    <input
                      aria-label="予定の終了範囲"
                      type="date"
                      value={taskEnd}
                      onInput={(e) => setTaskEnd(e.currentTarget.value)}
                      min={taskStart}
                      onChange={(e) => setTaskEnd(e.target.value)}
                      style={{ maxWidth: 160 }}
                    />
                    <span className="muted">{scopedTasks.length} 件</span>
                  </div>
                  <section className="panel">
                    <div className="section-heading">
                      <h2>予定</h2>
                      <span className="muted">暦日 · 締め切りは ◆</span>
                    </div>
                    <Gantt tasks={scopedTasks} onEdit={setTaskEdit} />
                  </section>
                  <section className="panel">
                    <h2>タスク一覧</h2>
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>タスク</th>
                            <th>案件・担当</th>
                            <th>状態</th>
                            <th>予定</th>
                            <th>締め切り</th>
                            <th />
                          </tr>
                        </thead>
                        <tbody>
                          {scopedTasks.map((t) => (
                            <tr key={t.id}>
                              <td>
                                <button
                                  className="inline-link"
                                  onClick={() => openPage(t.id)}
                                >
                                  {t.title}
                                </button>
                              </td>
                              <td>
                                {name(t.project_id)}
                                <small>
                                  {(t.assignee_ids || [])
                                    .map(name)
                                    .join("、") || "担当未確認"}
                                </small>
                              </td>
                              <td>
                                <Badge value={t.status || "unknown"} />
                              </td>
                              <td>
                                {t.plan_start || "未定"}
                                <small>～ {t.plan_end || "未定"}</small>
                              </td>
                              <td>{t.deadline || "未確認"}</td>
                              <td>
                                <button onClick={() => setTaskEdit(t)}>
                                  編集
                                </button>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    {!scopedTasks.length && (
                      <p className="empty">
                        タスクを含む業務メモを追加してください。
                      </p>
                    )}
                  </section>
                </>
              )}
              {view === "questions" && (
                <>
                  <p className="description">
                    業務を進めるための確認事項です。回答は原資料として保存され、Wiki
                    に反映されます。
                  </p>
                  {questions.filter(inScope).map((q) => (
                    <QuestionCard
                      key={q.id}
                      question={q}
                      evidence={<Evidence items={q.data.evidence} />}
                      onAnswer={(text: string) =>
                        safe(async () =>
                          watch(
                            await api("/questions/" + q.id + "/answers", {
                              text,
                              question_version: q.version,
                            }),
                          ),
                        )
                      }
                      onState={(next: string) =>
                        safe(async () => {
                          await api(
                            "/questions/" + q.id,
                            { status: next, version: q.version },
                            "PATCH",
                          );
                          await refresh();
                        })
                      }
                      busy={busy}
                    />
                  ))}
                  {!questions.filter(inScope).length && (
                    <p className="empty panel">確認事項は0件です。</p>
                  )}
                </>
              )}
              {view === "chat" && (
                <div className="chat">
                  <div className="chat-intro">
                    <h2>業務の文脈を踏まえて相談する</h2>
                    <p>
                      予定の整理、困りごとの確認、報告の作成を相談できます。
                    </p>
                    {!messages.length && (
                      <div className="suggestions">
                        {[
                          "今、誰が何に困っている？",
                          "今週の予定と未確認事項を整理して",
                          "次の定例で確認することをまとめて",
                        ].map((q) => (
                          <button key={q} onClick={() => setQuestion(q)}>
                            {q}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                  {messages.map((m) => (
                    <article key={m.id} className={"message " + m.role}>
                      <div className="message-role">
                        {m.role === "user" ? "自分" : "AI"}
                      </div>
                      <Markdown
                        text={m.body}
                        onPage={openPage}
                        onSource={openSource}
                      />
                      {m.role === "assistant" && (
                        <>
                          <Evidence items={m.data.citations} />
                          {(m.data.tools || []).map((t: any, i: number) => (
                            <ToolCard tool={t} key={i} />
                          ))}
                          <button
                            onClick={() =>
                              safe(async () =>
                                watch(
                                  await api("/messages/" + m.id + "/save", {}),
                                ),
                              )
                            }
                          >
                            Wiki に残す
                          </button>
                        </>
                      )}
                    </article>
                  ))}
                  <form className="chat-form" onSubmit={ask}>
                    <label className="sr-only" htmlFor="chat-question">
                      相談内容
                    </label>
                    <textarea
                      id="chat-question"
                      placeholder="相談内容を入力"
                      value={question}
                      onChange={(e) => setQuestion(e.target.value)}
                      rows={3}
                      required
                    />
                    <div>
                      <small>{scope ? name(scope) : "全体"} の文脈を参照</small>
                      <button className="primary" disabled={busy}>
                        送信
                      </button>
                    </div>
                  </form>
                  <button
                    className="quiet"
                    onClick={() => {
                      setConversation("");
                      setMessages([]);
                      localStorage.removeItem("our-context-conversation");
                    }}
                  >
                    新しい相談
                  </button>
                </div>
              )}
              {view === "reports" && (
                <>
                  <section className="panel report-controls">
                    <h2>業務報告を作成</h2>
                    <p className="description">
                      対象期間の記録と、現在の進捗・困りごとを出力します。
                    </p>
                    <div className="form-grid">
                      <label>
                        開始日
                        <input
                          type="date"
                          value={reportStart}
                          onInput={(e) => setReportStart(e.currentTarget.value)}
                          onChange={(e) => setReportStart(e.target.value)}
                        />
                      </label>
                      <label>
                        終了日
                        <input
                          type="date"
                          value={reportEnd}
                          onInput={(e) => setReportEnd(e.currentTarget.value)}
                          onChange={(e) => setReportEnd(e.target.value)}
                        />
                      </label>
                    </div>
                    <p>対象：{scope ? name(scope) : "全体"}</p>
                    <div className="actions">
                      <button
                        className="primary"
                        disabled={busy}
                        onClick={() => createReport("xlsx")}
                      >
                        Excel を作成
                      </button>
                      <button
                        disabled={busy}
                        onClick={() => createReport("markdown")}
                      >
                        報告文を作成
                      </button>
                    </div>
                  </section>
                  {exported && (
                    <section className="panel">
                      <div className="section-heading">
                        <h2>作成した報告</h2>
                        <a className="button primary" href={exported.url}>
                          ダウンロード
                        </a>
                      </div>
                      <Markdown text={exported.text} />
                      <button
                        onClick={() =>
                          safe(async () => {
                            if (navigator.clipboard)
                              await navigator.clipboard.writeText(
                                exported.text,
                              );
                            else {
                              const el = document.createElement("textarea");
                              el.value = exported.text;
                              document.body.append(el);
                              el.select();
                              document.execCommand("copy");
                              el.remove();
                            }
                            setNotice("報告文をコピーしました。");
                          })
                        }
                      >
                        報告文をコピー
                      </button>
                    </section>
                  )}
                </>
              )}
              {view === "settings" && (
                <>
                  <section className="panel">
                    <div className="section-heading">
                      <h2>接続設定</h2>
                      <button
                        onClick={() =>
                          safe(async () =>
                            setGuide(await api("/guides/connections")),
                          )
                        }
                      >
                        接続ガイドを開く
                      </button>
                    </div>
                    <p className="description">
                      .env を編集した後、docker compose up -d
                      で設定を反映します。
                    </p>
                    <div className="two-column">
                      <div>
                        <h3>LiteLLM</h3>
                        <dl>
                          <dt>API URL</dt>
                          <dd>{boot.settings.llm_url || "未設定"}</dd>
                          <dt>取り込みモデル</dt>
                          <dd>{boot.settings.ingest_model || "未設定"}</dd>
                          <dt>相談モデル</dt>
                          <dd>{boot.settings.query_model || "未設定"}</dd>
                          <dt>API キー</dt>
                          <dd>
                            {boot.settings.llm_key_set ? "設定済み" : "未設定"}
                          </dd>
                        </dl>
                        <button
                          disabled={busy}
                          onClick={() =>
                            safe(async () => {
                              setChecks((old) => ({ ...old, llm: "確認中" }));
                              const result = await api(
                                "/settings/check/llm",
                                {},
                              );
                              setChecks((old) => ({ ...old, llm: result }));
                            })
                          }
                        >
                          LiteLLM の接続を確認
                        </button>
                        {checks.llm && (
                          <pre>{JSON.stringify(checks.llm, null, 2)}</pre>
                        )}
                      </div>
                      <div>
                        <h3>Gitea</h3>
                        <dl>
                          <dt>URL</dt>
                          <dd>{boot.settings.gitea_url || "未設定"}</dd>
                          <dt>リポジトリ</dt>
                          <dd>{boot.settings.gitea_repo || "未設定"}</dd>
                          <dt>ブランチ</dt>
                          <dd>{boot.settings.gitea_branch}</dd>
                          <dt>対象ファイル</dt>
                          <dd>{boot.settings.allowed_paths.join("、")}</dd>
                        </dl>
                        <div className="actions">
                          <button
                            onClick={() =>
                              safe(async () => {
                                const result = await api(
                                  "/settings/check/gitea",
                                  {},
                                );
                                setChecks((old) => ({ ...old, gitea: result }));
                              })
                            }
                          >
                            Gitea の接続を確認
                          </button>
                          <button
                            disabled={busy}
                            onClick={() =>
                              safe(async () =>
                                watch(await api("/connectors/gitea/sync", {})),
                              )
                            }
                          >
                            今すぐ同期
                          </button>
                        </div>
                        {checks.gitea && (
                          <pre>{JSON.stringify(checks.gitea, null, 2)}</pre>
                        )}
                        <details>
                          <summary>履歴の分岐を再同期する</summary>
                          <p>
                            現在のブランチから対象ファイルを読み直し、Wiki
                            を更新します。
                          </p>
                          <button
                            disabled={busy}
                            onClick={() =>
                              safe(async () =>
                                watch(
                                  await api("/connectors/gitea/sync", {
                                    force: true,
                                  }),
                                ),
                              )
                            }
                          >
                            再同期
                          </button>
                        </details>
                      </div>
                    </div>
                  </section>
                  {guide && (
                    <section className="panel">
                      <div className="section-heading">
                        <h2>接続ガイド</h2>
                        <button onClick={() => setGuide("")}>閉じる</button>
                      </div>
                      <Markdown text={guide} />
                    </section>
                  )}
                  <section className="panel">
                    <div className="section-heading">
                      <h2>Wiki の点検</h2>
                      <button
                        disabled={busy}
                        onClick={() =>
                          safe(async () =>
                            setLintResult(
                              await watch(await api("/lint-runs", {})),
                            ),
                          )
                        }
                      >
                        点検を実行
                      </button>
                    </div>
                    {lintResult ? (
                      lintResult.issues.length ? (
                        lintResult.issues.map((i: any, n: number) => (
                          <p key={n}>
                            <button
                              className="inline-link"
                              onClick={() => openPage(i.page_id)}
                            >
                              {name(i.page_id)}
                            </button>
                            ：{i.message}
                          </p>
                        ))
                      ) : (
                        <p>点検項目は0件です。</p>
                      )
                    ) : (
                      <p className="description">
                        矛盾、古い情報、関連ページを確認します。
                      </p>
                    )}
                  </section>
                  <section className="panel">
                    <div className="section-heading">
                      <h2>処理履歴</h2>
                      <button
                        onClick={() =>
                          safe(async () => setJobs(await api("/jobs")))
                        }
                      >
                        履歴を更新
                      </button>
                    </div>
                    {jobs.map((j) => (
                      <div className="job-row" key={j.id}>
                        <Badge value={j.status} />
                        <div>
                          <strong>{labels[j.kind] || j.kind}</strong>
                          <small>
                            {fmt(j.created)} · {j.stage}
                          </small>
                          {j.error && <p className="error-text">{j.error}</p>}
                        </div>
                        {j.status === "failed" && (
                          <button
                            disabled={busy}
                            onClick={() =>
                              safe(async () =>
                                watch(
                                  await api("/jobs/" + j.id + "/retry", {}),
                                ),
                              )
                            }
                          >
                            再実行
                          </button>
                        )}
                      </div>
                    ))}
                  </section>
                </>
              )}
            </>
          )}
          <footer className="main-footer">
            Our Context
            <span>参照版 {boot?.revision?.slice(0, 8) || "準備中"}</span>
          </footer>
        </main>
      </div>
      {noteOpen && (
        <Modal title="記録を追加" onClose={() => setNoteOpen(false)}>
          <form onSubmit={submitNote}>
            <label>
              見出し
              <input
                value={noteTitle}
                onChange={(e) => setNoteTitle(e.target.value)}
                required
              />
            </label>
            <label>
              本文
              <textarea
                rows={12}
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="日付、作業、会議での判断、困りごと、所感などを記入"
                required
              />
            </label>
            <div className="actions">
              <button className="primary" disabled={busy}>
                保存して Wiki に反映
              </button>
              <button type="button" onClick={() => setNoteOpen(false)}>
                閉じる
              </button>
            </div>
          </form>
        </Modal>
      )}
      {source && (
        <Modal title={source.title} onClose={() => setSource(null)}>
          <div className="muted">
            原資料 {source.id} · {fmt(source.created)}
          </div>
          <pre className="source-text">{source.body}</pre>
          <details>
            <summary>取得情報</summary>
            <pre>{JSON.stringify(source.metadata, null, 2)}</pre>
          </details>
        </Modal>
      )}
      {taskEdit && (
        <Modal
          title={"タスクを編集：" + taskEdit.title}
          onClose={() => setTaskEdit(null)}
        >
          <TaskForm
            task={taskEdit}
            people={people}
            onSubmit={(values) =>
              safe(async () => {
                const result = await api("/change-proposals", {
                  base_revision: boot.revision,
                  operations: [{ task_id: taskEdit.id, set: values }],
                  reason: "画面から予定を更新",
                });
                setTaskEdit(null);
                setProposal(result);
              })
            }
          />
        </Modal>
      )}
      {proposal && (
        <Modal title="変更案を確認" onClose={() => setProposal(null)}>
          {proposal.impacts?.map((i: any) => (
            <div className="proposal-item" key={i.task_id}>
              <strong>{i.title}</strong>
              {Object.entries(i.set).map(([key, value]) => (
                <p key={key}>
                  {(
                    {
                      plan_start: "予定開始",
                      plan_end: "予定終了",
                      deadline: "締め切り",
                      status: "状態",
                      assignee_ids: "担当",
                    } as any
                  )[key] || key}
                  ：
                  {Array.isArray(value)
                    ? value.map(name).join("、")
                    : labels[String(value)] || String(value || "未定")}
                </p>
              ))}
            </div>
          ))}
          {proposal.assumptions?.map((a: string) => (
            <p className="muted" key={a}>
              {a}
            </p>
          ))}
          {proposal.warnings?.map((w: string) => (
            <p className="error-text" key={w}>
              {w}
            </p>
          ))}
          <div className="actions">
            <button
              className="primary"
              disabled={busy}
              onClick={() =>
                safe(async () => {
                  await watch(
                    await api(
                      "/change-proposals/" + proposal.id + "/apply",
                      {},
                    ),
                  );
                  setProposal(null);
                })
              }
            >
              変更を適用
            </button>
            <button onClick={() => setProposal(null)}>閉じる</button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function Modal({ title, onClose, children }: any) {
  useEffect(() => {
    const fn = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", fn);
    return () => document.removeEventListener("keydown", fn);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <section
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <header>
          <h2>{title}</h2>
          <button onClick={onClose} aria-label="閉じる">
            ×
          </button>
        </header>
        {children}
      </section>
    </div>
  );
}
function QuestionCard({ question: q, evidence, onAnswer, onState, busy }: any) {
  const [answer, setAnswer] = useState("");
  const dateQuestion = ["deadline", "plan_start", "plan_end"].includes(q.data.field);
  return (
    <section className="panel question-card">
      <Badge value={q.status} />
      <h2>{q.data.question}</h2>
      <p>{q.data.reason}</p>
      {q.data.impact && <p className="muted">影響：{q.data.impact}</p>}
      {evidence}
      {["open", "answer_failed"].includes(q.status) && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            onAnswer(answer);
          }}
        >
          <label>
            {dateQuestion ? "回答の日付" : "回答"}
            {dateQuestion ? <input type="date" value={answer} onInput={(e) => setAnswer(e.currentTarget.value)} required /> : <textarea
              rows={3}
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
              required
            />}
          </label>
          <div className="actions">
            <button className="primary" disabled={busy}>
              回答を保存
            </button>
            <button type="button" onClick={() => onState("deferred")}>
              保留
            </button>
            <button type="button" onClick={() => onState("dismissed")}>
              不要
            </button>
          </div>
        </form>
      )}
      {["deferred", "dismissed"].includes(q.status) && (
        <button onClick={() => onState("open")}>回答待ちに戻す</button>
      )}
    </section>
  );
}
function TaskForm({ task, people, onSubmit }: any) {
  const [values, setValues] = useState({ ...task });
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit({
          plan_start: values.plan_start || null,
          plan_end: values.plan_end || null,
          deadline: values.deadline || null,
          status: values.status || "unknown",
          assignee_ids: values.assignee_ids || [],
        });
      }}
    >
      <div className="form-grid">
        {[
          ["plan_start", "予定開始"],
          ["plan_end", "予定終了"],
          ["deadline", "締め切り"],
        ].map(([key, label]) => (
          <label key={key}>
            {label}
            <input
              type="date"
              value={values[key] || ""}
              onInput={(e) => {
                const value = e.currentTarget.value;
                setValues((v) => ({ ...v, [key]: value }));
              }}
              onChange={(e) => {
                const value = e.currentTarget.value;
                setValues((v) => ({ ...v, [key]: value }));
              }}
            />
          </label>
        ))}
        <label>
          状態
          <select
            value={values.status || "unknown"}
            onChange={(e) => {
              const status = e.currentTarget.value;
              setValues((v) => ({ ...v, status }));
            }}
          >
            {[
              "unknown",
              "todo",
              "in_progress",
              "blocked",
              "done",
              "cancelled",
            ].map((s) => (
              <option key={s} value={s}>
                {labels[s]}
              </option>
            ))}
          </select>
        </label>
      </div>
      <fieldset>
        <legend>担当</legend>
        {people.map((p: any) => (
          <label className="check" key={p.id}>
            <input
              type="checkbox"
              checked={(values.assignee_ids || []).includes(p.id)}
              onChange={(e) => {
                const checked = e.currentTarget.checked;
                setValues((v) => ({
                  ...v,
                  assignee_ids: checked
                    ? [...(v.assignee_ids || []), p.id]
                    : (v.assignee_ids || []).filter(
                        (id: string) => id !== p.id,
                      ),
                }));
              }}
            />
            {p.title}
          </label>
        ))}
      </fieldset>
      <button className="primary">変更案を作成</button>
    </form>
  );
}
function Gantt({ tasks, onEdit }: any) {
  const dated = tasks.filter((t: any) => t.plan_start && t.plan_end),
    undated = tasks.filter((t: any) => !t.plan_start || !t.plan_end);
  if (!dated.length)
    return (
      <p className="empty">
        予定日を登録するとガントに表示されます。日程未定の作業は下の一覧で確認できます。
      </p>
    );
  const day = (s: string) => Date.parse(s + "T00:00:00Z") / 86400000;
  const start = Math.min(
      ...dated.map((t: any) => day(t.plan_start)),
      ...dated.filter((t: any) => t.deadline).map((t: any) => day(t.deadline)),
    ),
    fullEnd = Math.max(
      ...dated.map((t: any) => day(t.plan_end)),
      ...dated.filter((t: any) => t.deadline).map((t: any) => day(t.deadline)),
    );
  const days = Math.max(7, fullEnd - start + 2),
    left = 190,
    width = Math.max(1000, left + 20 + days * 24),
    unit = (width - left - 20) / days,
    height = 45 + dated.length * 44;
  return (
    <>
      <div className="gantt-scroll">
        <svg
          className="gantt"
          viewBox={`0 0 ${width} ${height}`}
          width={width}
          height={height}
          role="img"
          aria-label="タスクの予定と締め切り"
        >
          <defs>
            <marker
              id="arrow"
              markerWidth="6"
              markerHeight="6"
              refX="5"
              refY="3"
              orient="auto"
            >
              <path d="M0,0 L6,3 L0,6" fill="currentColor" />
            </marker>
          </defs>
          {Array.from({ length: days }, (_, i) => {
            const d = new Date((start + i) * 86400000);
            return (
              <g key={i}>
                {[0, 6].includes(d.getUTCDay()) && (
                  <rect
                    x={left + i * unit}
                    y={27}
                    width={unit}
                    height={height - 27}
                    className="weekend"
                  />
                )}
                <line
                  x1={left + i * unit}
                  x2={left + i * unit}
                  y1={27}
                  y2={height}
                  className="grid-line"
                />
                {i % Math.max(1, Math.ceil(days / 16)) === 0 && (
                  <text x={left + i * unit + 3} y={17}>
                    {d.getUTCMonth() + 1}/{d.getUTCDate()}
                  </text>
                )}
              </g>
            );
          })}
          {dated.map((t: any, i: number) => {
            const y = 40 + i * 44,
              x = left + Math.max(0, day(t.plan_start) - start) * unit,
              w = Math.max(
                3,
                Math.min(days, day(t.plan_end) - start + 1) * unit - (x - left),
              );
            return (
              <g
                key={t.id}
                onClick={() => onEdit(t)}
                role="button"
                tabIndex={0}
                aria-label={`${t.title} ${t.plan_start} から ${t.plan_end}`}
                onKeyDown={(e) => {
                  if (e.key === "Enter") onEdit(t);
                }}
              >
                <title>
                  {t.title}：{t.plan_start} ～ {t.plan_end}
                </title>
                <text x={4} y={y + 16} className="task-label">
                  {t.title.length > 16 ? t.title.slice(0, 16) + "…" : t.title}
                </text>
                <rect
                  x={x}
                  y={y}
                  width={w}
                  height={25}
                  rx={4}
                  className={"task-bar " + t.status}
                />
                {t.deadline && day(t.deadline) - start < days && (
                  <text
                    x={left + (day(t.deadline) - start + 0.5) * unit}
                    y={y + 19}
                    className="deadline"
                  >
                    ◆
                  </text>
                )}
                {(t.dependency_ids || []).map((id: string) => {
                  const j = dated.findIndex((d: any) => d.id === id);
                  if (j < 0) return null;
                  const from = dated[j],
                    fx = left + (day(from.plan_end) - start + 1) * unit,
                    fy = 52 + j * 44;
                  return (
                    <path
                      key={id}
                      d={`M${fx},${fy} H${fx + 7} V${y + 12} H${x - 2}`}
                      className="dependency"
                      markerEnd="url(#arrow)"
                    />
                  );
                })}
              </g>
            );
          })}
        </svg>
      </div>
      {undated.length > 0 && (
        <p className="muted">
          日程未定：{undated.map((t: any) => t.title).join("、")}
        </p>
      )}
      {fullEnd - start >= 90 && (
        <p className="muted">先頭から90日間を表示しています。</p>
      )}
    </>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
