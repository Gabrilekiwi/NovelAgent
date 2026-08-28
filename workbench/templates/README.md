# Workbench UI contract

The runnable product entry is `workbench/static/index.html`, a dependency-free static SPA served by the FastAPI application at `/`. It uses `/static/workbench.css`, `/static/workbench.js`, and `/static/workbench-spa.js`, then consumes the same-origin JSON API with the HttpOnly launch cookie. No Jinja2 installation is required.

The files under `workbench/templates/` are retained as server-rendered design references and component examples; they are not required by the runtime.

These templates are server-rendered Jinja2 views. They do not require a Node build or any external asset/CDN. Mount `workbench/static` at `/static` (or provide `static_prefix`) and load templates from `workbench/templates`.

## Suggested page routes

| Template | Route | Main context |
| --- | --- | --- |
| `home.html` | `GET /` | `projects[]` |
| `project_overview.html` | `GET /projects/{project_id}` | `project`, `health`, `recent_chapters[]`, `recent_runs[]` |
| `chapters.html` | `GET /projects/{project_id}/chapters` | `project`, `chapters[]` |
| `chapter_detail.html` | `GET /projects/{project_id}/chapters/{chapter}` | `project`, `chapter` |
| `outlines.html` | `GET /projects/{project_id}/outlines/{batch_id}` | `project`, `batch` |
| `production.html` | `GET /projects/{project_id}/production` | `project`, optional `session` |
| `pending.html` | `GET /projects/{project_id}/pending` | `project`, `pending_items[]`, optional `selected_pending` |
| `history.html` | `GET /projects/{project_id}/history` | `project`, `stats`, `runs[]` |
| `project_health.html` | `GET /projects/{project_id}/health` | `project`, `health` |

Every project view should provide `project.id`, `project.title`, `project.genre`, `project.language`, and a `project_url`. Shared optional values are `pending_count`, `provider_ready`, `provider_name`, `active_job`, `workbench_token`, and `csrf_token`. URL values such as `chapters_url` may override every conventional default in the templates.

The domain `ProjectRecord.to_dict()` currently exposes `name` and nested `metadata`; the route/view-model layer should map those to `title`, `genre`, `language`, and `summary`. It should not teach templates about persistence records.

## Mutation endpoints

Forms use the planned API surface:

- `POST /api/projects` and `POST /api/projects/import`
- `PUT /api/outline-batches/{id}` and `POST /api/outline-batches/{id}/approve`
- `POST /api/projects/{id}/sessions`
- `POST /api/jobs/{id}/pause`, `/cancel`, `/resume`, and `/recover`
- `PUT /api/drafts/{id}`, then `POST /api/drafts/{id}/validate` or `/confirm`
- `POST /api/chapters/{chapter}/amendments`

JSON forms send `X-Expected-Revision`, `X-Expected-SHA256`, and `If-Match` when CAS data is present. Return `409` for a stale revision/SHA and `423` for a held book lock. A successful response may provide `revision`, `sha`/`sha256`/`proposal_sha256`, `message`, and a same-origin `redirect_url`. FastAPI validation arrays are rendered as safe plain text.

For outline batches the view model should expose `id`, `status`, `revision`, `source_state_sha`, `source_fingerprint`, `start_chapter`, `end_chapter`, and exactly ten `items`. Each item uses `chapter_index`, `title`, `core_event`, `required_beats[]`, and `ending_pressure`. The domain service's `proposal_sha256` is the content CAS value; `source_state_sha` identifies the StoryProject state against which approval must be checked.

## Progress stream

`production.html` opens `session.events_url` with native `EventSource`; after three stream errors it polls `session.status_url?after={last_event_id}`. The SSE endpoint must authenticate by the same-site launch cookie because browser `EventSource` cannot add custom headers. It should honor `Last-Event-ID` and/or the initial `after` query parameter.

Events may use the default `message` event or one of `status`, `progress`, `stage`, `event`, `queue`, `metrics`, `attention`, `complete`. JSON fields consumed by the UI are:

```json
{
  "id": 42,
  "status": "generating",
  "stage": "generating",
  "stage_state": "current",
  "title": "场景 3 正在生成",
  "detail": "已复用两个可信场景",
  "progress_percent": 46,
  "chapter": 20,
  "chapter_status": "generating",
  "model_calls": 3,
  "token_count_label": "12.4k",
  "cost_label": "¥1.28"
}
```

Polling may return the same shape plus `events[]` and `last_event_id`. Terminal statuses stop further browser updates.

## Safety and accessibility

The JavaScript writes server messages only through `textContent`; it never inserts response HTML. Redirects are restricted to the current origin. The backend must still validate all paths, bind only to `127.0.0.1`, enforce launch-token/Host/Origin checks, whitelist subprocess arguments, and keep API keys out of responses and SQLite.
