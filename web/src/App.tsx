import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import type { AnalysisJob, AnalysisReport, RunDetail, RunEvent, RunRecord, RunState } from "./types";

type ViewMode = "loading" | "live" | "replay" | "offline";
const ACTIVE_STATES = new Set<RunState>(["created", "mining", "validating", "baseline", "training", "evolving", "evaluating"]);

function App() {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [mode, setMode] = useState<ViewMode>("loading");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [analysisJob, setAnalysisJob] = useState<AnalysisJob | null>(null);
  const [analysisBusy, setAnalysisBusy] = useState(false);

  const refresh = useCallback(async (runId: string) => {
    const response = await fetch("/api/runs/" + encodeURIComponent(runId));
    if (!response.ok) throw new Error("Run request failed (" + response.status + ")");
    const next = (await response.json()) as RunDetail;
    setDetail(next);
    setMode("live");
    return next;
  }, []);

  useEffect(() => {
    let alive = true;
    const bootstrap = async () => {
      try {
        const response = await fetch("/api/runs");
        if (!response.ok) throw new Error("Local API unavailable");
        const result = (await response.json()) as { runs: RunRecord[] };
        if (!alive) return;
        if (result.runs.length > 0) await refresh(result.runs[0].id);
        else setMode("live");
      } catch {
        try {
          const response = await fetch(import.meta.env.BASE_URL + "data/demo-run.json");
          if (!response.ok) throw new Error("No replay is saved yet");
          const saved = (await response.json()) as RunDetail;
          if (alive) {
            setDetail(saved);
            setMode("replay");
          }
        } catch {
          if (alive) setMode("offline");
        }
      }
    };
    void bootstrap();
    return () => { alive = false; };
  }, [refresh]);

  useEffect(() => {
    if (mode !== "live" || !detail || !ACTIVE_STATES.has(detail.run.state)) return;
    const runId = detail.run.id;
    const latestSequence = detail.events.at(-1)?.sequence ?? 0;
    const stream = new EventSource("/api/runs/" + encodeURIComponent(runId) + "/events?after=" + latestSequence);
    stream.addEventListener("progress", (event) => {
      const received = JSON.parse((event as MessageEvent<string>).data) as RunEvent;
      setDetail((current) => {
        if (!current || current.run.id !== runId) return current;
        if (current.events.some((item) => item.sequence === received.sequence)) return current;
        return { ...current, events: [...current.events, received] };
      });
      if (["complete", "blocked", "failed", "cancelled"].includes(received.type)) {
        stream.close();
        void refresh(runId).catch(() => undefined);
      }
    });
    const poll = window.setInterval(() => { void refresh(runId).catch(() => undefined); }, 1_800);
    return () => {
      stream.close();
      window.clearInterval(poll);
    };
  }, [detail?.run.id, detail?.run.state, mode, refresh]);

  useEffect(() => {
    if (mode !== "live" || !detail || detail.run.state !== "complete") return;
    let alive = true;
    void fetch("/api/runs/" + encodeURIComponent(detail.run.id) + "/analyses")
      .then(async (response) => response.ok ? await response.json() as { analyses: AnalysisJob[] } : null)
      .then((body) => {
        if (!alive || !body) return;
        const latest = body.analyses.find((job) => job.kind === "ladder");
        if (latest) setAnalysisJob(latest);
      }).catch(() => undefined);
    return () => { alive = false; };
  }, [detail?.run.id, detail?.run.state, mode]);

  useEffect(() => {
    setAnalysisJob(null);
  }, [detail?.run.id]);

  useEffect(() => {
    if (!analysisJob || !["queued", "running"].includes(analysisJob.status)) return;
    const poll = window.setInterval(() => {
      void fetch("/api/analyses/" + encodeURIComponent(analysisJob.id))
        .then(async (response) => response.ok ? await response.json() as { analysis: AnalysisJob } : null)
        .then((body) => {
          if (!body) return;
          setAnalysisJob(body.analysis);
          if (["complete", "blocked", "failed", "cancelled"].includes(body.analysis.status)) {
            if (detail) void refresh(detail.run.id).catch(() => undefined);
          }
        }).catch(() => undefined);
    }, 1_200);
    return () => window.clearInterval(poll);
  }, [analysisJob, detail, refresh]);

  const handleStart = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setNotice("");
    try {
      const response = await fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo: "mahmoud/boltons", seed: 42 }),
      });
      const body = (await response.json()) as { run?: RunRecord; detail?: string };
      if (!response.ok || !body.run) throw new Error(body.detail ?? "Could not start this run.");
      await refresh(body.run.id);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Could not start this run.");
    } finally {
      setBusy(false);
    }
  };

  const handleCancel = async () => {
    if (!detail) return;
    setBusy(true);
    try {
      await fetch("/api/runs/" + encodeURIComponent(detail.run.id) + "/cancel", { method: "POST" });
      await refresh(detail.run.id);
    } catch {
      setNotice("The local API did not confirm cancellation.");
    } finally {
      setBusy(false);
    }
  };

  const handleRunLadder = async () => {
    if (!detail) return;
    setAnalysisBusy(true);
    setNotice("");
    try {
      const response = await fetch(`/api/runs/${encodeURIComponent(detail.run.id)}/analyses`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "ladder" }),
      });
      const body = await response.json() as { analysis?: AnalysisJob; detail?: string };
      if (!response.ok || !body.analysis) throw new Error(body.detail ?? "Could not start the model ladder.");
      setAnalysisJob(body.analysis);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Could not start the model ladder.");
    } finally {
      setAnalysisBusy(false);
    }
  };

  const isReplay = mode === "replay";
  const isActive = detail ? ACTIVE_STATES.has(detail.run.state) : false;
  const scoreEvents = useMemo(() => (detail?.events ?? []).filter((event) => event.type === "metric"), [detail?.events]);
  const trainingMetrics = scoreEvents.filter((event) => event.payload?.split === "training")
    .sort((left, right) => Number(left.payload?.round ?? 0) - Number(right.payload?.round ?? 0));
  const heldoutMetrics = scoreEvents.filter((event) => event.payload?.split === "heldout");
  const baselineHeldout = heldoutMetrics.find((event) => event.payload?.label === "baseline");
  const finalHeldout = heldoutMetrics.find((event) => event.payload?.label === "final");
  const sameHeldoutSkill = typeof baselineHeldout?.payload?.skill_version === "string"
    && baselineHeldout.payload.skill_version === finalHeldout?.payload?.skill_version;
  const lineage = detail?.analysis_report?.lineage ?? [];
  const noAcceptedCandidates = lineage.length > 0 && lineage.every((entry) => !entry.accepted);

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href={import.meta.env.BASE_URL} aria-label="AlanGym home">
          <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>
          <span>alan<span className="brand-gym">gym</span></span>
        </a>
        <div className="topbar-center">
          <span className="eyebrow">OPENFIRE · FIELD STUDY 001</span>
          <span className="topbar-title">Can a skill make a small model better?</span>
        </div>
        <div className={"connection " + mode} aria-live="polite">
          <span className="connection-dot" />
          {mode === "live" ? "LOCAL SESSION" : mode === "replay" ? "SAVED REPLAY" : mode === "loading" ? "CONNECTING" : "OFFLINE"}
        </div>
      </header>

      {detail ? (
        <>
          <section className="hero">
            <div className="hero-copy">
              <div className="hero-kicker"><span className="kicker-mark">01</span><span>THE EXPERIMENT</span></div>
              <h1>Skills should earn<br />their next line.</h1>
              <p>AlanGym turns merged pull requests into real repair tasks. Alan solves them, repository tests grade the work, and only training failures can shape the skill.</p>
              {sameHeldoutSkill && noAcceptedCandidates && <p className="result-context">No candidate skill was accepted. Both held-out evaluations used the same retained skill; the score change does not establish its cause.</p>}
              <div className="hero-foot"><span className="hero-rule" /><span>{detail.run.model}</span><i>·</i><span>two bounded rounds</span></div>
            </div>
            <div className="hero-chart">
              <div className="chart-head">
                <div><span className="chart-label">PASS RATE</span><span className="chart-caption">A measured change in task success</span></div>
                {detail.run.state === "complete"
                  ? <span className="chart-complete"><i />RUN COMPLETE</span>
                  : <span className={"chart-state " + detail.run.state}>{stateLabel(detail.run.state)}</span>}
              </div>
              <PassRateChart training={trainingMetrics} heldout={heldoutMetrics} />
              <div className="chart-legend">
                <span><i className="legend-training" />training · {detail.run.training_count} tasks</span>
                <span><i className="legend-heldout" />held-out · {detail.run.heldout_count} tasks</span>
                <span className="chart-note">held-out tested at start and finish</span>
              </div>
            </div>
          </section>

          <section className="run-strip">
            <div className="run-identity">
              <span className="section-index">RUN / {detail.run.id.slice(0, 8).toUpperCase()}</span>
              <a className="repo-link" href={"https://github.com/" + detail.run.repo} target="_blank" rel="noreferrer">
                <GitHubIcon />{detail.run.repo}<ArrowIcon />
              </a>
              <span className="run-meta">started {formatDate(detail.run.created_at)} <b>·</b> seed {detail.run.seed}</span>
            </div>
            <div className="run-measures">
              <ScoreReadout label="TRAINING" metric={trainingMetrics.at(-1)} planned={detail.run.training_count} />
              <span className="measure-divider" />
              <ScoreReadout label="HELD-OUT" metric={heldoutMetrics.find((event) => event.payload?.label === "final") ?? heldoutMetrics[0]} planned={detail.run.heldout_count} />
              <span className="measure-divider" />
              <div className="measure"><span className="measure-label">SKILL VERSION</span><strong className="measure-version">{detail.skills.at(-1)?.version ?? "pending"}</strong></div>
            </div>
            {isActive && !isReplay && <button className="cancel-button" onClick={handleCancel} disabled={busy}>{busy ? "WAIT…" : "CANCEL RUN"}</button>}
          </section>

          <div className="content-grid">
            <section className="task-section panel">
              <div className="panel-heading">
                <div><span className="section-index">02 / TASK SET</span><h2>Real work, held to its tests.</h2></div>
                <span className="task-count">{detail.tasks.length ? detail.tasks.length + " VERIFIED" : "MINING…"}</span>
              </div>
              {detail.tasks.length ? (
                <div className="task-list">
                  {detail.tasks.map((task, index) => (
                    <TaskRow key={task.id} task={task} trial={latestTrial(detail.trials, task.pr_number, detail.skills.at(-1)?.version)} index={index} repo={detail.run.repo} />
                  ))}
                </div>
              ) : (
                <div className="empty-tasks">
                  <span className="empty-glyph">✳</span>
                  <p>{detail.run.state === "blocked" ? "The task set could not be verified." : "The miner is checking merged PRs against their parent commits."}</p>
                  <span>Nothing enters the run until its tests fail before and pass after the fix.</span>
                </div>
              )}
            </section>
            <aside className="right-column">
              <section className="skill-panel panel">
                <div className="panel-heading compact">
                  <div><span className="section-index">03 / SKILL EVOLUTION</span><h2>Earned changes</h2></div>
                  <span className="version-count">{String(detail.skills.length).padStart(2, "0")} VERSIONS</span>
                </div>
                <SkillDiff detail={detail} />
              </section>
              <section className="activity-panel panel">
                <div className="panel-heading compact">
                  <div><span className="section-index">04 / FIELD NOTES</span><h2>Run log</h2></div>
                  <span className="live-indicator"><i />{isReplay ? "ARCHIVE" : "LIVE"}</span>
                </div>
                <Activity events={detail.events} />
              </section>
            </aside>
          </div>
          {detail.analysis_report && <EvidenceLab report={detail.analysis_report} detail={detail} isReplay={isReplay} analysisJob={analysisJob} analysisBusy={analysisBusy} notice={notice} onRunLadder={handleRunLadder} />}
          <footer className="footer"><span>ALANGYM <b>·</b> AN OPENFIRE EXPERIMENT</span><span>Scores come from repository tests. Unknown costs stay unreported.</span></footer>
        </>
      ) : (
        <EmptyState mode={mode} busy={busy} notice={notice} onStart={handleStart} />
      )}
    </main>
  );
}

