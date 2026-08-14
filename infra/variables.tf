variable "aws_region" {
  type        = string
  default     = "us-east-1"
  description = "AWS region for all resources. Must have Amazon Bedrock Claude model availability."
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "app_name" {
  type    = string
  default = "prior-auth-agent"
}

variable "ecr_image_uri" {
  type        = string
  default     = "PLACEHOLDER-account-id.dkr.ecr.us-east-1.amazonaws.com/prior-auth-agent:latest"
  description = "Full ECR image URI. Replace after the first docker push to ECR."
}

variable "llm_backend" {
  type        = string
  default     = "bedrock"
  description = "'bedrock' is required inside a PHI boundary. 'anthropic' for local/dev."
}

variable "bedrock_model_id" {
  type        = string
  default     = "us.anthropic.claude-opus-4-8-20251101-v1:0"
  description = "Amazon Bedrock model ID. Must be available in var.aws_region."
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "private_subnet_cidrs" {
  type    = list(string)
  default = ["10.0.1.0/24", "10.0.2.0/24"]
}

variable "public_subnet_cidrs" {
  type    = list(string)
  default = ["10.0.10.0/24", "10.0.11.0/24"]
}

variable "audit_retention_days" {
  type        = number
  default     = 2556 # 7 years — exceeds HIPAA 6-year minimum
  description = "Object-lock COMPLIANCE retention period in days for the audit archive bucket."
}

variable "kms_deletion_window_days" {
  type        = number
  default     = 30
  description = "KMS pending-deletion window in days. 30 days provides maximum recovery time."
}

variable "task_cpu" {
  type    = number
  default = 1024 # 1 vCPU
}

variable "task_memory" {
  type    = number
  default = 2048 # 2 GB
}

variable "desired_count" {
  type    = number
  default = 2 # minimum two tasks for AZ redundancy
}

variable "log_retention_days" {
  type    = number
  default = 90
}
