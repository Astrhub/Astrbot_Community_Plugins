ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS superseded_at timestamptz;
ALTER TABLE artifact_jobs ADD COLUMN IF NOT EXISTS superseded_at timestamptz;

ALTER TABLE review_decisions DROP CONSTRAINT IF EXISTS review_decisions_action_check;
ALTER TABLE review_decisions ADD CONSTRAINT review_decisions_action_check CHECK (
    action IN (
        'auto_reject', 'auto_approve', 'approve', 'reject', 'request_changes',
        'retry_publish', 'revoke', 'emergency_override', 'policy_migrate',
        'comment', 'manual_approve', 'retry_review'
    )
);
ALTER TABLE review_decisions ADD CONSTRAINT review_decisions_manual_reason_check CHECK (
    action NOT IN ('comment', 'manual_approve', 'retry_review') OR length(btrim(reason)) > 0
);
