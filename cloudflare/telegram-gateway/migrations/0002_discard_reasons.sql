DROP INDEX telegram_updates_status;

CREATE TABLE telegram_updates_v2 (
  update_id INTEGER PRIMARY KEY,
  status TEXT NOT NULL CHECK (
    status IN (
      'received',
      'awaiting_reason',
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

INSERT INTO telegram_updates_v2 (
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
ALTER TABLE telegram_updates_v2 RENAME TO telegram_updates;

CREATE INDEX telegram_updates_status
  ON telegram_updates (status, updated_at);

CREATE TABLE pending_discard_reasons (
  request_id TEXT PRIMARY KEY,
  prompt_message_id INTEGER UNIQUE,
  application_id TEXT NOT NULL,
  vacancy_version TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN (
      'prompting',
      'pending',
      'dispatching',
      'dispatched',
      'failed',
      'uncertain',
      'superseded'
    )
  ),
  reason_text TEXT,
  reply_update_id INTEGER UNIQUE,
  status_evidence TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  consumed_at TEXT
);

CREATE INDEX pending_discard_reasons_scope
  ON pending_discard_reasons (
    actor_id,
    chat_id,
    status,
    expires_at
  );
