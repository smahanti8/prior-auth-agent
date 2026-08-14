# ── ECS Fargate service ────────────────────────────────────────────────────────
#
# ECS Fargate, not EKS. See DECISIONS.md D14 for the full rationale.
# Short version: cluster orchestration complexity isn't warranted at this scale,
# and the Kubernetes operator surface doesn't belong in a compliance-focused
# reference architecture targeting this workload size.
#
# All ECS tasks run in private subnets — no public IP on task ENIs.
# Inbound requests reach the service only through the ALB.

resource "aws_ecs_cluster" "main" {
  name = var.app_name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.app_name}"
  retention_in_days = var.log_retention_days
  # PHI boundary reminder: this log group must not retain raw FHIR bundle content.
  # Error traces from evidence_extractor and determination may contain bundle fragments.
  # Add a log processor or CloudWatch metric filter to redact before retention.
}

# ── Task definition ────────────────────────────────────────────────────────────

resource "aws_ecs_task_definition" "app" {
  family                   = var.app_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name      = var.app_name
    image     = var.ecr_image_uri
    essential = true

    portMappings = [{
      containerPort = 8000
      protocol      = "tcp"
    }]

    environment = [
      { name = "LLM_BACKEND",      value = var.llm_backend },
      { name = "BEDROCK_REGION",   value = var.aws_region },
      { name = "BEDROCK_MODEL_ID", value = var.bedrock_model_id },
      # ANTHROPIC_API_KEY is intentionally absent: the Bedrock path uses the
      # task IAM role for credentials, not a static API key.
      # If LLM_BACKEND=anthropic is needed (e.g., dev), inject the key via
      # Secrets Manager rather than an environment variable.
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
}

# ── ALB ────────────────────────────────────────────────────────────────────────
# ALB in public subnets terminates TLS. The ECS security group allows inbound
# only from the ALB security group — the service is not directly reachable.

resource "aws_security_group" "alb" {
  name        = "${var.app_name}-alb-sg"
  description = "ALB — HTTPS inbound from internet, forward to ECS on port 8000"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.app_name}-alb-sg" }
}

resource "aws_security_group" "ecs_task" {
  name        = "${var.app_name}-ecs-sg"
  description = "ECS tasks — inbound from ALB only, outbound to VPC endpoints and NAT"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.app_name}-ecs-sg" }
}

resource "aws_lb" "main" {
  name               = "${var.app_name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
}

resource "aws_lb_target_group" "app" {
  name        = "${var.app_name}-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip" # required for Fargate awsvpc networking

  health_check {
    path                = "/health"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"

  # Replace with your ACM certificate ARN before applying.
  # certificate_arn = "arn:aws:acm:PLACEHOLDER-region:PLACEHOLDER-account:certificate/PLACEHOLDER"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

# ── ECS service ────────────────────────────────────────────────────────────────

resource "aws_ecs_service" "app" {
  name            = var.app_name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_task.id]
    assign_public_ip = false # private subnet — no public IP on task ENI
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = var.app_name
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.https]
}
