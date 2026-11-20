# Browser adapters

The existing DOM interfaces stay in `src/personal_agent/channels/web/static/`. Strict TypeScript
owns the shared request lifecycle, Markdown token conversion and task form boundaries in this
directory. This is an incremental migration; the whole UI is not TypeScript or React.

## Develop and verify

Use Node 22.12+ and the project's Python development environment:

```sh
uv sync --frozen --group dev
npm ci
npm run build
npm run check
```

`npm run build` checks generated API types, typechecks the adapters and runs Vite. It needs only
Node and the checked-in schema. It writes `static/query.js`, `static/markdown-parser.js` and
`static/THIRD_PARTY_NOTICES.txt`; commit all three. Python serves the compiled JavaScript directly,
including from the wheel. Neither Node nor a CDN is needed when the service starts.

`npm run check` additionally exports the live Python contract into a temporary directory, compares
it with the committed OpenAPI JSON, checks generated TypeScript and both bundles byte for byte,
checks the licenses of the modules actually bundled, and runs all browser JavaScript tests. It
uses `PYTHON`, `.venv` or `python3`, in that order. It never downloads dependencies or opens agent
state. `npm test` runs only the JavaScript tests against the shipped assets.

After changing API models or the application version:

```sh
uv run --frozen python scripts/export_api_schema.py
npm run generate:api
npm run build
npm run check
```

`frontend/generated/api.d.ts` comes from openapi-typescript. `contracts.ts` uses those types for
the actual task, step and answer payload builders. The existing detailed task view models remain
in `static/workbench.d.ts`; this is not a claim that every legacy page has strict static checking.

## Request and privacy boundaries

`RequestScope` uses TanStack Query Core for concurrent GET deduplication, request state and
cancellation. Queries include the principal/space context and a session epoch. Their stale time
and unused cache retention are zero; subsequent reads always reach server authorization. There
is no persistence, offline replay, focus refetch or automatic retry. A caller-provided abort
signal uses an independent query so cancelling it cannot cancel another consumer.

Writes and binary downloads bypass the query/mutation cache. Writes are sent once with the
caller's explicit idempotency key, when the endpoint supports one. A lost response never proves
that a write did not happen. Finishing a write, including a failed response, cancels older reads
before the UI's next refresh. Downloads are revoked along with reads when a session or space
changes. The response body is checked against the epoch again before any value is returned.

401/403 clear private requests and synchronously notify the page. This clears rendered content
even if cancellation wins a parallel `Promise.all` race. The workbench can then recover to the
remaining personal space. The Service Worker includes the new public assets only and continues
to exclude `/api/` responses. Browser storage still holds only the display theme.

## Markdown boundary

markdown-it parses CommonMark tokens with raw HTML disabled. Its HTML renderer is never called.
The adapter emits a small allowlisted data tree, and the existing DOM renderer exclusively uses
DOM nodes and text content. It validates URLs again, allows only HTTP/HTTPS/mailto destinations,
and displays images as labels/links without fetching them.

Limits remain 60,000 characters per document, 20,000 characters per inline fragment, six DOM
nesting levels, 200 table body rows and 20 columns. Oversized or overly deep content falls back
to text. Reference links and entity decoding now use the standard parser; malformed nested link
syntax follows CommonMark and may produce separate autolinks, never nested anchors.

Pinned dependency versions are in `package.json`/`package-lock.json`. TypeScript 5.9.3 satisfies
openapi-typescript 7.13's declared TypeScript 5 peer range. Upgrade that combination together
when the generator supports a newer major; do not suppress peer dependency conflicts.
