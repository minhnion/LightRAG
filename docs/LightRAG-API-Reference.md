# LightRAG API Reference

Tài liệu này mô tả HTTP API hiện có của LightRAG Server. Contract được trình
bày theo code hiện tại, bao gồm ingest tài liệu/video, query RAG, knowledge
graph, workspace, multimodal assets và Ollama-compatible API.

## 1. Quy ước chung

### Base URL

```bash
export LIGHTRAG_URL="http://localhost:9621"
```

Nếu server chạy sau reverse proxy với `API_PREFIX`, thêm prefix đó vào mọi
đường dẫn. Ví dụ `API_PREFIX=/rag` thì `/query` được gọi tại `/rag/query`.

Các endpoint dữ liệu (`/documents`, `/query`, `/graph`, `/graphs`, `/assets` và
`/api`) đều hỗ trợ workspace qua header:

```http
X-LightRAG-Workspace-ID: <workspace-uuid>
```

Không gửi header này để dùng workspace mặc định. Các endpoint quản lý
workspace `/v1/workspaces/*` không dùng header này.

### Authentication

Khi bật API key, gửi:

```http
X-API-Key: <LIGHTRAG_API_KEY>
```

Khi bật tài khoản JWT, gửi:

```http
Authorization: Bearer <access_token>
```

Một số deployment có whitelist path. `/health`, `/auth-status` và `/login` có
hành vi đặc biệt được mô tả bên dưới. Không đưa API key/JWT vào frontend bundle
hoặc URL query string.

### Content type và lỗi

- JSON request: `Content-Type: application/json`.
- Upload file: `multipart/form-data`.
- Login: `application/x-www-form-urlencoded`.
- Ollama request: JSON hoặc `application/octet-stream` chứa JSON.
- Validation lỗi thông thường trả `422` với `{"detail": [...]}`.
- `/query/data` chuẩn hóa lỗi validation thành `400` với envelope riêng.

Các mã lỗi thường gặp:

| HTTP | Ý nghĩa |
| --- | --- |
| `400` | Body/parameter không hợp lệ hoặc thao tác không hợp lệ |
| `401` | Thiếu, sai hoặc hết hạn JWT |
| `403` | Thiếu/sai API key hoặc không đủ quyền |
| `404` | Không tìm thấy resource |
| `409` | Trùng tài liệu, pipeline bận hoặc workspace không thể thao tác |
| `413` | File upload vượt `MAX_UPLOAD_SIZE` |
| `422` | Pydantic validation error |
| `500` | Lỗi server hoặc dependency |

## 2. Service, authentication và documentation

### `GET /`

Redirect đến `/webui/` nếu frontend đã build; nếu không có WebUI thì redirect
đến `/docs`. Response là HTTP redirect, không có JSON contract cố định.

### `GET /auth-status`

Không yêu cầu authentication. Dùng để biết server đang mở hay bảo vệ.

Response khi authentication tắt:

```json
{
  "auth_configured": false,
  "access_token": "<guest-jwt>",
  "token_type": "bearer",
  "auth_mode": "disabled",
  "message": "Authentication is disabled. Using guest access.",
  "core_version": "...",
  "api_version": "...",
  "webui_title": "...",
  "webui_description": "..."
}
```

Khi authentication bật, response không chứa guest token:

```json
{
  "auth_configured": true,
  "auth_mode": "enabled",
  "core_version": "...",
  "api_version": "...",
  "webui_title": "...",
  "webui_description": "..."
}
```

### `POST /login`

`application/x-www-form-urlencoded`:

| Field | Bắt buộc | Mô tả |
| --- | --- | --- |
| `username` | Có | Tên tài khoản trong `AUTH_ACCOUNTS` |
| `password` | Có | Mật khẩu tài khoản |

```bash
curl -X POST "$LIGHTRAG_URL/login" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'username=<username>' \
  --data-urlencode 'password=<password>'
```

Response thành công:

```json
{
  "access_token": "<jwt>",
  "token_type": "bearer",
  "auth_mode": "enabled",
  "core_version": "...",
  "api_version": "...",
  "webui_title": "...",
  "webui_description": "..."
}
```

Khi auth tắt, endpoint trả guest token và không cần kiểm tra username/password.

### `GET /health`

Đây là liveness endpoint và luôn cố gắng trả `200`, phù hợp cho Docker/K8s
healthcheck. Request chưa authenticate chỉ nhận các trường không nhạy cảm:

```json
{
  "status": "healthy",
  "auth_mode": "enabled",
  "core_version": "...",
  "api_version": "...",
  "webui_available": true,
  "webui_title": "...",
  "webui_description": "...",
  "pipeline_busy": false,
  "pipeline_active": false
}
```

Request đã authenticate nhận thêm `working_directory`, `input_directory`,
`configuration`, queue status, lock status và các thông tin vận hành.

### Documentation endpoints

| Method | Endpoint | Output |
| --- | --- | --- |
| `GET` | `/docs` | Swagger UI offline |
| `GET` | `/redoc` | ReDoc |
| `GET` | `/openapi.json` | OpenAPI JSON machine-readable |
| `GET` | `/docs/oauth2-redirect` | OAuth2 redirect hỗ trợ Swagger |

## 3. Workspace management

### `POST /v1/workspaces`

Tạo workspace mới. Trả HTTP `201`.

Input JSON:

| Field | Kiểu | Bắt buộc | Mô tả |
| --- | --- | --- | --- |
| `display_name` | string | Có | Tên hiển thị, 1–255 ký tự |
| `description` | string/null | Không | Mô tả, tối đa 10.000 ký tự |

```json
{
  "display_name": "Finance 2026",
  "description": "Financial documents"
}
```

Output `WorkspaceResponse`:

```json
{
  "id": "7b2e7ef9-7a25-4f7d-9d4b-b3c0e15d4b47",
  "display_name": "Finance 2026",
  "description": "Financial documents",
  "status": "active",
  "is_default": false,
  "created_at": "2026-08-02T12:00:00+00:00",
  "updated_at": "2026-08-02T12:00:00+00:00",
  "deleted_at": null
}
```

`id` là UUID. `status`, timestamp và `deleted_at` do server quản lý.

### `GET /v1/workspaces`

Query parameter:

| Parameter | Mặc định | Mô tả |
| --- | --- | --- |
| `include_deleted` | `false` | Nếu `true`, gồm cả record đã bị xóa |

Output: mảng `WorkspaceResponse`.

### `GET /v1/workspaces/{workspace_id}`

`workspace_id` là UUID path parameter. Output: một `WorkspaceResponse`.

### `PATCH /v1/workspaces/{workspace_id}`

Phải gửi ít nhất một field:

| Field | Kiểu | Mô tả |
| --- | --- | --- |
| `display_name` | string/null | Tên mới, 1–255 ký tự nếu có giá trị |
| `description` | string/null | Mô tả mới; gửi `null` để xóa |

Output: `WorkspaceResponse` đã cập nhật.

### `DELETE /v1/workspaces/{workspace_id}`

Xóa workspace bất đồng bộ, trả HTTP `202`:

```json
{
  "status": "deleting",
  "workspace_id": "<uuid>",
  "job_id": "<uuid>",
  "message": "Workspace deletion has been scheduled."
}
```

`job_id` chỉ dùng để ghi log/đối soát; API hiện tại không có endpoint polling
riêng cho job này. Workspace mặc định hoặc workspace đang bận có thể trả `409`.

## 4. Document ingestion và pipeline

Tất cả endpoint trong mục này có prefix `/documents`. Ingestion chạy nền;
response thành công chỉ xác nhận đã enqueue. Dùng `track_id` để theo dõi.

### `POST /documents/upload`

`multipart/form-data`:

| Part | Kiểu | Bắt buộc | Mô tả |
| --- | --- | --- | --- |
| `file` | binary file | Có | File được lưu vào input directory của workspace |

Ví dụ upload video:

```bash
curl -X POST "$LIGHTRAG_URL/documents/upload" \
  -H "X-API-Key: $LIGHTRAG_API_KEY" \
  -H "X-LightRAG-Workspace-ID: $WORKSPACE_ID" \
  -F 'file=@./meeting.mp4'
```

Video `mp4`, `mov`, `mkv`, `webm`, `avi`, `m4v` được route sang video parser
khi `LIGHTRAG_PARSER` đã cấu hình `video-iteP`. Parser video dùng FFmpeg,
Qwen3-ASR, Qwen3.5 VLM và Qwen3-VL Embedding theo cấu hình deployment.

Output `InsertResponse`:

```json
{
  "status": "success",
  "message": "File 'meeting.mp4' uploaded successfully. Processing will continue in background.",
  "track_id": "upload_20260802_120000_abc123"
}
```

`status` là `success`, `partial_success` hoặc `failure`. Filename trùng trong
cùng workspace thường trả `409`; extension không hỗ trợ trả `400`; file quá
`MAX_UPLOAD_SIZE` trả `413`.

### `POST /documents/text`

Input JSON:

| Field | Kiểu | Bắt buộc | Mặc định/mô tả |
| --- | --- | --- | --- |
| `text` | string | Có | Nội dung, tối thiểu 1 ký tự sau trim |
| `file_source` | string/null | Không | Tên nguồn hiển thị/citation; thiếu dùng sentinel nội bộ |
| `chunking` | object/null | Không | Cấu hình chunking; thiếu dùng addon/env mặc định |

```json
{
  "text": "LightRAG stores knowledge as a graph.",
  "file_source": "manual-note.txt",
  "chunking": {
    "strategy": "recursive_character",
    "params": {
      "chunk_token_size": 1200,
      "chunk_overlap_token_size": 100,
      "separators": ["\n\n", "\n", ". ", " "]
    }
  }
}
```

### `POST /documents/texts`

Input JSON:

| Field | Kiểu | Bắt buộc | Mô tả |
| --- | --- | --- | --- |
| `texts` | string[] | Có | Mảng không rỗng; từng phần tử được trim |
| `file_sources` | string[]/null | Không | Nguồn tương ứng; nếu gửi phải khớp số lượng |
| `chunking` | object/null | Không | Một cấu hình dùng chung cho toàn bộ texts |

Output giống `/documents/text`: `InsertResponse`.

### Cấu hình `chunking`

`strategy` nhận một trong `fixed_token`, `recursive_character`,
`semantic_vector`, `paragraph_semantic`. `params` không nhận key lạ.

| Strategy | Các field hợp lệ trong `params` |
| --- | --- |
| `fixed_token` | `chunk_token_size` int >= 1; `chunk_overlap_token_size` int >= 0 và nhỏ hơn size; `split_by_character` string; `split_by_character_only` boolean |
| `recursive_character` | `chunk_token_size`; `chunk_overlap_token_size`; `separators` string[] |
| `semantic_vector` | `chunk_token_size`; `breakpoint_threshold_type` = `percentile`/`standard_deviation`/`interquartile`/`gradient`; `breakpoint_threshold_amount` > 0; `buffer_size` int >= 1; `sentence_split_regex` regex hợp lệ |
| `paragraph_semantic` | `chunk_token_size`; `chunk_overlap_token_size`; `drop_references` boolean |

Các field không gửi sẽ inherit từ addon params/environment. Request sai kiểu,
overlap không hợp lệ hoặc regex sai trả `422`.

### `POST /documents/scan`

Không có body. Quét input directory của workspace, tìm file mới có parser
phù hợp và enqueue xử lý nền.

Output `ScanResponse`:

```json
{
  "status": "scanning_started",
  "message": "Scanning process has been initiated in the background",
  "track_id": "scan_20260802_120000_abc123"
}
```

`status` có thể là `scanning_started` hoặc
`scanning_skipped_pipeline_busy`.

### `POST /documents/reprocess_failed`

Không có body. Enqueue lại các document `FAILED`, `PENDING` và các document
`PROCESSING` bị bỏ dở. Giữ nguyên `track_id` cũ.

Output:

```json
{
  "status": "reprocessing_started",
  "message": "Reprocessing of failed documents has been initiated in background. Documents retain their original track_id.",
  "track_id": ""
}
```

### `POST /documents/cancel_pipeline`

Không có body. Đặt cờ hủy pipeline; việc hủy là graceful.

```json
{
  "status": "cancellation_requested",
  "message": "Pipeline cancellation has been requested. Documents will be marked as FAILED."
}
```

Nếu pipeline không chạy, `status` là `not_busy`.

### `GET /documents/pipeline_status`

Không có input. Output `PipelineStatusResponse`:

