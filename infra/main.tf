terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
  }

  # Replace with your actual S3 backend configuration before applying.
  # Never commit real bucket names, account IDs, or region values here.
  backend "s3" {
    bucket  = "PLACEHOLDER-tfstate-bucket"
    key     = "prior-auth-agent/terraform.tfstate"
    region  = "us-east-1"
    encrypt = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.app_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }

  # Credentials: use AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY environment
  # variables, an EC2/ECS instance profile, or AWS SSO.
  # Never hardcode credentials in Terraform files.
}
