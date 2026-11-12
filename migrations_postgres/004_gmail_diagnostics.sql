CREATE TABLE gmail_diagnostics (
 id TEXT PRIMARY KEY,
 mailbox TEXT NOT NULL,
 recipient TEXT NOT NULL,
 subject TEXT NOT NULL,
 body TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 snapshot TEXT NOT NULL,
 expires BIGINT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('prepared','sending','sent','unknown')),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 approved_at TEXT,
 sent_at TEXT,
 message_id TEXT,
 thread_id TEXT,
 reply_count INTEGER NOT NULL DEFAULT 0,
 checked_at TEXT,
 UNIQUE(mailbox,recipient)
);