| Field | Kiểu | Mô tả |
| --- | --- | --- |
| `autoscanned` | boolean | Auto-scan đã khởi động chưa |
| `busy` | boolean | Pipeline đang xử lý/giữ lock |
| `job_name` | string | Tên job hiện tại |
| `job_start` | string/null | Thời điểm bắt đầu ISO-8601 |
| `docs` | int | Tổng document trong job |
| `batchs` | int | Tổng số batch |
| `cur_batch` | int | Batch hiện tại |
| `request_pending` | boolean | Có request mới chờ pipeline đọc lại không |
| `latest_message` | string | Log message mới nhất |
| `history_messages` | string[]/null | Lịch sử message |
| `update_status` | object/null | Trạng thái update của namespace |

### `GET /documents/track_status/{track_id}`

Path parameter `track_id` là ID trả từ upload/text/scan. Output:

```json
{
  "track_id": "upload_...",
  "documents": [
    {
      "id": "doc-...",
      "content_summary": "First characters of content",
      "content_length": 1024,
      "status": "processed",
      "created_at": "2026-08-02T12:00:00+00:00",
      "updated_at": "2026-08-02T12:01:00+00:00",
      "track_id": "upload_...",
      "chunks_count": 4,
      "error_msg": null,
      "metadata": {},
      "file_path": "meeting.mp4"
    }
  ],
  "total_count": 1,
  "status_summary": {"processed": 1}
}
```

`status` hiện có: `pending`, `parsing`, `analyzing`, `processing`,
`preprocessed` (legacy), `processed`, `failed`.

### `GET /documents`

Endpoint legacy, không có input. Output nhóm document theo status:

```json
{
  "statuses": {
    "processed": ["<DocStatusResponse>", "..."]
  }
}
```

Tối đa khoảng 1.000 record; integration mới nên dùng `/documents/paginated`.

### `POST /documents/paginated`

Input JSON:

| Field | Kiểu | Mặc định | Mô tả |
| --- | --- | --- | --- |
| `status_filter` | DocStatus/null | null | Filter một status, legacy |
| `status_filters` | DocStatus[]/null | null | Filter nhiều status; ưu tiên hơn `status_filter` |
| `page` | int >= 1 | 1 | Trang 1-based |
| `page_size` | int 10–200 | 50 | Số record/trang |
| `sort_field` | enum | `updated_at` | `created_at`, `updated_at`, `id`, `file_path` |
| `sort_direction` | enum | `desc` | `asc` hoặc `desc` |

Output:

```json
{
  "documents": ["<DocStatusResponse>"],
  "pagination": {
    "page": 1,
    "page_size": 50,
    "total_count": 120,
    "total_pages": 3,
    "has_next": true,
    "has_prev": false
  },
  "status_counts": {"processed": 110, "failed": 10}
}
```

### `GET /documents/status_counts`

Output:

```json
{"status_counts": {"pending": 0, "parsing": 1, "processed": 10, "failed": 0}}
```

### `DELETE /documents/delete_document`

Xóa một hoặc nhiều document bất đồng bộ. Input JSON:

| Field | Kiểu | Mặc định | Mô tả |
| --- | --- | --- | --- |
| `doc_ids` | string[] | bắt buộc | Không rỗng, mỗi ID không rỗng và không trùng |
| `delete_file` | boolean | `false` | Xóa file trong input directory |
| `delete_llm_cache` | boolean | `false` | Xóa cache extraction của document |

```json
{
  "doc_ids": ["doc-123", "doc-456"],
  "delete_file": true,
  "delete_llm_cache": false
}
```

Output:

```json
{
  "status": "deletion_started",
  "message": "Document deletion for 2 documents has been initiated. Processing will continue in background.",
  "doc_id": "doc-123, doc-456"
}
```

`status` có thể là `deletion_started`, `busy` hoặc `not_allowed`.

### `DELETE /documents`

Không có body. Xóa toàn bộ documents, graph, vectors, chunks, status và file
trong workspace hiện tại. Output:

```json
{"status": "success", "message": "All documents cleared successfully. Deleted 15 files."}
```

`status` có thể là `success`, `partial_success`, `busy` hoặc `fail`. Đây là
thao tác destructive.

### `POST /documents/clear_cache`

Body JSON `{}` được giữ để tương thích; mọi cache LLM của workspace sẽ bị xóa,
không có tham số mode.

Output:

```json
{"status": "success", "message": "Successfully cleared all cache"}
```

## 5. Query và retrieval

Ba endpoint query dùng chung request schema dưới đây.

### Query request schema

