ALTER TABLE platform_credentials
  ADD COLUMN credential_name VARCHAR(120) NOT NULL DEFAULT 'Unnamed account' AFTER id;