function EmptyState({
  mode, busy, notice, onStart,
}: {
  mode: ViewMode;
  busy: boolean;
  notice: string;
  onStart: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <>
      <section className="empty-hero">
        <div className="empty-art" aria-hidden="true">
          <span className="art-ring ring-one" /><span className="art-ring ring-two" /><span className="art-ring ring-three" />
          <span className="art-cross">+</span><span className="art-dot dot-one" /><span className="art-dot dot-two" />
          <span className="art-spark">✳</span><span className="art-word">skill<br />≠<br />guess</span>
        </div>
        <div className="empty-copy">
          <div className="hero-kicker"><span className="kicker-mark">01</span><span>THE EXPERIMENT</span></div>
          <h1>Skills should earn<br />their next line.</h1>
          <p>A real pull request becomes a repair task. Alan takes the first pass, repository tests keep score, and training failures decide what the skill learns.</p>
          <form className="start-form" onSubmit={onStart}>
            <label htmlFor="repo">DEMO REPOSITORY</label>
            <div className="repo-input-wrap">
              <GitHubIcon />
              <input id="repo" value="mahmoud/boltons" readOnly aria-readonly="true" disabled={mode !== "live" || busy} />
              <button type="submit" disabled={mode !== "live" || busy}>{busy ? "STARTING…" : "START RUN"}<ArrowIcon /></button>
            </div>
            <div className="form-foot">
              <span>14 planned tasks · 6 train · 8 held out · 2 rounds</span>
              {mode === "live" && <span className="api-ready"><i />LOCAL API READY</span>}
            </div>
            {notice && <p className="form-notice" role="alert">{notice}</p>}
          </form>
          {mode === "offline" && <div className="offline-note"><span className="connection-dot" />Start alangym serve to run locally. A public replay appears after a completed run is reviewed and published.</div>}
        </div>
      </section>
      <section className="method-band">
        <div><span>01</span><strong>Mine</strong><small>verified merged PRs</small></div>
        <div><span>02</span><strong>Measure</strong><small>real regression tests</small></div>
        <div><span>03</span><strong>Evolve</strong><small>training failures only</small></div>
        <div><span>04</span><strong>Hold out</strong><small>final check stays unseen</small></div>
      </section>
      <footer className="footer"><span>ALANGYM <b>·</b> AN OPENFIRE EXPERIMENT</span><span>One model. One skill. A test suite that decides.</span></footer>
    </>
  );
}