| Field | Kiểu | Mặc định | Mô tả |
| --- | --- | --- | --- |
| `query` | string | bắt buộc | Câu hỏi, tối thiểu 3 ký tự |
| `mode` | enum | `mix` | `local`, `global`, `hybrid`, `naive`, `mix`, `bypass` |
| `only_need_context` | boolean/null | null | Chỉ trả context, không sinh câu trả lời |
| `only_need_prompt` | boolean/null | null | Chỉ trả prompt đã tạo |
| `response_type` | string/null | null | Dạng trả lời, ví dụ `Multiple Paragraphs` |
| `top_k` | int >= 1/null | null | Số entity/relation chính cần lấy |
| `chunk_top_k` | int >= 1/null | null | Số text chunk vector retrieval |
| `max_entity_tokens` | int >= 1/null | null | Budget token cho entity context |
| `max_relation_tokens` | int >= 1/null | null | Budget token cho relation context |
| `max_total_tokens` | int >= 1/null | null | Tổng budget context |
| `hl_keywords` | string[] | `[]` | High-level keywords; rỗng thì LLM tự tạo |
| `ll_keywords` | string[] | `[]` | Low-level keywords; rỗng thì LLM tự tạo |
| `conversation_history` | object[]/null | null | Message history chỉ gửi cho LLM, không dùng retrieval |
| `user_prompt` | string/null | null | Prompt tùy chỉnh thay prompt mặc định |
| `enable_rerank` | boolean/null | null | Bật reranking cho chunks |
| `include_references` | boolean/null | `true` | Thêm references cho `/query` và `/query/stream` |
| `include_chunk_content` | boolean/null | `false` | Thêm text chunks vào từng reference |
| `include_content_blocks` | boolean | `false` | Thêm markdown/image/table/equation/video blocks |
| `include_graph_context` | boolean | `false` | Thêm ID graph node/relation đã dùng |
| `stream` | boolean/null | tùy endpoint | `/query` ép false; `/query/stream` mặc định true |

`conversation_history` có dạng `[{"role":"user|assistant|system", "content":"..."}]`;
mỗi item phải có `role` là string không rỗng.

Query mode:

- `local`: entity và quan hệ trực tiếp.
- `global`: pattern/quan hệ ở phạm vi knowledge graph rộng.
- `hybrid`: kết hợp local và global.
- `naive`: vector chunks, không dùng graph.
- `mix`: kết hợp graph và vector, thường là mode khuyến nghị.
- `bypass`: gọi LLM trực tiếp, không retrieval.

### `POST /query`

Request: `application/json` theo Query request schema. Endpoint luôn trả JSON
không streaming, dù field `stream` có gửi thế nào.

Output `QueryResponse`:

```json
{
  "response": "Câu trả lời được sinh bởi LLM.",
  "references": [
    {
      "reference_id": "1",
      "file_path": "meeting.mp4",
      "document_id": "doc-...",
      "display_name": "meeting.mp4",
      "page": null,
      "content": ["...chunk text..."]
    }
  ],
  "content_blocks": [
    {
      "type": "video_scene",
      "asset_id": "asset-...",
      "caption": "Scene description",
      "media_type": "video_contact_sheet",
      "scene_id": "scene-0001",
      "start_ms": 0,
      "end_ms": 12000,
      "frame_timestamps_ms": [0, 4000, 8000, 12000],
      "manifest_ref": "meeting.video.manifest.json"
    }
  ],
  "graph_context": {"nodes": ["entity-id"], "edges": ["relation-id"]}
}
```

`references` có thể là `null` khi `include_references=false`. `content` chỉ
có khi `include_chunk_content=true`. `content_blocks` và `graph_context` chỉ
có khi bật flag tương ứng. Các content block thông thường có thể có `type`
là `markdown`, `image`, `table`, `equation`; video dùng `video_scene` với
timecode milliseconds như ví dụ trên.

### `POST /query/stream`

Request giống `/query`. Output `Content-Type: application/x-ndjson`; mỗi dòng
là một JSON object độc lập:

```text
{"references":[...]}
{"content_blocks":[...]}
{"response":"Phần đầu câu trả lời"}
{"response":" phần tiếp theo"}
```

Các field có thể xuất hiện:

| Field | Mô tả |
| --- | --- |
| `references` | Thường xuất hiện ở dòng đầu nếu bật references |
| `content_blocks` | Context/evidence chuẩn hóa, có thể xuất hiện cùng dòng metadata đầu |
| `graph_context` | Graph IDs nếu bật option |
| `response` | Một fragment hoặc toàn bộ response |
| `error` | Lỗi trong quá trình stream |

