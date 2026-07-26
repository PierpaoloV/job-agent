CREATE TABLE callback_authorizations (
  token TEXT PRIMARY KEY,
  group_key TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('prepare', 'discard', 'details')),
  application_id TEXT NOT NULL,
  vacancy_version TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('issued', 'consumed')),
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  consumed_at TEXT,
  UNIQUE (group_key, action)
);

CREATE INDEX callback_authorizations_expiry
  ON callback_authorizations (status, expires_at);

CREATE TABLE telegram_updates (
  update_id INTEGER PRIMARY KEY,
  status TEXT NOT NULL CHECK (
    status IN (
      'received',
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

CREATE INDEX telegram_updates_status
  ON telegram_updates (status, updated_at);
