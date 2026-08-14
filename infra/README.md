# infra/ — Reference Architecture (Documentation-Grade)

> **Not a provisioned system.** All account IDs, region names, and ARNs in these
> files use `PLACEHOLDER-*` values. This Terraform documents the infrastructure
> intent and compliance reasoning for a PHI-boundary deployment. Do not run
> `terraform apply` without replacing every placeholder, reviewing the KMS key
> policy, and running `terraform plan` against your own account first.

## What this provisions (when instantiated)

| Component | Resource | Purpose |
|-----------|----------|---------|
| Network boundary | VPC, private subnets, NAT, VPC endpoints | Isolate compute; keep Bedrock/S3/ECR traffic off the NAT path |
| Container service | ECS Fargate cluster + service | Run the FastAPI pipeline; no Kubernetes (see DECISIONS.md D14) |
| Container registry | ECR | Immutable image tags; CVE scan on push; CMK-encrypted |
| Ingress | ALB in public subnets | TLS termination; ECS service unreachable from internet directly |
| Audit archival | S3 + object lock (COMPLIANCE) | Extend D7/D8 hash chain with immutable external storage |
| Encryption | Customer-managed KMS key | Customer holds the key; AWS cannot access key material |
| IAM | Execution role + task role | Least-privilege: container cannot manage its own key |

## Files

| File | What it contains |
|------|-----------------|
| `main.tf` | Provider + S3 backend (replace backend config before applying) |
| `variables.tf` | All input variables with placeholder defaults |
| `outputs.tf` | Exported resource identifiers (ARNs, DNS names) |
| `vpc.tf` | VPC, subnets, NAT gateway, VPC endpoints |
| `ecs.tf` | Fargate cluster, task definition, ALB, security groups |
| `ecr.tf` | Container registry + lifecycle policy |
| `iam.tf` | Execution role + task role, least-privilege policies |
| `kms.tf` | Customer-managed key, separated admin/user key policy |
| `s3.tf` | Audit archive bucket, object lock, SSE-KMS, lifecycle |

## Prerequisites

1. Terraform >= 1.7
2. AWS account with Amazon Bedrock Claude model access enabled in your target region
3. An ACM certificate ARN — add to `aws_lb_listener.https.certificate_arn` in `ecs.tf`
4. A container image pushed to ECR — set `var.ecr_image_uri` after first push
5. Replace all `PLACEHOLDER-account-id` and `PLACEHOLDER-*-role` values in `kms.tf` and `s3.tf`

## Usage

```bash
cd infra/
terraform init
terraform plan -var="aws_region=us-east-1"   # review before applying
# terraform apply                             # only after replacing all placeholders
```

## Compliance notes

See the main [README — What changes inside a PHI boundary](../README.md#what-changes-inside-a-phi-boundary)
for the full rationale. Key operational reminders:

- **LLM_BACKEND=bedrock** is injected as an ECS environment variable. All model
  calls use the task role's AWS credentials, not an Anthropic API key. PHI in
  prompts stays within your AWS compliance perimeter.
- **Object lock COMPLIANCE mode** makes audit log objects unmodifiable by any IAM
  principal including root. Do not change to GOVERNANCE mode without reviewing
  DECISIONS.md D7/D8.
- **CloudWatch log group** (`/ecs/prior-auth-agent`) must not retain raw FHIR
  bundle content. Add a log processor or metric filter to redact PHI fields.