Nếu gửi `stream=false`, endpoint vẫn trả NDJSON nhưng thường chỉ có một dòng
chứa response hoàn chỉnh. Client phải parse theo từng dòng, không parse toàn bộ
body như một JSON duy nhất.

### `POST /query/data`

Request giống query nhưng endpoint không gọi bước sinh câu trả lời LLM; luôn
trả dữ liệu retrieval có cấu trúc và luôn gồm references.

Output:

```json
{
  "status": "success",
  "message": "Query executed successfully",
  "data": {
    "entities": [
      {
        "entity_name": "Qwen3",
        "entity_type": "MODEL",
        "description": "...",
        "source_id": "chunk-123",
        "file_path": "meeting.mp4",
        "reference_id": "1"
      }
    ],
    "relationships": [
      {
        "src_id": "Qwen3",
        "tgt_id": "vLLM",
        "description": "...",
        "keywords": "serving, inference",
        "weight": 1.0,
        "source_id": "chunk-123",
        "file_path": "meeting.mp4",
        "reference_id": "1"
      }
    ],
    "chunks": [
      {
        "content": "Retrieved chunk text",
        "file_path": "meeting.mp4",
        "chunk_id": "chunk-123",
        "reference_id": "1"
      }
    ],
    "references": [{"reference_id": "1", "file_path": "meeting.mp4"}]
  },
  "metadata": {
    "query_mode": "mix",
    "keywords": {"high_level": ["..."], "low_level": ["..."]},
    "processing_info": {
      "total_entities_found": 1,
      "total_relations_found": 1,
      "entities_after_truncation": 1,
      "relations_after_truncation": 1,
      "final_chunks_count": 1
    }
  }
}
```

Trong `naive`, `entities`/`relationships` thường rỗng; trong `bypass`, các
mảng retrieval thường rỗng. Khi validation lỗi, endpoint trả:

```json
{"status":"failure","message":"Validation error: ...","data":{},"metadata":{}}
```

## 6. Knowledge graph

### Read endpoints

| Method và endpoint | Input | Output |
| --- | --- | --- |
| `GET /graph/label/list` | Không có | `string[]`, toàn bộ graph labels |
| `GET /graph/label/popular?limit=300` | `limit`: 1–1000 | `string[]`, labels theo degree |
| `GET /graph/label/search?q=Tesla&limit=50` | `q` bắt buộc; `limit`: 1–100 | `string[]`, labels fuzzy match |
| `GET /graph/entity/exists?name=Tesla` | `name` bắt buộc | `{"exists": true/false}` |
| `GET /graphs?label=Tesla&max_depth=3&max_nodes=1000` | `label` bắt buộc; `max_depth >= 1`; `max_nodes >= 1` | Connected graph object |

`GET /graphs` trả graph theo backend hiện tại, thường gồm nodes và edges.
`max_depth` giới hạn số hop; `max_nodes` giới hạn node trả về.

### `GET /graphs/projection`

Đây là contract graph ổn định, đã normalize raw graph backend và hỗ trợ phân
trang.

| Parameter | Mặc định | Ràng buộc/mô tả |
| --- | --- | --- |
| `view` | `pruned` | `full` hoặc `pruned` |
| `max_nodes` | 300 | 1–10.000 |
| `max_edges` | 800 | 1–50.000 |
| `strategy` | `importance` | `importance`, `community`, `degree` |
| `seed` | `""` | Entity ID/label để lấy projection cục bộ |
| `max_depth` | 2 | 0–50; có tác dụng khi có seed |
| `include_media` | true | Gắn asset references vào node |
| `cursor` | `""` | Opaque cursor từ response trước |

Output:

```json
{
  "workspace_id": "<workspace-uuid>",
  "view": "pruned",
  "revision": "2026-08-02T12:00:00Z",
  "nodes": [
    {
      "id": "Qwen3",
      "name": "Qwen3",
      "entity_type": "MODEL",
      "labels": ["Qwen3"],
      "description": "...",
      "source_document_ids": ["doc-..."],
      "display": {"kind": "entity", "title": "Qwen3"},
      "metadata": {"degree": 4, "community": 0},
      "media": [
        {"asset_id": "asset-...", "kind": "image", "mime_type": "image/jpeg", "page": null, "caption": "..."}
      ]
    }
  ],
  "edges": [
    {
      "id": "rel-...",
      "source": "Qwen3",
      "target": "vLLM",
      "relation": "served_by",
      "description": "...",
      "keywords": ["serving"],
      "weight": 1.0,
      "source_document_ids": ["doc-..."]
    }
  ],
  "page": {"next_cursor": null, "truncated": false},
  "stats": {
    "total_nodes": 10,
    "total_edges": 12,
    "returned_nodes": 10,
    "returned_edges": 12
  }
}
```

