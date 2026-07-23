ALTER TABLE review_decisions
    DROP CONSTRAINT IF EXISTS review_decisions_action_check;
ALTER TABLE review_decisions
    ADD CONSTRAINT review_decisions_action_check
    CHECK (
        action IN (
            'auto_reject', 'auto_approve', 'approve', 'reject',
            'request_changes', 'retry_publish', 'revoke', 'emergency_override',
            'policy_migrate'
        )
    );

ALTER TABLE review_decisions
    DROP CONSTRAINT IF EXISTS review_decisions_policy_migrate_reason_check;
ALTER TABLE review_decisions
    ADD CONSTRAINT review_decisions_policy_migrate_reason_check
    CHECK (
        action <> 'policy_migrate'
        OR (
            policy_version_id IS NOT NULL
            AND length(btrim(reason)) > 0
        )
    );

CREATE INDEX IF NOT EXISTS review_decisions_policy_migration_idx
    ON review_decisions(artifact_id, created_at DESC)
    WHERE action = 'policy_migrate';
