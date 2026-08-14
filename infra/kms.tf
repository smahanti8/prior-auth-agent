# ── Customer-managed key for audit log encryption ──────────────────────────────
#
# Why CMK over AWS-managed (aws/s3):
#   With an AWS-managed key, AWS controls key access and under certain operational
#   conditions AWS staff can theoretically access key material. With a CMK, the
#   customer owns the key policy and AWS cannot access customer-managed key material.
#
# Key policy separates admin from user:
#   - Key admin role: manages key lifecycle (rotate, disable, schedule deletion)
#     but cannot use the key for data encryption/decryption.
#   - Task role: can encrypt and decrypt audit objects but cannot manage the key.
#   This prevents a compromised container from disabling or destroying its own
#   audit trail.
#
# Replace PLACEHOLDER-account-id and PLACEHOLDER-key-admin-role with your values
# before applying. The root statement is required by AWS — do not remove it.

resource "aws_kms_key" "audit_log" {
  description             = "CMK for ${var.app_name} audit log — customer holds key policy"
  deletion_window_in_days = var.kms_deletion_window_days
  enable_key_rotation     = true  # annual automatic rotation
  multi_region            = false # cross-region replication uses a separate CMK in the target region

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Full key management for the account root. Required by AWS — cannot be removed.
        Sid    = "RootAdminAccess"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::PLACEHOLDER-account-id:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        # Key admin role: manages lifecycle but cannot encrypt/decrypt data.
        Sid    = "KeyAdministration"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::PLACEHOLDER-account-id:role/PLACEHOLDER-key-admin-role"
        }
        Action = [
          "kms:Create*", "kms:Describe*", "kms:Enable*", "kms:List*",
          "kms:Put*", "kms:Update*", "kms:Revoke*", "kms:Disable*",
          "kms:Get*", "kms:Delete*", "kms:ScheduleKeyDeletion", "kms:CancelKeyDeletion",
          "kms:TagResource", "kms:UntagResource",
        ]
        Resource = "*"
      },
      {
        # Task role: encrypt/decrypt audit objects only — no key admin actions.
        Sid    = "TaskRoleEncryptDecrypt"
        Effect = "Allow"
        Principal = {
          AWS = aws_iam_role.ecs_task.arn
        }
        Action   = ["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"]
        Resource = "*"
      },
      {
        # Execution role: decrypt for ECR image pull.
        Sid    = "ExecutionRoleDecrypt"
        Effect = "Allow"
        Principal = {
          AWS = aws_iam_role.ecs_execution.arn
        }
        Action   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = "*"
      },
    ]
  })

  tags = {
    Name      = "${var.app_name}-audit-cmk"
    DataClass = "PHI-adjacent"
  }
}

resource "aws_kms_alias" "audit_log" {
  name          = "alias/${var.app_name}-audit"
  target_key_id = aws_kms_key.audit_log.key_id
}
