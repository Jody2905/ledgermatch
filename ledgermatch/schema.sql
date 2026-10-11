-- LedgerMatch database schema
-- Amounts are stored as INTEGER cents (12.50 -> 1250): exact in every database.

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('preparer', 'approver')),
    password_hash TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1
);

-- The bank accounts being reconciled
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    bank_name   TEXT NOT NULL,
    last_digits TEXT NOT NULL            -- only the last 4 digits are ever stored
);

-- One reconciliation of one account for one period
CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        INTEGER NOT NULL REFERENCES accounts (id),
    period_end        TEXT NOT NULL,
    bank_balance      INTEGER NOT NULL,
    book_balance      INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft', 'submitted', 'approved', 'rejected')),
    prepared_by       INTEGER NOT NULL REFERENCES users (id),
    created_at        TEXT NOT NULL,
    submitted_at      TEXT,
    approved_by       INTEGER REFERENCES users (id),
    approved_at       TEXT,
    rejection_reason  TEXT,
    UNIQUE (account_id, period_end)
);

CREATE TABLE IF NOT EXISTS bank_lines (
    run_id       INTEGER NOT NULL REFERENCES runs (id),
    line_no      INTEGER NOT NULL,
    date         TEXT NOT NULL,
    description  TEXT NOT NULL,
    reference    TEXT NOT NULL,
    amount       INTEGER NOT NULL,
    error_reason TEXT,                   -- set when marked as a bank error
    PRIMARY KEY (run_id, line_no)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    run_id       INTEGER NOT NULL REFERENCES runs (id),
    line_no      INTEGER NOT NULL,
    date         TEXT NOT NULL,
    payee        TEXT NOT NULL,
    reference    TEXT NOT NULL,
    voucher      TEXT NOT NULL,
    amount       INTEGER NOT NULL,
    error_reason TEXT,                   -- set when marked as an error in the books
    PRIMARY KEY (run_id, line_no)
);

CREATE TABLE IF NOT EXISTS matches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs (id),
    rule        TEXT NOT NULL CHECK (rule IN ('reference', 'amount_date', 'grouped', 'manual')),
    note        TEXT NOT NULL DEFAULT '',
    difference  INTEGER NOT NULL DEFAULT 0,   -- bank total minus ledger total (manual matches only)
    created_by  INTEGER REFERENCES users (id) -- NULL for automatic matches
);

-- Which lines belong to which match (one match can cover several lines)
CREATE TABLE IF NOT EXISTS match_items (
    match_id  INTEGER NOT NULL REFERENCES matches (id) ON DELETE CASCADE,
    run_id    INTEGER NOT NULL,
    side      TEXT NOT NULL CHECK (side IN ('bank', 'ledger')),
    line_no   INTEGER NOT NULL,
    PRIMARY KEY (match_id, side, line_no),
    UNIQUE (run_id, side, line_no)           -- a line can only ever be in one match
);

CREATE TABLE IF NOT EXISTS issues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs (id),
    kind            TEXT NOT NULL,
    detail          TEXT NOT NULL,
    dismissed_by    INTEGER REFERENCES users (id),
    dismiss_reason  TEXT
);

CREATE TABLE IF NOT EXISTS issue_items (
    issue_id  INTEGER NOT NULL REFERENCES issues (id),
    side      TEXT NOT NULL CHECK (side IN ('bank', 'ledger')),
    line_no   INTEGER NOT NULL,
    PRIMARY KEY (issue_id, side, line_no)
);

-- Who did what, when. Rows are only ever added.
CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   INTEGER REFERENCES runs (id),
    user_id  INTEGER REFERENCES users (id),
    action   TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT '',
    at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log (run_id);