type EvidenceTab = "heatmap" | "xray" | "ladder" | "lineage" | "battle";

function EvidenceLab({
  report, detail, isReplay, analysisJob, analysisBusy, notice, onRunLadder,
}: {
  report: AnalysisReport;
  detail: RunDetail;
  isReplay: boolean;
  analysisJob: AnalysisJob | null;
  analysisBusy: boolean;
  notice: string;
  onRunLadder: () => void;
}) {
  const [tab, setTab] = useState<EvidenceTab>("heatmap");
  const phases = report.heatmap.phases;
  const [phaseIndex, setPhaseIndex] = useState(Math.max(0, phases.length - 1));
  const phase = phases[phaseIndex] ?? "baseline";
  const tabs: Array<[EvidenceTab, string]> = [
    ["heatmap", "TASK SCRUBBER"],
    ["xray", "X-RAY"],
    ["ladder", "MODEL LADDER"],
    ["lineage", "SKILL LINEAGE"],
    ["battle", "BATTLE / FORK"],
  ];

  return (
    <section className="evidence-lab panel">
      <div className="evidence-heading">
        <div><span className="section-index">05 / EVIDENCE LAB</span><h2>Every claim points to saved evidence.</h2></div>
        <span className="evidence-run">{report.coverage.task_count} TASKS Â· {report.coverage.trial_count} TRIALS</span>
      </div>
      <div className="evidence-tabs" role="tablist" aria-label="Experiment evidence">
        {tabs.map(([key, label]) => (
          <button key={key} role="tab" aria-selected={tab === key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}>{label}</button>
        ))}
      </div>
      <div className="evidence-content">
        {tab === "heatmap" && (
          <>
            <div className="scrubber-head">
              <div><span className="evidence-label">SELECTED PHASE</span><strong>{phaseLabel(phase)}</strong></div>
              {phases.length > 1 && <label className="scrubber-control" htmlFor="phase-scrubber">
                <span>{String(phaseIndex + 1).padStart(2, "0")} / {String(phases.length).padStart(2, "0")}</span>
                <input id="phase-scrubber" type="range" min={0} max={phases.length - 1} value={phaseIndex} onChange={(event) => setPhaseIndex(Number(event.target.value))} />
              </label>}
            </div>
            <div className="heatmap-grid" role="table" aria-label="Task outcomes by run phase">
              <div className="heatmap-row heatmap-header" role="row"><span role="columnheader">TASK</span><span role="columnheader">SPLIT</span><span role="columnheader">OUTCOME</span></div>
              {report.heatmap.tasks.map((task) => {
                const passed = task.outcomes[phase];
                return <div className="heatmap-row" role="row" key={task.task_id}>
                  <span className="heatmap-task" role="cell">PR #{task.pr_number}</span>
                  <span className={"split-tag " + (task.split === "train" ? "train" : task.heldout_cohort === "retained_validation" ? "validation" : "fresh")} role="cell">{task.split === "train" ? "TRAIN" : task.heldout_cohort === "retained_validation" ? "VALIDATION" : "NEW HOLDOUT"}</span>
                  <span className={"heat-cell " + (passed === true ? "pass" : passed === false ? "fail" : "unknown")} role="cell">{passed === true ? "PASS" : passed === false ? "FAIL" : "NO RESULT"}</span>
                </div>;
              })}
              {report.heatmap.tasks.length === 0 && <div className="evidence-empty">Tasks appear after the benchmark split is saved.</div>}
            </div>
            <p className="evidence-note">Scrub phases to inspect the saved result for each task. Hatched or missing results are never counted as passes.</p>
          </>
        )}
        {tab === "xray" && <TaskXray report={report} />}
        {tab === "ladder" && <ModelLadder report={report} isReplay={isReplay} canRun={detail.run.state === "complete" && !isReplay} job={analysisJob} busy={analysisBusy} notice={notice} onRun={onRunLadder} />}
        {tab === "lineage" && <SkillLineage report={report} />}
        {tab === "battle" && <BattleFork detail={detail} report={report} />}
      </div>
    </section>
  );
}

