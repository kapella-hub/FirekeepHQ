# Stale Webhook Cleanup — 2026-05-27

## Webhooks Removed

| id | url | events | created_at |
|----|-----|--------|------------|
| a3121872-1e0b-4e2b-8103-d335931f98a5 | http://example.com/hook | memory.learned | 2026-03-05 |
| 9da47f84-deec-4104-aec7-cbb37460dd96 | https://httpbin.org/post | sentinel.alert, session.completed | 2026-03-19 |

Both were placeholder/test URLs from initial setup in March 2026. Neither destination was a real
consumer — events were firing into the void on every memory write, alert, and session completion.

## State After Cleanup

`GET /webhooks/` returns `[]`. Webhook infrastructure is intact; no destinations registered.
New webhooks can be added via `POST /webhooks/` when real consumers exist.
