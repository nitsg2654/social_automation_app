CREATE TABLE IF NOT EXISTS social_posts (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  batch_id CHAR(36) NULL,
  topic TEXT NOT NULL,
  platform VARCHAR(50) NOT NULL,
  post_content LONGTEXT NOT NULL,
  hashtags TEXT NULL,
  image_prompt TEXT NULL,
  image_url TEXT NULL,
  status VARCHAR(30) DEFAULT 'pending',
  scheduled_at DATETIME(6) NULL,
  posted_at DATETIME(6) NULL,
  platform_post_id VARCHAR(255) NULL,
  platform_post_url TEXT NULL,
  retry_count INT DEFAULT 0,
  error_message TEXT NULL,
  created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  KEY idx_social_posts_status_schedule (status, scheduled_at),
  KEY idx_social_posts_platform_posted (platform, posted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS platform_credentials (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  credential_name VARCHAR(120) NOT NULL,
  platform VARCHAR(30) NOT NULL,
  page_id VARCHAR(255) NOT NULL,
  access_token TEXT NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  UNIQUE KEY uq_platform_credentials_account (platform, page_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
