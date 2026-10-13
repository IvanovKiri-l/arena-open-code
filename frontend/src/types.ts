// Типы данных API арены (совпадают с тем, что отдаёт бэкенд).

export type ResponseRecord = {
  id: string;
  slot: number;
  profile: string;
  temperature: number;
  output: string | null;
  latency_ms: number | null;
  tokens: number | null;
  error: string | null;
  status: string; // ok | error | pending | disqualified
};

export type VerdictScore = {
  id: string;
  score: number;
  reason: string;
};

export type VerdictRecord = {
  judge_index: number;
  winner_id: string | null;
  scores: VerdictScore[];
  rationale: string | null;
  created_at?: string | null;
};

export type JudgeResultPayload = {
  winner_id: string | null;
  verdicts: VerdictRecord[];
  errors: string[];
};

export type RunPayload = {
  id: string;
  created_at: string;
  prompt: string;
  options: Record<string, unknown>;
  status: string;
  winner_id: string | null;
  responses: ResponseRecord[];
  verdicts: VerdictRecord[];
};

export type RunSummary = {
  id: string;
  created_at: string;
  prompt: string;
  status: string;
  winner_id: string | null;
  responses_count: number;
};

export type Health = {
  status: string;
  service: string;
  model: string | null;
  llm_configured: boolean;
  arena_n: number;
  judges: number;
  verify_cmd_set: boolean;
};

export type StreamEvent =
  | { type: "chunk"; slot: number; text: string }
  | { type: "response"; data: ResponseRecord }
  | { type: "verifying" }
  | { type: "judging" }
  | { type: "verdict"; data: JudgeResultPayload }
  | { type: "error"; message: string }
  | { type: "end" };