function TaskXray({ report }: { report: AnalysisReport }) {
  const xray = report.xray;
  return <div className="feature-view">
    <div className="feature-intro"><span className="evidence-label">{xray.mode === "task_level" ? "TASK-LEVEL COMPARISON" : "STEP-LEVEL COUNTERFACTUAL"}</span>
      <strong>{xray.status === "measured" ? `${xray.changed_tasks ?? 0} / ${xray.total_tasks ?? 0} task outcomes changed` : "No paired X-ray evidence"}</strong>
      <p>{xray.reason ?? "Exact next-action probes are not saved for this run."}</p>
    </div>
    {xray.outcomes?.length ? <div className="xray-list">{xray.outcomes.map((item) => (
      <div className="xray-row" key={item.task_id}><span>{item.task_id.toUpperCase()}</span><span className={item.baseline_passed ? "pass-text" : "fail-text"}>{(xray.left_label ?? "BASELINE").toUpperCase()} {item.baseline_passed ? "PASS" : "FAIL"}</span><ArrowIcon /><span className={item.final_passed ? "pass-text" : "fail-text"}>{(xray.right_label ?? "FINAL").toUpperCase()} {item.final_passed ? "PASS" : "FAIL"}</span><b>{item.changed ? "CHANGED" : "SAME"}</b></div>
    ))}</div> : <div className="evidence-empty">Exact per-step probes need saved message prefixes. This report does not guess which skill line caused a task result.</div>}
  </div>;
}

