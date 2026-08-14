# ── Audit log archive bucket ───────────────────────────────────────────────────
#
# Provides external durability for the hash-chained audit log (DECISIONS.md D7/D8).
# D7 identifies the gap: an attacker who can rewrite the entire local log file can
# rebuild the chain. Object lock at the storage layer makes that impossible —
# individual objects cannot be modified or deleted before the retention period expires.
#
# Object lock mode: COMPLIANCE (not GOVERNANCE).
#   GOVERNANCE mode allows privileged IAM users to override retention. COMPLIANCE
#   mode makes the retention floor unoverridable by any IAM principal, including root.
#   For records that back clinical determinations under HIPAA, COMPLIANCE is required.
#
# Retention: 7 years (2556 days). HIPAA requires 6-year minimum; 7 years is standard
# practice to cover edge cases in the "last effective date" interpretation.
#
# Encryption: SSE-KMS with the CMK from kms.tf. The bucket default encryption means
# every PutObject is encrypted automatically — the application does not need to
# specify an encryption header per write.
#
# Versioning: required prerequisite for object lock.
# Lifecycle: transitions to Glacier IR after 90 days — lower storage cost, still
# sub-millisecond retrieval for audit review.
#
# Bucket name: globally unique; append your AWS account ID to the base name.
# Replace PLACEHOLDER-account-id below.

resource "aws_s3_bucket" "audit_archive" {
  bucket = "${var.app_name}-audit-PLACEHOLDER-account-id"

  tags = {
    Name       = "${var.app_name}-audit-archive"
    DataClass  = "PHI-adjacent"
    Regulation = "HIPAA"
  }
}

resource "aws_s3_bucket_versioning" "audit_archive" {
  bucket = aws_s3_bucket.audit_archive.id

  versioning_configuration {
    status = "Enabled" # required for object lock
  }
}

resource "aws_s3_bucket_object_lock_configuration" "audit_archive" {
  bucket = aws_s3_bucket.audit_archive.id

  rule {
    default_retention {
      mode = "COMPLIANCE" # unoverridable — no IAM principal can shorten this retention
      days = var.audit_retention_days
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit_archive" {
  bucket = aws_s3_bucket.audit_archive.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.audit_log.arn
    }
    bucket_key_enabled = true # reduces KMS API call volume for high-write-frequency buckets
  }
}

resource "aws_s3_bucket_public_access_block" "audit_archive" {
  bucket = aws_s3_bucket.audit_archive.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "audit_archive" {
  bucket = aws_s3_bucket.audit_archive.id

  rule {
    id     = "transition-to-glacier-ir"
    status = "Enabled"

    transition {
      days          = 90
      storage_class = "GLACIER_IR" # Glacier Instant Retrieval: ~60% cost reduction, <ms retrieval
    }
    # No expiration rule: object lock prevents deletion before the retention period.
  }
}

# ── Bucket policy ──────────────────────────────────────────────────────────────

resource "aws_s3_bucket_policy" "audit_archive" {
  bucket = aws_s3_bucket.audit_archive.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Deny any request not using TLS — audit log writes must be encrypted in transit.
        Sid       = "DenyNonTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.audit_archive.arn,
          "${aws_s3_bucket.audit_archive.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
      {
        Sid       = "AllowTaskRoleWrite"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.ecs_task.arn }
        Action    = ["s3:PutObject", "s3:GetObject"]
        Resource  = "${aws_s3_bucket.audit_archive.arn}/determinations/*"
      },
    ]
  })
}
