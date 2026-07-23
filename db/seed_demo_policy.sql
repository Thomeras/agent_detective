-- Demo shadow-policy rules that carry real signal (replaces the always-firing
-- 'spend-limit-demo' $0.001 cost cap — a rule that fires on every run
-- demonstrates nothing and sits, miscategorised, next to causal findings).
--
-- Apply:
--   docker compose exec -T postgres psql -U postgres -d agent_detective \
--     -f - < db/seed_demo_policy.sql
--
-- Two rules, two honest governance stories:
--   spend-anomaly      cost governance — warns only on an anomalous run
--                      (typical demo/live runs cost $0.004–$0.034; $0.05 is a
--                      genuine outlier, e.g. a retry storm), so a firing is
--                      information, not noise.
--   latent-defect-gate correctness governance — "this gate WOULD HAVE blocked
--                      the run that shipped a contract-nonconformant
--                      deliverable" (shadow mode: recorded, never enforced).

UPDATE policy_rules
SET name = 'spend-anomaly',
    predicate = '{"cost_over": 0.05}',
    action = 'warn'
WHERE name = 'spend-limit-demo';

INSERT INTO policy_rules (name, predicate, action, shadow, enabled)
VALUES ('latent-defect-gate',
        '{"report_types_any": ["shipped_with_latent_defect"]}',
        'block', true, true)
ON CONFLICT (name) DO NOTHING;
