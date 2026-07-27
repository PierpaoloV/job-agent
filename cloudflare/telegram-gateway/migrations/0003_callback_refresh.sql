DROP INDEX telegram_updates_status;

CREATE TABLE telegram_updates_v3 (
  update_id INTEGER PRIMARY KEY,
  status TEXT NOT NULL CHECK (
    status IN (
      'received',
      'awaiting_reason',
      'refreshed',
      'rejected',
      'dispatching',
      'dispatched',
      'failed',
      'uncertain'
    )
  ),
  evidence TEXT NOT NULL DEFAULT '',
  received_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

INSERT INTO telegram_updates_v3 (
  update_id,
  status,
  evidence,
  received_at,
  updated_at
)
SELECT
  update_id,
  status,
  evidence,
  received_at,
  updated_at
FROM telegram_updates;

DROP TABLE telegram_updates;
ALTER TABLE telegram_updates_v3 RENAME TO telegram_updates;

CREATE INDEX telegram_updates_status
  ON telegram_updates (status, updated_at);