function ModelLadder({ report, isReplay, canRun, job, busy, notice, onRun }: {
  report: AnalysisReport;
  isReplay: boolean;
  canRun: boolean;
  job: AnalysisJob | null;
  busy: boolean;
  notice: string;
  onRun: () => void;
}) {
  const active = job && ["queued", "running"].includes(job.status);
  return <div className="feature-view">
    <div className="ladder-actions">
      <div><span className="evidence-label">THREE CONFIGURATIONS · {report.benchmark.heldout_count * 3} TASK RUNS</span><p>Starts Alan using the configured Flash and V4 Pro models. It never purchases credits; dollar costs remain hidden unless verified.</p></div>
      {!isReplay && <button onClick={onRun} disabled={busy || Boolean(active) || !canRun}>{busy ? "STARTING…" : active ? "LADDER RUNNING" : "RUN MODEL LADDER"}</button>}
    </div>
    {job && <div className={`analysis-progress ${job.status}`}><span>{job.status.toUpperCase()}</span><p>{job.message}</p>{active && <progress value={job.progress} max={1} />}</div>}
    {notice && <p className="form-notice" role="alert">{notice}</p>}
    <div className="ladder-grid">{report.ladder.configs.map((config) => (
    <article className="ladder-card" key={config.id}>
      <div className="ladder-status"><span className={"status-dot " + config.status} />{config.status.toUpperCase()}</div>
      <h3>{config.label}</h3><code>{config.model}</code>
      <strong className="ladder-score">{config.passed === null ? "—" : config.passed} <small>/ {config.total}</small></strong>
      <p>{config.reason ?? (config.rate === null ? "Some trials have no pass result." : `${Math.round(config.rate * 100)}% held-out pass rate`)}</p>
      <div className="ladder-meta">
        <span>TOKENS <b>{formatTokenPair(config.tokens_input, config.tokens_output)}</b></span>
        <span>COST <b>{config.cost_usd === null ? "unverified" : `$${config.cost_usd.toFixed(4)}`}</b></span>
      </div>
    </article>
    ))}</div>
  </div>;
}

