# ============================================================================
# COP Engine — Terraform Cloud Deploy (AWS ECS Fargate)
# ============================================================================
# Deploys the full COP stack:
#   - VPC with public/private subnets
#   - ECS Fargate cluster (cop-api)
#   - DocumentDB (MongoDB-compatible) cluster
#   - Application Load Balancer with HTTPS
#   - ECR repository for Docker images
#   - CloudWatch logging
#   - Auto-scaling (2–6 tasks)
# ============================================================================

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Remote state — byt till din egen S3-bucket
  backend "s3" {
    bucket         = "cop-terraform-state"
    key            = "prod/terraform.tfstate"
    region         = "eu-north-1"
    encrypt        = true
    dynamodb_table = "cop-terraform-locks"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "COP-Engine"
      Environment = var.environment
      ManagedBy   = "Terraform"
    }
  }
}

# ============================================================================
# DATA SOURCES
# ============================================================================

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

# ============================================================================
# VPC & NETWORKING
# ============================================================================

resource "aws_vpc" "cop" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = "${var.project}-vpc" }
}

resource "aws_internet_gateway" "cop" {
  vpc_id = aws_vpc.cop.id
  tags   = { Name = "${var.project}-igw" }
}

# Public subnets (ALB)
resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.cop.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${var.project}-public-${count.index + 1}" }
}

# Private subnets (ECS + DocumentDB)
resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.cop.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = { Name = "${var.project}-private-${count.index + 1}" }
}

# NAT Gateway (ECS tasks need outbound internet)
resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${var.project}-nat-eip" }
}

resource "aws_nat_gateway" "cop" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = "${var.project}-nat" }

  depends_on = [aws_internet_gateway.cop]
}

# Route tables
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.cop.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.cop.id
  }
  tags = { Name = "${var.project}-public-rt" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.cop.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.cop.id
  }
  tags = { Name = "${var.project}-private-rt" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ============================================================================
# SECURITY GROUPS
# ============================================================================

resource "aws_security_group" "alb" {
  name_prefix = "${var.project}-alb-"
  vpc_id      = aws_vpc.cop.id

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }

  ingress {
    description = "HTTP (redirect)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project}-alb-sg" }
}

resource "aws_security_group" "ecs" {
  name_prefix = "${var.project}-ecs-"
  vpc_id      = aws_vpc.cop.id

  ingress {
    description     = "From ALB"
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project}-ecs-sg" }
}

resource "aws_security_group" "docdb" {
  name_prefix = "${var.project}-docdb-"
  vpc_id      = aws_vpc.cop.id

  ingress {
    description     = "MongoDB from ECS"
    from_port       = 27017
    to_port         = 27017
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs.id]
  }

  tags = { Name = "${var.project}-docdb-sg" }
}

# ============================================================================
# ECR REPOSITORY
# ============================================================================

resource "aws_ecr_repository" "cop" {
  name                 = "${var.project}-api"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = { Name = "${var.project}-ecr" }
}

resource "aws_ecr_lifecycle_policy" "cop" {
  repository = aws_ecr_repository.cop.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# ============================================================================
# DOCUMENTDB (MongoDB-kompatibel)
# ============================================================================

resource "aws_docdb_subnet_group" "cop" {
  name       = "${var.project}-docdb-subnet"
  subnet_ids = aws_subnet.private[*].id
  tags       = { Name = "${var.project}-docdb-subnet" }
}

resource "aws_docdb_cluster_parameter_group" "cop" {
  name   = "${var.project}-params"
  family = "docdb5.0"

  parameter {
    name  = "tls"
    value = "enabled"
  }

  tags = { Name = "${var.project}-docdb-params" }
}

resource "aws_docdb_cluster" "cop" {
  cluster_identifier              = "${var.project}-docdb"
  engine                          = "docdb"
  master_username                 = var.docdb_username
  master_password                 = var.docdb_password
  db_subnet_group_name            = aws_docdb_subnet_group.cop.name
  db_cluster_parameter_group_name = aws_docdb_cluster_parameter_group.cop.name
  vpc_security_group_ids          = [aws_security_group.docdb.id]
  backup_retention_period         = 7
  preferred_backup_window         = "02:00-04:00"
  skip_final_snapshot             = var.environment != "prod"
  deletion_protection             = var.environment == "prod"

  tags = { Name = "${var.project}-docdb" }
}

resource "aws_docdb_cluster_instance" "cop" {
  count              = var.docdb_instance_count
  identifier         = "${var.project}-docdb-${count.index + 1}"
  cluster_identifier = aws_docdb_cluster.cop.id
  instance_class     = var.docdb_instance_class

  tags = { Name = "${var.project}-docdb-instance-${count.index + 1}" }
}

# ============================================================================
# ECS CLUSTER & TASK
# ============================================================================

resource "aws_ecs_cluster" "cop" {
  name = "${var.project}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = { Name = "${var.project}-cluster" }
}

resource "aws_cloudwatch_log_group" "cop" {
  name              = "/ecs/${var.project}"
  retention_in_days = 30
  tags              = { Name = "${var.project}-logs" }
}

resource "aws_iam_role" "ecs_task_execution" {
  name = "${var.project}-ecs-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_secrets" {
  name = "${var.project}-secrets-access"
  role = aws_iam_role.ecs_task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ssm:GetParameters",
        "secretsmanager:GetSecretValue"
      ]
      Resource = [
        aws_ssm_parameter.anthropic_key.arn,
        aws_ssm_parameter.docdb_uri.arn
      ]
    }]
  })
}

