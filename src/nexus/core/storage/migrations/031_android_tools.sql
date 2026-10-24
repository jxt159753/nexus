CREATE TABLE grants_new (
    grant_id TEXT PRIMARY KEY,
    identity_id TEXT NOT NULL REFERENCES identities(identity_id),
    tool TEXT NOT NULL CHECK(tool IN ('system.info','filesystem.list','filesystem.read','filesystem.search','filesystem.write_preview','filesystem.write','command.run','filesystem.read_lines','filesystem.glob','filesystem.grep','filesystem.edit_preview','filesystem.edit','interaction.observe','interaction.act','filesystem.read_image','plugin.call','android.device_status','android.notification_digest')),
    tool_version INTEGER NOT NULL CHECK(tool_version=1),
    root_id TEXT NOT NULL CHECK(root_id IN ('','workspace','android:device','android:notifications')),
    UNIQUE(identity_id,tool,tool_version,root_id),
    CHECK((tool='system.info' AND root_id='') OR
          (tool='android.device_status' AND root_id='android:device') OR
          (tool='android.notification_digest' AND root_id='android:notifications') OR
          (tool NOT IN ('system.info','android.device_status','android.notification_digest') AND root_id='workspace'))
);
INSERT INTO grants_new SELECT * FROM grants;
DROP TABLE grants;
ALTER TABLE grants_new RENAME TO grants;
