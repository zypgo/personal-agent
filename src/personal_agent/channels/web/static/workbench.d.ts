/** V3 HTTP boundaries. Secrets are ephemeral responses, never persistent browser state. */
export type Role = "owner" | "member" | "viewer";
export interface Principal { id: string; display_name: string; is_admin: boolean; authz_version?: number; }
export interface Space { id: string; name: string; kind: "personal" | "project"; }
export interface IdentityResponse { principal: Principal; spaces: Space[]; space_id: string; role: Role; }
export interface Member { principal_id: string; display_name: string; role: Role; version: number; }
export interface OneTimeCode { code: string; expires_at: number; }
export interface Document { path: string; text: string; revision: number; }
export interface DocumentWrite { path: string; text: string; revision?: number; }
export interface DocumentVersion { id: number; action: string; actor: string; created_at: number; text: string; }
export interface Diagnostics { budget: {space_spent_usd: number; instance_remaining_usd: number; instance_limit_usd: number}; tasks: Record<string, number>; outbox: Record<string, number>; nodes: Node[]; cleanup_pending: number; }
export interface InputFile { path: string; size: number; sha256: string; }
export interface Plan {
  command: string;
  timeout_s: number;
  network: "none";
  input_manifest: InputFile[];
  output_paths: string[];
  workspace_revision: string;
  required_capabilities: string[];
}
export interface Artifact { id: string; name?: string; filename?: string; }
export interface Result { stdout?: string; stderr?: string; error?: string; exit_code?: number | null; artifacts?: Artifact[]; stale_revision?: boolean; }
export interface Job { id: string; task_id: string; task_revision: number; step_id: string; state: string; desired_state: "run" | "cancel"; approver_id: string; plan_hash: string; plan: Plan; current_attempt: string | null; result?: Result | null; }
export interface InputQuestion { id: string; task_id: string; task_revision: number; question: string; choices: string[]; state: "open" | "answered" | "cancelled"; answer: string | null; asked_by: string; answered_by: string | null; answered_at: number | null; }
export interface Task { id: string; actor_id: string; title: string; state: string; revision: number; created_at: number; updated_at: number; jobs?: Job[]; questions?: InputQuestion[]; }
export interface LogSnapshot { sequence: number; state: string; text: string; created_at: number; }
export interface StepWrite { expected_revision: number; step_id: string; command: string; input_paths: string[]; output_paths: string[]; timeout_s: number; }
export interface QuestionWrite { expected_revision: number; request_id: string; question: string; choices: string[]; }
export interface AnswerWrite { expected_revision: number; answer: string; }
export interface Review { space_id: string; title: string; job: Job; }
export interface TaskWrite { title: string; command: string; input_paths: string[]; output_paths: string[]; timeout_s: number; client_request_id: string; }
export interface Device { id: number; label: string; last_seen_at: number; agent?: string; }
export interface ChannelBinding { channel: string; account_id: string; sender_id: string; recipient_id: string; bound_at: number; version: number; active: boolean; }
export interface Node { id: string; label: string; last_seen_at: number | null; revoked_at: number | null; online?: boolean; }
export interface VaultStatus {
  enabled: boolean; exists?: boolean; root?: string | null; notes?: number; areas?: Record<string, number>;
  other?: number; skipped?: number; backlog?: number; issues?: Record<string, number>; max_note_bytes?: number;
}
export interface VaultFolder { name: string; count: number; }
export interface VaultEntry { path: string; title: string; summary?: string; type?: string; status?: string; updated?: number; outside?: boolean; private?: boolean; tags?: string[]; }
export interface VaultLink { path: string; title: string; }
export interface VaultNote {
  path: string; title: string; text: string; rev: string; type: string; status: string; origin: string; outside: boolean;
  generated: boolean; tags: string[]; aliases: string[]; outgoing: VaultLink[]; missing: string[]; backlinks: VaultLink[];
  updated: number; size: number; obsidian: string | null;
}
export interface VaultNoteWrite { path: string; text: string; mode: "create" | "replace" | "append"; rev?: string; }
export interface VaultVersion { id: number; action: string; actor: string; created_at: number; restorable: boolean; text: string; truncated: boolean; }
export interface VaultIssue { kind: string; path: string; detail: string; }
export interface ApiOptions { method?: "GET" | "POST"; body?: object; }

export interface ApiResponses {
  "/api/v3/me": IdentityResponse;
  "/api/v3/members": { members: Member[] };
  "/api/v3/tasks": { tasks: Task[] };
  "/api/v3/review-jobs": { pending: Review[] };
  "/api/v3/knowledge": { documents: Document[] };
  "/api/v3/devices": { devices: Device[] };
  "/api/v3/channel-bindings": { bindings: ChannelBinding[] };
  "/api/v3/nodes": { nodes: Node[] };
  "/api/v3/diagnostics": Diagnostics;
  "/api/v3/knowledge/history": { versions: DocumentVersion[] };
  "/api/v3/vault": VaultStatus;
  "/api/v3/vault/notes": { folder: string; query: string; folders: VaultFolder[]; notes: VaultEntry[] };
  "/api/v3/vault/note": { note: VaultNote; notice: string };
  "/api/v3/vault/history": { path: string; versions: VaultVersion[] };
  "/api/v3/vault/lint": { issues: VaultIssue[]; counts: Record<string, number> };
}
