export type RunState =
  | "created"
  | "mining"
  | "validating"
  | "baseline"
  | "training"
  | "evolving"
  | "evaluating"
  | "complete"
  | "blocked"
  | "failed"
  | "cancelled";

export interface RunRecord {
  id: string;
  repo: string;
  state: RunState;
  model: string;
  seed: number;
  training_count: number;
  heldout_count: number;
  round_number: number;
  cost_usd: number | null;
  created_at: string;
  updated_at: string;
}

export interface TaskRecord {
  id: string;
  pr_number: number;
  title: string;
  body: string;
  merged_at: string;
  merge_sha: string;
  base_sha: string;
  source_paths: string[];
  test_paths: string[];
  fixture_paths: string[];
  split: "train" | "heldout";
  heldout_cohort?: "retained_validation" | "fresh" | null;
}

export interface TrialRecord {
  id: string;
  task_id: string;
  pr_number: number;
  phase: string;
  skill_version: string;
  status: string;
  passed: boolean | null;
  elapsed_seconds: number;
  cost_usd: number | null;
  model: string;
  trace?: { agent_status?: string; grader_output?: string; source_diff?: string };
  config_id?: string;
  skill_mode?: string;
  tokens_input?: number | null;
  tokens_output?: number | null;
  tokens_reasoning?: number | null;
  cost_verified?: boolean;
  steps?: TraceStep[];
}

export interface TraceStep {
  kind: string;
  role: string;
  tool?: string;
  tokens_input?: number | null;
  tokens_output?: number | null;
}

export interface AnalysisScore {
  passed: number;
  total: number;
  completed?: number;
  unknown?: number;
  rate: number | null;
}

export interface LadderConfig {
  id: string;
  label: string;
  model: string;
  skill_mode: string;
  status: string;
  reason: string | null;
  passed: number | null;
  total: number;
  rate: number | null;
  tokens_input: number | null;
  tokens_output: number | null;
  cost_usd: number | null;
  cost_per_pass: number | null;
  cost_verified?: boolean;
}

export interface AnalysisReport {
  schema_version: number;
  run_id: string;
  benchmark: { repo: string; seed: number; training_count: number; heldout_count: number };
  training: Record<string, AnalysisScore>;
  heldout: Record<string, AnalysisScore>;
  ladder: { configs: LadderConfig[]; task_ids: string[] };
  heatmap: {
    phases: string[];
    tasks: Array<{
      task_id: string;
      pr_number: number;
      split: string;
      heldout_cohort?: string | null;
      outcomes: Record<string, boolean | null>;
    }>;
  };
  lineage: Array<{
    type: string;
    message: string;
    version: string | null;
    parent_version: string | null;
    training_passes: number | null;
    current_passes: number | null;
    flipped_pr_numbers: number[];
    accepted: boolean;
    reason: string | null;
    flaky_pr_numbers: number[];
    source_pr_numbers: number[];
  }>;
  ratchet: { status: string; policy?: string; rejected_count?: number; flaky_pr_numbers?: number[]; reason?: string };
  provenance: {
    status: string;
    version: string | null;
    source_pr_numbers: number[];
    lines: Array<{ line: number; text: string; origin: string }>;
  };
  xray: {
    status: string;
    mode?: string;
    left_label?: string;
    right_label?: string;
    reason?: string;
    outcomes?: Array<{
      task_id: string;
      baseline_passed: boolean;
      final_passed: boolean;
      changed: boolean;
    }>;
    changed_tasks?: number;
    total_tasks?: number;
  };
  compiler: Record<string, unknown>;
  twins: Record<string, unknown>;
  battle: Record<string, unknown>;
  fork: Record<string, unknown>;
  coverage: { task_count: number; trial_count: number; costs_verified: boolean; token_counts_complete: boolean };
}

export interface AnalysisJob {
  id: string;
  run_id: string;
  kind: string;
  status: "queued" | "running" | "complete" | "blocked" | "failed" | "cancelled";
  progress: number;
  message: string;
  error: string | null;
  result: Record<string, unknown> | null;
}

export interface RunEvent {
  sequence: number;
  type: string;
  message: string;
  created_at: string;
  payload?: Record<string, unknown>;
  payload_json?: string;
}

export interface SkillVersion {
  version: string;
  parent_version: string | null;
  content: string;
  training_passes: number | null;
  created_at: string;
}

export interface RunDetail {
  run: RunRecord;
  tasks: TaskRecord[];
  trials: TrialRecord[];
  skills: SkillVersion[];
  events: RunEvent[];
  analysis_report?: AnalysisReport;
}
