/** V4 UI boundaries. Worker credentials are one-time mutation responses, never cached. */
export type AgentKind = "grok" | "muse" | "personal_agent" | "codex" | "cursor";
export type WakeMode = "manual" | "grok_routine" | "native_schedule" | "verified_hook";
export interface Agent {
  id: string; name: string; kind: AgentKind; owner_id: string; allowed_actor_ids: string[];
  shared_environment: boolean; capabilities: string[]; availability: "unknown" | "online" | "offline";
  secret_env: Record<string,string>; wake_mode: WakeMode; state?: "active" | "revoked"; last_seen_at?: number | null; revoked_at?: number | null;
}
export interface Connection {
  base_url?: string; space_id?: string; agent_id?: string; worker_url?: string; mcp_url?: string; openapi_url?: string;
  bridge_url?: string; openapi_download_path?: string; instructions?: string;
}
export interface AgentCreated { agent: Agent; worker_token: string; connection: Connection; }
export interface Artifact { id?: string; artifact_id?: string; name?: string; filename?: string; size?: number; sha256?: string; download_url?: string; }
export interface Result { text: string; artifacts: Artifact[]; outcome?: string; }
export interface Attempt { id: string; state: string; result?: Result | null; progress_text?: string; result_eligible?: boolean; }
export interface Step {
  id: string; agent_id: string; prompt: string; depends_on: string[]; acceptance: string; required: boolean; state: string;
  current_attempt?: string | Attempt | null; attempt?: Attempt | null; attempts?: Attempt[]; result?: Result | null;
  can_confirm_stopped?: boolean;
  agent_name?: string; agent_kind?: AgentKind; agent_owner_id?: string; shared_environment?: boolean;
}
export interface Question { id: string; step_id: string; attempt_id: string; text: string; choices: string[]; state: "open" | "answered" | "closed"; answer?: string; }
export interface Wake { id: string; wake_id?: string; agent_id: string; step_id: string; state: string; claimed_at?: number | null; can_confirm_stopped?: boolean; }
/** Where a task stands as a thread. `need` is what the owner has to do for it to move on, if anything. */
export type ThreadPhase = "needs_you" | "running" | "done" | "closed" | "archived";
export interface Thread {
  phase: ThreadPhase; label: string; need: "approve" | "answer" | "accept" | "check" | null;
  archived: boolean; snoozed_until: number | null; note: string | null;
}
export interface Task {
  id: string; title: string; actor_id: string; visibility: "private" | "space"; revision: number; plan_hash: string;
  state: string; steps: Step[]; questions: Question[]; created_at: number; updated_at: number;
  can_manage?: boolean; can_approve?: boolean; can_accept?: boolean;
  wakes?: Wake[]; thread?: Thread;
}
export interface TaskCreate {
  client_request_id: string; title: string; visibility: "private" | "space"; timeout_s: number;
  steps: Pick<Step,"id" | "agent_id" | "prompt" | "depends_on" | "acceptance" | "required">[];
}
export interface PlanApproval { revision: number; plan_hash: string; }
export interface ResultAcceptance extends PlanApproval { step_id: string; attempt_id: string; }
