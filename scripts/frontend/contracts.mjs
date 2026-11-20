// Check both contract transitions: runtime Pydantic/FastAPI -> OpenAPI -> TypeScript. The normal
// build uses only the checked-in schema; verification additionally needs the project's Python env.
import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { delimiter, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import openapiTS, { astToString, COMMENT_HEADER } from "openapi-typescript";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const schemaPath = join(root, "frontend/generated/openapi.json");
const typesPath = join(root, "frontend/generated/api.d.ts");
const check = process.argv.includes("--check");
if (check && !process.argv.includes("--types-only")) {
  const directory = await mkdtemp(join(tmpdir(), "pa-api-contract-"));
  try {
    const exported = join(directory, "openapi.json");
    const localPython = join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
    const python = process.env.PYTHON || (existsSync(localPython) ? localPython : "python3");
    await promisify(execFile)(python, ["scripts/export_api_schema.py", "--output", exported], {
      cwd: root,
      env: { ...process.env, PYTHONPATH: [join(root, "src"), process.env.PYTHONPATH].filter(Boolean).join(delimiter) },
      timeout: 30_000,
    });
    if (!(await readFile(exported)).equals(await readFile(schemaPath))) {
      throw new Error("OpenAPI is stale: run uv run python scripts/export_api_schema.py, then npm run generate:api.");
    }
  } finally { await rm(directory, { recursive: true, force: true }); }
}
const schema = JSON.parse(await readFile(schemaPath, "utf8"));
const contents = COMMENT_HEADER + astToString(await openapiTS(schema, { alphabetize: true }));
if (check) {
  if (contents !== await readFile(typesPath, "utf8")) {
    throw new Error("API types are stale: run npm run generate:api and commit the generated declaration.");
  }
} else await writeFile(typesPath, contents);
console.log(`${check ? "verified" : "generated"} API types from OpenAPI ${schema.info.version}`);
