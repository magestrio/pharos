-- 0002_pick_invalidate_view.sql — 2026-06-03. The `Pick.invalidate_at`
-- field added to the decision schema lands inside `decisions.payload`
-- JSONB (no column needed — payload is forward-compatible). This view
-- gives the web UI + ad-hoc analytics a flat per-pick projection so the
-- "which picks have custom stop-loss thresholds" query is one SELECT
-- away rather than a hand-rolled JSON traversal.
--
-- The view is read-only and recomputed on each query; no
-- materialization or maintenance overhead. Drops the cycle if any of
-- the nested keys are missing (`?` operator on jsonb), so picks
-- predating the schema extension don't pollute the output.

CREATE OR REPLACE VIEW pick_invalidations AS
SELECT
  d.cycle_ts,
  v.value->>'venue_id'                          AS venue_id,
  p.value->>'product_id'                        AS product_id,
  p.value->'invalidate_at'                      AS invalidate_at,
  (p.value->'invalidate_at') IS NOT NULL
    AND (p.value->'invalidate_at') != 'null'    AS has_custom_invalidate
FROM decisions d,
  jsonb_array_elements(d.payload->'venues') AS v,
  jsonb_array_elements(v.value->'picks')    AS p
WHERE p.value ? 'invalidate_at';

COMMENT ON VIEW pick_invalidations IS
  'Flat per-pick projection of decisions.payload[venues][picks][invalidate_at]. '
  'has_custom_invalidate distinguishes operator-set thresholds from null/default.';
