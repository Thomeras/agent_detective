-- ClickHouse init for Agent Detective (build spec section 5).
-- Raw OTLP spans, one row per span; queried by trace_id.

CREATE TABLE IF NOT EXISTS otel_spans
(
    trace_id       String,
    span_id        String,
    parent_span_id String,
    name           String,
    kind           String,
    start_time     DateTime64(9),
    end_time       DateTime64(9),
    attributes     String,  -- raw JSON attributes payload
    status_code    String,
    resource_attributes String DEFAULT '{}'  -- flattened OTLP resource attrs (JSON object)
)
ENGINE = MergeTree
ORDER BY (trace_id, start_time);
