CREATE TABLE assistant_preferences (
    owner_id TEXT PRIMARY KEY REFERENCES identities(identity_id),
    personality TEXT NOT NULL CHECK(personality IN ('nexus','companion','explorer','advisor')),
    emoji TEXT NOT NULL CHECK(emoji IN ('off','natural','expressive')),
    revision INTEGER NOT NULL CHECK(revision >= 1)
);
ALTER TABLE chat_turns ADD COLUMN assistant_style_json TEXT NOT NULL DEFAULT '{"personality":"nexus","emoji":"natural"}';
