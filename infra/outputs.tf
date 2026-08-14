output "alb_dns_name" {
  value       = aws_lb.main.dns_name
  description = "DNS name of the Application Load Balancer. Point your CNAME record here."
}

output "ecr_repository_url" {
  value       = aws_ecr_repository.app.repository_url
  description = "ECR repository URL — use as the base for var.ecr_image_uri."
}

output "audit_bucket_arn" {
  value       = aws_s3_bucket.audit_archive.arn
  description = "ARN of the audit archive bucket. Reference in IAM policies."
}

output "audit_bucket_name" {
  value = aws_s3_bucket.audit_archive.bucket
}

output "kms_key_arn" {
  value       = aws_kms_key.audit_log.arn
  description = "ARN of the customer-managed KMS key for audit log encryption."
}

output "kms_key_id" {
  value = aws_kms_key.audit_log.key_id
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  value = aws_ecs_service.app.name
}

output "task_role_arn" {
  value       = aws_iam_role.ecs_task.arn
  description = "IAM role assumed by the running ECS task. Grant additional resource access here."
}
