CREATE TABLE device_permission_sync (
    client_id TEXT PRIMARY KEY REFERENCES device_clients(client_id),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    permissions_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
