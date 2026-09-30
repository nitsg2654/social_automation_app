CREATE TABLE IF NOT EXISTS social_posts (
    id BIGINT NOT NULL AUTO_INCREMENT,
    batch_id CHAR(36) NOT NULL,
    topic TEXT NOT NULL,
    platform VARCHAR(50) NOT NULL,
    post_content LONGTEXT NOT NULL,
    hashtags LONGTEXT NULL,
    image_prompt LONGTEXT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    scheduled_at DATETIME(6) NULL,
    posted_at DATETIME(6) NULL,
    platform_post_id TEXT NULL,
    retry_count INT NOT NULL DEFAULT 0,
    error_message TEXT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    image_url TEXT NULL,
    PRIMARY KEY (id),
    KEY idx_social_posts_status_created (status, created_at),
    KEY idx_social_posts_platform (platform)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS social_approval_keys (
    idempotency_key CHAR(36) NOT NULL PRIMARY KEY,
    post_id BIGINT NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_social_approval_post (post_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
