# ── IAM roles for ECS ─────────────────────────────────────────────────────────
#
# Two distinct roles:
#   execution role — used by the ECS control plane to pull images and write logs
#   task role      — assumed by the running container for Bedrock and S3 calls
#
# Separation matters: the container process cannot pull images or modify the
# execution infrastructure even if the task role is compromised. The task role
# cannot manage the KMS key it uses for encryption — only the key admin role can.

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ── Execution role ─────────────────────────────────────────────────────────────

resource "aws_iam_role" "ecs_execution" {
  name               = "${var.app_name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Allow execution role to decrypt ECR images encrypted with the CMK.
resource "aws_iam_role_policy" "execution_kms" {
  name = "kms-ecr-decrypt"
  role = aws_iam_role.ecs_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
      Resource = [aws_kms_key.audit_log.arn]
    }]
  })
}

# ── Task role ──────────────────────────────────────────────────────────────────

resource "aws_iam_role" "ecs_task" {
  name               = "${var.app_name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

# Bedrock: invoke models only — no key management, no model creation.
resource "aws_iam_role_policy" "task_bedrock" {
  name = "bedrock-invoke"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
      ]
      # Scoped to Anthropic foundation models in this region.
      Resource = ["arn:aws:bedrock:${var.aws_region}::foundation-model/anthropic.*"]
    }]
  })
}

# S3: write and read audit log objects only — no delete, no bucket operations.
resource "aws_iam_role_policy" "task_s3_audit" {
  name = "s3-audit-write"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:GetObject"]
      Resource = ["${aws_s3_bucket.audit_archive.arn}/determinations/*"]
    }]
  })
}

# KMS: encrypt and decrypt audit log objects — no key admin actions.
# The task can use the key but cannot rotate, disable, or schedule it for deletion.
resource "aws_iam_role_policy" "task_kms" {
  name = "kms-audit-encrypt"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"]
      Resource = [aws_kms_key.audit_log.arn]
    }]
  })
}
