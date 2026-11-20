/** Typed form-to-API boundaries compiled against the server's generated contract. */
import type { components } from "./generated/api";

type Schema = components["schemas"];
interface PlanFields { command: string; input_paths: string; output_paths: string; timeout_s: string; }
const paths = (value: string): string[] => value.split(/\r?\n/).map(path => path.trim()).filter(Boolean);
const plan = (fields: PlanFields) => ({
  command: fields.command,
  input_paths: paths(fields.input_paths),
  output_paths: paths(fields.output_paths),
  timeout_s: Number(fields.timeout_s),
});

export function buildTaskRequest(fields: PlanFields & { title: string }, requestId: string): Schema["CreateTask"] {
  return { ...plan(fields), title: fields.title.trim(), client_request_id: requestId };
}
export function buildStepRequest(fields: PlanFields, revision: number, stepId: string): Schema["AddStep"] {
  return { ...plan(fields), expected_revision: revision, step_id: stepId };
}
export function buildAnswerRequest(answer: string, revision: number): Schema["AnswerQuestion"] {
  return { answer, expected_revision: revision };
}
