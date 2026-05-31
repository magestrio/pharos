-- 0001_initial.sql — `data-store.2`. Source-of-truth schema for the
-- vault8004 cycle/portfolio/event store. Design rationale lives in
-- ~/Documents/brain/01-projects/vault8004/notes/data-store.md.

CREATE TABLE cycles (
  cycle_ts          TIMESTAMPTZ PRIMARY KEY,
  started_at        TIMESTAMPTZ NOT NULL,
  finished_at       TIMESTAMPTZ,
  result            TEXT NOT NULL,
  wake_reason       TEXT NOT NULL,
  confidence        DOUBLE PRECISION,
  expected_apr_pct  DOUBLE PRECISION,
  actions_planned   INTEGER,
  actions_executed  INTEGER,
  error             TEXT
);
CREATE INDEX cycles_started_at_idx ON cycles (started_at DESC);
CREATE INDEX cycles_wake_reason_idx ON cycles (wake_reason);

CREATE TABLE snapshots (
  cycle_ts  TIMESTAMPTZ PRIMARY KEY REFERENCES cycles(cycle_ts) ON DELETE CASCADE,
  payload   JSONB NOT NULL
);

CREATE TABLE decisions (
  cycle_ts  TIMESTAMPTZ PRIMARY KEY REFERENCES cycles(cycle_ts) ON DELETE CASCADE,
  payload   JSONB NOT NULL
);
CREATE INDEX decisions_confidence_idx
  ON decisions ((payload->>'confidence'));

CREATE TABLE positions_snapshot (
  cycle_ts    TIMESTAMPTZ NOT NULL REFERENCES cycles(cycle_ts) ON DELETE CASCADE,
  venue       TEXT NOT NULL,
  product_id  TEXT NOT NULL DEFAULT '',
  coin        TEXT,
  amount      NUMERIC(38, 18),
  amount_usd  NUMERIC(20, 4),
  PRIMARY KEY (cycle_ts, venue, product_id)
);
CREATE INDEX positions_venue_coin_idx ON positions_snapshot (venue, coin);

CREATE TABLE events (
  id                  BIGSERIAL PRIMARY KEY,
  event_ts            TIMESTAMPTZ NOT NULL,
  kind                TEXT NOT NULL,
  severity            TEXT NOT NULL,
  position_id         TEXT,
  coin                TEXT,
  payload             JSONB NOT NULL,
  triggered_cycle_ts  TIMESTAMPTZ REFERENCES cycles(cycle_ts) ON DELETE SET NULL
);
CREATE INDEX events_event_ts_idx ON events (event_ts DESC);
CREATE INDEX events_kind_idx ON events (kind);
CREATE INDEX events_triggered_cycle_idx ON events (triggered_cycle_ts);

CREATE TABLE executions (
  cycle_ts  TIMESTAMPTZ NOT NULL REFERENCES cycles(cycle_ts) ON DELETE CASCADE,
  idx       INTEGER NOT NULL,
  action    JSONB NOT NULL,
  status    TEXT NOT NULL,
  error     TEXT,
  PRIMARY KEY (cycle_ts, idx)
);
CREATE INDEX executions_status_idx ON executions (status);