function SkillLineage({ report }: { report: AnalysisReport }) {
  const provenance = report.provenance;
  return <div className="lineage-view">
    <div className="feature-intro"><span className="evidence-label">VERSION PROVENANCE · {provenance.status.replaceAll("_", " ").toUpperCase()}</span>
      <strong>{provenance.version ?? "No skill version saved"}</strong>
      <p>{provenance.source_pr_numbers.length ? `Version-level training sources: ${provenance.source_pr_numbers.map((number) => `#${number}`).join(", ")}. Individual line origins remain unattributed.` : "No accepted training edit has source PR attribution."}</p>
    </div>
    {provenance.lines.length > 0 && <pre className="skill-lines">{provenance.lines.map((line) => <span key={line.line}><i>{String(line.line).padStart(2, "0")}</i><code>{line.text || " "}</code><b>{line.origin === "initial" ? "STARTER" : "UNATTRIBUTED"}</b></span>)}</pre>}
    <div className="lineage-list">{report.lineage.map((node, index) => (
      <article className={"lineage-node " + (node.accepted ? "accepted" : "rejected")} key={`${node.version}-${index}`}>
        <span>{node.accepted ? "ACCEPTED" : "REJECTED"}</span><strong>{node.version ?? "candidate"}</strong>
        <small>{node.parent_version ? `from ${node.parent_version}` : "root version"} Â· {node.training_passes ?? "?"} training passes</small>
        {node.reason && <small>reason: {node.reason.replaceAll("_", " ")}</small>}
        {node.source_pr_numbers.length > 0 && <small>version sources {node.source_pr_numbers.map((number) => `#${number}`).join(", ")}</small>}
        {node.flipped_pr_numbers.length > 0 && <small>flipped {node.flipped_pr_numbers.map((number) => `#${number}`).join(", ")}{node.flaky_pr_numbers.length ? ` · flaky retry ${node.flaky_pr_numbers.map((number) => `#${number}`).join(", ")}` : ""}</small>}
      </article>
    ))}</div>
    {report.lineage.length === 0 && <div className="evidence-empty">The starter skill remains frozen until a candidate improves training without regressing prior passes.</div>}
  </div>;
}

function BattleFork({ detail, report }: { detail: RunDetail; report: AnalysisReport }) {
  const [selectedPr, setSelectedPr] = useState<number | null>(null);
  const pairs = useMemo(() => {
    const grouped = new Map<number, { without?: RunDetail["trials"][number]; with?: RunDetail["trials"][number] }>();
    for (const trial of detail.trials) {
      if (trial.config_id !== "flash_no_skill" && trial.config_id !== "flash_current_skill") continue;
      const pair = grouped.get(trial.pr_number) ?? {};
      if (trial.config_id === "flash_no_skill") pair.without = trial;
      else pair.with = trial;
      grouped.set(trial.pr_number, pair);
    }
    return [...grouped.entries()].filter((entry): entry is [number, { without: RunDetail["trials"][number]; with: RunDetail["trials"][number] }] => Boolean(entry[1].without && entry[1].with));
  }, [detail.trials]);
  const pair = pairs.find(([pr]) => pr === selectedPr)?.[1] ?? pairs[0]?.[1];
  const withoutSteps = pair?.without.steps ?? [];
  const withSteps = pair?.with.steps ?? [];
  const longest = Math.max(withoutSteps.length, withSteps.length);
  const divergence = Array.from({ length: longest }).findIndex((_, index) => {
    const left = withoutSteps[index], right = withSteps[index];
    return !left || !right || left.kind !== right.kind || left.tool !== right.tool;
  });
  return <div className="battle-view">
    <div className="feature-intro"><span className="evidence-label">CONTROLLED A / B</span>
      <strong>{pairs.length ? "Same model · same task · skill is the variable" : "No matched ladder pair saved"}</strong>
      <p>{pairs.length ? String(report.battle.reason ?? "Only action labels are replayed here; private prompts, arguments, and tool output remain local.") : String(report.battle.reason ?? "The battle replay becomes available when both Flash configurations have saved trials for the same held-out PR.")}</p>
    </div>
    {pairs.length > 0 && pair && <>
      <label className="battle-select">TASK <select value={selectedPr ?? pairs[0][0]} onChange={(event) => setSelectedPr(Number(event.target.value))}>{pairs.map(([pr]) => <option key={pr} value={pr}>PR #{pr}</option>)}</select></label>
      <div className="battle-panes">{[
        { title: "FLASH · NO SKILL", trial: pair.without, steps: withoutSteps },
        { title: "FLASH · CURRENT SKILL", trial: pair.with, steps: withSteps },
      ].map((pane) => <div className="battle-pane" key={pane.title}>
        <header><span>{pane.title}</span><b className={pane.trial.passed ? "pass-text" : "fail-text"}>{pane.trial.passed ? "PASS" : pane.trial.passed === false ? "FAIL" : "UNKNOWN"}</b></header>
        <ol>{Array.from({ length: Math.max(1, longest) }, (_, index) => {
          const step = pane.steps[index];
          return <li className={index === divergence ? "divergence" : ""} key={index}><i>{String(index + 1).padStart(2, "0")}</i><code>{step ? step.kind === "tool" ? step.tool ?? "tool" : "assistant response" : "no recorded step"}</code><b>{index === divergence ? "FIRST VISIBLE CHANGE" : ""}</b></li>;
        })}</ol>
      </div>)}</div>
      {divergence >= 0 && <p className="evidence-note">First visible difference is step {divergence + 1}. Tool labels are not proof of a specific skill line causing the outcome.</p>}
    </>}
    <div className="fork-status"><span className="evidence-label">TIME-TRAVEL FORK · {String(report.fork.status ?? "unavailable").toUpperCase()}</span><p>{String(report.fork.reason ?? "Exact conversation and workspace restoration evidence is not saved; no fork is offered.")}</p></div>
  </div>;
}

function phaseLabel(phase: string) {
  return phase.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatTokenPair(input: number | null, output: number | null) {
  if (input === null || output === null) return "unavailable";
  return `${(input + output).toLocaleString()} (${input.toLocaleString()} in · ${output.toLocaleString()} out)`;
}

function PassRateChart({ training, heldout }: { training: RunEvent[]; heldout: RunEvent[] }) {
  const left = 48, right = 746, top = 22, bottom = 224;
  const x = (round: number) => left + (round / 2) * (right - left);
  const y = (rate: number) => bottom - Math.max(0, Math.min(1, rate)) * (bottom - top);
  const trainingPoints = training.map((event) => {
    const round = Number(event.payload?.round ?? 0), rate = Number(event.payload?.rate ?? 0);
    return { x: x(round), y: y(rate), label: Math.round(rate * 100) + "%", round };
  });
  const heldoutPoints = heldout.map((event) => {
    const round = event.payload?.label === "baseline" ? 0 : 2, rate = Number(event.payload?.rate ?? 0);
    return { x: x(round), y: y(rate), label: Math.round(rate * 100) + "%", round };
  });
  const trainingPath = trainingPoints.map((point, index) => (index ? "L" : "M") + point.x + "," + point.y).join(" ");
  const hasData = trainingPoints.length > 0 || heldoutPoints.length > 0;
  return (
    <div className="chart-wrap">
      <svg viewBox="0 0 780 270" role="img" aria-label="Training and held-out pass rates by round">
        {[0, 0.25, 0.5, 0.75, 1].map((value) => (
          <g key={value}><line x1={left} x2={right} y1={y(value)} y2={y(value)} className="chart-gridline" /><text x="0" y={y(value) + 4} className="chart-axis">{Math.round(value * 100)}%</text></g>
        ))}
        {[0, 1, 2].map((round) => (
          <g key={round}><line x1={x(round)} x2={x(round)} y1={top} y2={bottom} className="chart-vline" /><text x={x(round)} y="252" textAnchor="middle" className="chart-xaxis">{round === 0 ? "BASELINE" : "ROUND " + round}</text></g>
        ))}
        {hasData && trainingPath && <path d={trainingPath} className="training-path" />}
        {trainingPoints.map((point, index) => (
          <g key={"training-" + index}><circle cx={point.x} cy={point.y} r="5.5" className="training-point" /><text x={point.x + 10} y={point.y - 11} className="training-value">{point.label}</text></g>
        ))}
        {heldoutPoints.map((point, index) => (
          <g key={"heldout-" + index}><circle cx={point.x} cy={point.y} r="5.5" className="heldout-point" /><text x={point.x + 10} y={point.y + 19} className="heldout-value">{point.label}</text></g>
        ))}
        {!hasData && <g className="chart-empty"><line x1={left} x2={right} y1={bottom} y2={bottom} className="chart-empty-line" /><text x={(left + right) / 2} y="124" textAnchor="middle">WAITING FOR MEASURED RESULTS</text></g>}
      </svg>
    </div>
  );
}

function ScoreReadout({ label, metric, planned }: { label: string; metric?: RunEvent; planned: number }) {
  const passed = metric?.payload?.passed;
  const round = metric?.payload?.round;
  const caption = metric?.payload?.label === "baseline" ? "baseline"
    : typeof round === "number" ? `round ${round}`
    : metric?.payload?.label === "final" ? "final" : metric ? "observed" : "awaiting run";
  return (
    <div className="measure">
      <span className="measure-label">{label}</span>
      <strong>{typeof passed === "number" ? passed + "/" + planned : "—/" + planned}</strong>
      <span className="measure-caption">{caption}</span>
    </div>
  );
}

function TaskRow({ task, trial, index, repo }: {
  task: RunDetail["tasks"][number];
  trial?: RunDetail["trials"][number];
  index: number;
  repo: string;
}) {
  const status = trial ? (trial.passed === true ? "passed" : trial.passed === false ? "failed" : "unknown") : "waiting";
  return (
    <div className="task-row">
      <span className="task-number">{String(index + 1).padStart(2, "0")}</span>
      <span className={"split-tag " + (task.split === "train" ? "train" : task.heldout_cohort === "retained_validation" ? "validation" : "fresh")}>{task.split === "train" ? "TRAIN" : task.heldout_cohort === "retained_validation" ? "VALIDATION" : "NEW HOLDOUT"}</span>
      <div className="task-copy">
        <a href={"https://github.com/" + repo + "/pull/" + task.pr_number} target="_blank" rel="noreferrer"><span>#{task.pr_number}</span> {task.title}<ArrowIcon /></a>
        <small>{task.test_paths.join(" · ")}{trial ? ` · ${phaseLabel(trial.phase)}` : ""}</small>
      </div>
      <span className={"task-result " + status}><i />{trial ? (trial.passed === true ? "PASS" : trial.passed === false ? "FAIL" : trial.status.toUpperCase()) : "QUEUED"}</span>
    </div>
  );
}

function SkillDiff({ detail }: { detail: RunDetail }) {
  const accepted = [...detail.events].reverse().find((event) => event.type === "skill_accepted");
  const diff = accepted?.payload?.diff;
  if (typeof diff === "string" && diff.length > 0) {
    return (
      <div className="diff-box">
        <div className="diff-head"><span>{String(accepted?.payload?.parent_version ?? "previous")}</span><ArrowIcon /><span>{String(accepted?.payload?.version ?? "current")}</span><b>+{Number(accepted?.payload?.training_passes ?? 0)} TRAIN PASS</b></div>
        <pre>{diff.split("\n").map((line, index) => <span key={index} className={line.startsWith("+") ? "diff-add" : line.startsWith("-") ? "diff-remove" : ""}>{line || " "}</span>)}</pre>
      </div>
    );
  }
  const latest = detail.skills.at(-1);
  return (
    <div className="skill-empty">
      <span className="skill-symbol">✳</span>
      {latest ? <><strong>{latest.version}</strong><p>The starter skill is frozen. A new version appears only when training pass rate improves.</p></> : <><strong>Skill not loaded yet</strong><p>The first version is saved after the task set is verified.</p></>}
    </div>
  );
}

function Activity({ events }: { events: RunEvent[] }) {
  if (events.length === 0) return <div className="activity-empty">The run log will appear here as tasks move through the harness.</div>;
  return (
    <ol className="activity-list">
      {[...events].reverse().slice(0, 7).map((event) => (
        <li key={event.sequence}><span className="activity-marker" /><div><p>{event.message}</p><time>{formatTime(event.created_at)}</time></div></li>
      ))}
    </ol>
  );
}

function latestTrial(trials: RunDetail["trials"], prNumber: number, skillVersion?: string) {
  return [...trials].reverse().find((trial) =>
    trial.pr_number === prNumber
    && trial.phase !== "ladder"
    && !trial.phase.endsWith("_ratchet_retry")
    && (!skillVersion || trial.skill_version === skillVersion)
  );
}

function stateLabel(state: RunState) {
  return state.replaceAll("_", " ").toUpperCase();
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(value));
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" }).format(new Date(value));
}

function GitHubIcon() {
  return <svg className="github-icon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 .9a11.1 11.1 0 0 0-3.51 21.63c.56.1.76-.24.76-.54v-2.13c-3.1.67-3.76-1.32-3.76-1.32-.51-1.3-1.25-1.65-1.25-1.65-1.02-.7.08-.69.08-.69 1.13.08 1.73 1.16 1.73 1.16 1 .72 2.61.51 3.24.39.1-.72.39-1.22.7-1.5-2.48-.28-5.1-1.24-5.1-5.5 0-1.22.44-2.21 1.16-2.99-.12-.29-.5-1.42.11-2.96 0 0 .95-.3 3.05 1.14a10.6 10.6 0 0 1 5.56 0c2.1-1.43 3.04-1.14 3.04-1.14.61 1.54.23 2.67.12 2.96.72.78 1.15 1.77 1.15 2.99 0 4.27-2.62 5.22-5.11 5.5.4.35.75 1.02.75 2.06v3.05c0 .3.2.64.77.53A11.1 11.1 0 0 0 12 .9Z" /></svg>;
}

function ArrowIcon() {
  return <svg className="arrow-icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8h9M8 3l5 5-5 5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" /></svg>;
}

export default App;
