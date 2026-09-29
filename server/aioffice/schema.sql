CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER,
  agent TEXT,
  step TEXT,
  backend TEXT,                      -- direct | embed | rerank
  model TEXT,
  prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  latency_ms INTEGER DEFAULT 0,
  status TEXT NOT NULL,              -- ok | error
  error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_task ON llm_calls(task_id);

CREATE TABLE IF NOT EXISTS embeddings (
  text_hash TEXT NOT NULL,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL,
  PRIMARY KEY (text_hash, model)
);
