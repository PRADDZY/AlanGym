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
  trace: string;
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
}
