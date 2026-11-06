# Coordination state

Chỉ **integrator** ghi file này. Mỗi stream một dòng.

| stream | owner | current task | status | last sha | note |
|--------|-------|--------------|--------|----------|------|
| stream-A | A | — | idle | — | |
| stream-B | B | — | idle | — | |
| stream-C | C | — | idle | — | |

Trạng thái hợp lệ: `active` / `waiting` / `idle` / `blocked`.
Một dòng `dependency` (tuỳ chọn) ghi các task đang mở, khớp với `.coord/deps.jsonl`.
