CREATE TABLE artifact_reviews (
  review_id TEXT PRIMARY KEY,
  event_key TEXT NOT NULL UNIQUE,
  application_id TEXT NOT NULL,
  vacancy_version TEXT NOT NULL,
  package_hash TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN (
      'authorizing',
      'documents_sent',
      'pending',
      'deciding',
      'approved',
      'regenerate_requested',
      'expired',
      'cleanup_uncertain',
      'cleanup_retrying',
      'dispatch_uncertain',
      'dispatch_recovering',
      'dispatch_accepted',
      'expiring',
      'expiry_cleanup_uncertain'
    )
  ),
  document_message_ids TEXT,
  control_message_id INTEGER,
  decision TEXT,
  status_evidence TEXT NOT NULL DEFAULT '',
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  decided_at TEXT
);

CREATE INDEX artifact_reviews_expiry
  ON artifact_reviews (status, expires_at);

CREATE TABLE artifact_review_authorizations (
  token TEXT PRIMARY KEY,
  review_id TEXT NOT NULL REFERENCES artifact_reviews(review_id),
  action TEXT NOT NULL CHECK (
    action IN ('approve_artifacts', 'regenerate_artifacts')
  ),
  actor_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('issued', 'consumed')),
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  consumed_at TEXT,
  UNIQUE (review_id, action)
);

CREATE INDEX artifact_review_authorizations_expiry
  ON artifact_review_authorizations (status, expires_at);