`media[].asset_id` là opaque ID; dùng `/assets/{asset_id}` để lấy bytes, không
đọc hoặc tự ghép filesystem path.

### Graph mutation request schemas

| Endpoint | Input JSON |
| --- | --- |
| `POST /graph/entity/create` | `{"entity_name": "Tesla", "entity_data": {"description": "...", "entity_type": "ORGANIZATION"}}` |
| `POST /graph/entity/edit` | `{"entity_name": "Tesla", "updated_data": {"description": "..."}, "allow_rename": false, "allow_merge": false}` |
| `POST /graph/entities/merge` | `{"entities_to_change": ["Tesla Inc"], "entity_to_change_into": "Tesla"}` |
| `POST /graph/relation/create` | `{"source_entity": "Elon Musk", "target_entity": "Tesla", "relation_data": {"description": "...", "keywords": "CEO", "weight": 1.0}}` |
| `POST /graph/relation/edit` | `{"source_id": "Elon Musk", "target_id": "Tesla", "updated_data": {"weight": 2.0}}` |
| `DELETE /graph/entity/delete` | `{"entity_name": "Tesla"}` |
| `DELETE /graph/relation/delete` | `{"source_entity": "Elon Musk", "target_entity": "Tesla"}` |

Mô tả field:

- `entity_data`, `updated_data`, `relation_data` là dictionary mở; các field
  thường gặp là `description`, `entity_type`, `keywords`, `source_id`, `weight`.
- `allow_rename` cho phép đổi tên entity qua `updated_data.entity_name`.
- `allow_merge` cho phép tự merge nếu tên mới đã tồn tại.
- `entities_to_change` là danh sách entity nguồn sẽ bị xóa; entity đích phải
  tồn tại.
- Relation được lưu dạng undirected; source/target có thể đổi thứ tự khi trả.

Các endpoint create/edit/merge trả envelope:

```json
{
  "status": "success",
  "message": "...",
  "data": {"...": "operation-specific fields"}
}
```

`/graph/entity/edit` thêm `operation_summary`:

```json
{
  "merged": false,
  "merge_status": "not_attempted",
  "merge_error": null,
  "operation_status": "success",
  "target_entity": null,
  "final_entity": "Tesla",
  "renamed": false
}
```

Deletion output `DeletionResult`:

```json
{
  "status": "success",
  "doc_id": "",
  "message": "Entity deleted successfully",
  "status_code": 200,
  "file_path": null
}
```

`status` có thể là `success`, `not_found`, `not_allowed` hoặc `fail`.

## 7. Multimodal assets

### `GET /assets/{asset_id}`

Path parameter `asset_id` là opaque asset ID lấy từ query content block,
reference hoặc graph projection.

Query parameters:

| Parameter | Mặc định | Mô tả |
| --- | --- | --- |
| `variant` | `original` | `original` hoặc `thumbnail` |
| `download` | `false` | `false` trả inline; `true` yêu cầu download |

Response thành công là binary, không phải JSON. Header quan trọng:

| Header | Mô tả |
| --- | --- |
| `Content-Type` | MIME type của asset |
| `Content-Disposition` | `inline` hoặc `attachment` |
| `ETag` | SHA-256 dạng `"sha256-..."` |
| `Cache-Control` | `private, max-age=3600` |
| `Accept-Ranges` | `bytes` |

Asset chỉ được phục vụ nếu thuộc workspace trong `X-LightRAG-Workspace-ID`.
Asset không tồn tại hoặc thuộc workspace khác đều trả `404`.

```bash
curl -L "$LIGHTRAG_URL/assets/$ASSET_ID?variant=thumbnail" \
  -H "X-API-Key: $LIGHTRAG_API_KEY" \
  -H "X-LightRAG-Workspace-ID: $WORKSPACE_ID" \
  -o scene.jpg
```

## 8. Ollama-compatible API