resource "aws_iam_role" "ecs_task" {
  name = "${var.project}-ecs-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

# Secrets via SSM Parameter Store
resource "aws_ssm_parameter" "anthropic_key" {
  name  = "/${var.project}/${var.environment}/anthropic-api-key"
  type  = "SecureString"
  value = var.anthropic_api_key

  tags = { Name = "${var.project}-anthropic-key" }
}

resource "aws_ssm_parameter" "docdb_uri" {
  name  = "/${var.project}/${var.environment}/docdb-uri"
  type  = "SecureString"
  value = "mongodb://${var.docdb_username}:${var.docdb_password}@${aws_docdb_cluster.cop.endpoint}:27017/?tls=true&retryWrites=false"

  tags = { Name = "${var.project}-docdb-uri" }
}

resource "aws_ecs_task_definition" "cop" {
  family                   = "${var.project}-api"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name  = "cop-api"
    image = "${aws_ecr_repository.cop.repository_url}:${var.image_tag}"

    portMappings = [{
      containerPort = var.container_port
      hostPort      = var.container_port
      protocol      = "tcp"
    }]

    environment = [
      { name = "COP_PORT",    value = tostring(var.container_port) },
      { name = "COP_WORKERS", value = tostring(var.gunicorn_workers) },
      { name = "ENVIRONMENT", value = var.environment },
    ]

    secrets = [
      {
        name      = "ANTHROPIC_API_KEY"
        valueFrom = aws_ssm_parameter.anthropic_key.arn
      },
      {
        name      = "MONGO_URI"
        valueFrom = aws_ssm_parameter.docdb_uri.arn
      }
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.cop.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "cop-api"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "curl -f http://localhost:${var.container_port}/health || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
  }])

  tags = { Name = "${var.project}-task" }
}

# ============================================================================
# APPLICATION LOAD BALANCER
# ============================================================================

resource "aws_lb" "cop" {
  name               = "${var.project}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  tags = { Name = "${var.project}-alb" }
}

resource "aws_lb_target_group" "cop" {
  name        = "${var.project}-tg"
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.cop.id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = "/health"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 10
    interval            = 30
    matcher             = "200"
  }

  tags = { Name = "${var.project}-tg" }
}

# HTTP → HTTPS redirect
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.cop.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# HTTPS listener (kräver ACM-certifikat)
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.cop.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.cop.arn
  }
}

# ============================================================================
# ECS SERVICE + AUTO-SCALING
# ============================================================================

resource "aws_ecs_service" "cop" {
  name            = "${var.project}-api"
  cluster         = aws_ecs_cluster.cop.id
  task_definition = aws_ecs_task_definition.cop.arn
  desired_count   = var.min_tasks
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.cop.arn
    container_name   = "cop-api"
    container_port   = var.container_port
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.https]

  tags = { Name = "${var.project}-service" }
}

# Auto-scaling
resource "aws_appautoscaling_target" "cop" {
  max_capacity       = var.max_tasks
  min_capacity       = var.min_tasks
  resource_id        = "service/${aws_ecs_cluster.cop.name}/${aws_ecs_service.cop.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "cpu" {
  name               = "${var.project}-cpu-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.cop.resource_id
  scalable_dimension = aws_appautoscaling_target.cop.scalable_dimension
  service_namespace  = aws_appautoscaling_target.cop.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 70.0
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}

resource "aws_appautoscaling_policy" "memory" {
  name               = "${var.project}-memory-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.cop.resource_id
  scalable_dimension = aws_appautoscaling_target.cop.scalable_dimension
  service_namespace  = aws_appautoscaling_target.cop.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageMemoryUtilization"
    }
    target_value       = 80.0
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}