Các endpoint có prefix `/api`, dùng cho Open WebUI hoặc client tương thích
Ollama. Chúng cũng nhận `X-LightRAG-Workspace-ID`.

### `GET /api/version`

Output:

```json
{"version": "0.9.3"}
```

Version response là compatibility version và có thể thay đổi.

### `GET /api/tags`

Output `OllamaTagResponse`:

```json
{
  "models": [
    {
      "name": "lightrag:latest",
      "model": "lightrag:latest",
      "size": 0,
      "digest": "...",
      "modified_at": "...",
      "details": {
        "parent_model": "",
        "format": "gguf",
        "family": "lightrag",
        "families": ["lightrag"],
        "parameter_size": "13B",
        "quantization_level": "Q4_0"
      }
    }
  ]
}
```

### `GET /api/ps`

Output `{"models": [...]}`. Mỗi model gồm các field của `/api/tags` và thêm
`expires_at`, `size_vram`.

### `POST /api/generate`

Input JSON:

| Field | Kiểu | Bắt buộc | Mặc định/mô tả |
| --- | --- | --- | --- |
| `model` | string | Có | Tên model compatibility |
| `prompt` | string | Có | Prompt gửi trực tiếp cho LLM |
| `system` | string/null | Không | System prompt |
| `stream` | boolean | Không | `false` |
| `options` | object/null | Không | Option phụ tùy client/provider |

Khác với `/query`, endpoint này gọi LLM trực tiếp, không tự chạy retrieval.

Non-stream response:

```json
{
  "model": "lightrag:latest",
  "created_at": "2026-08-02T12:00:00Z",
  "response": "Generated text",
  "done": true,
  "context": [],
  "total_duration": 123,
  "load_duration": 0,
  "prompt_eval_count": 10,
  "prompt_eval_duration": 10,
  "eval_count": 20,
  "eval_duration": 100
}
```

Với `stream=true`, response là NDJSON gồm nhiều object có `response` fragment
và dòng cuối `done=true`. Khi provider lỗi, stream có thể chứa `error`.

### `POST /api/chat`

Input JSON:

| Field | Kiểu | Bắt buộc | Mặc định/mô tả |
| --- | --- | --- | --- |
| `model` | string | Có | Tên model compatibility |
| `messages` | object[] | Có | Không rỗng; phần tử cuối phải role `user` |
| `messages[].role` | string | Có | `system`, `user`, `assistant` hoặc role tương thích |
| `messages[].content` | string | Có | Nội dung message |
| `messages[].images` | string[]/null | Không | Trường tương thích Ollama; base64 image nếu client cần |
| `stream` | boolean | Không | `true` |
| `options` | object/null | Không | Option phụ |
| `system` | string/null | Không | System prompt bổ sung |

LightRAG parse mode prefix ở message user cuối:

```text
/local câu hỏi
/global câu hỏi
/hybrid câu hỏi
/naive câu hỏi
/mix câu hỏi
/bypass câu hỏi
/context câu hỏi
/hybrid[Trả lời ngắn] câu hỏi
```

History các message trước được chuyển thành `conversation_history` cho query.

Non-stream response:

```json
{
  "model": "lightrag:latest",
  "created_at": "2026-08-02T12:00:00Z",
  "message": {
    "role": "assistant",
    "content": "Answer text",
    "images": null
  },
  "done": true
}
```

Với `stream=true`, response là `application/x-ndjson`; mỗi dòng có cùng shape,
`message.content` là fragment. Dòng cuối có `done=true`; lỗi có thể có thêm
field `error`.

## 9. Luồng tích hợp khuyến nghị

```text
POST /v1/workspaces
        |
POST /documents/upload hoặc /documents/text
        |
GET /documents/track_status/{track_id}
        |
POST /query, /query/stream hoặc /query/data
        |
GET /assets/{asset_id} cho hình ảnh/contact sheet/video evidence
```

Với video, chỉ cần upload qua `/documents/upload`; không cần endpoint riêng.
Sau khi status là `processed`, query với:

```json
{
  "query": "Trong video có những nội dung gì?",
  "mode": "mix",
  "include_references": true,
  "include_content_blocks": true
}
```

Client nên lưu `track_id`, `doc_id`, `asset_id` và `job_id` để retry/đối soát;
không retry các thao tác destructive nếu chưa xác định trạng thái hiện tại.
