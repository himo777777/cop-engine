# ============================================================================
# COP Engine — Terraform Variables
# ============================================================================

# ---------- Projekt ----------

variable "project" {
  description = "Projektnamn (prefix för alla resurser)"
  type        = string
  default     = "cop"
}

variable "environment" {
  description = "Miljö: dev, staging, prod"
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment måste vara dev, staging eller prod."
  }
}

# ---------- AWS ----------

variable "aws_region" {
  description = "AWS-region (eu-north-1 = Stockholm)"
  type        = string
  default     = "eu-north-1"
}

variable "vpc_cidr" {
  description = "VPC CIDR-block"
  type        = string
  default     = "10.0.0.0/16"
}

variable "allowed_cidrs" {
  description = "CIDR-block som tillåts nå ALB (0.0.0.0/0 = alla)"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

# ---------- ECS / Fargate ----------

variable "container_port" {
  description = "Port som COP API lyssnar på"
  type        = number
  default     = 8000
}

variable "task_cpu" {
  description = "Fargate CPU (1024 = 1 vCPU)"
  type        = string
  default     = "1024"
}

variable "task_memory" {
  description = "Fargate minne (MB)"
  type        = string
  default     = "2048"
}

variable "gunicorn_workers" {
  description = "Antal gunicorn workers"
  type        = number
  default     = 2
}

variable "min_tasks" {
  description = "Minimum antal ECS tasks"
  type        = number
  default     = 2
}

variable "max_tasks" {
  description = "Maximum antal ECS tasks (auto-scaling)"
  type        = number
  default     = 6
}

variable "image_tag" {
  description = "Docker image tag att deploya"
  type        = string
  default     = "latest"
}

# ---------- DocumentDB ----------

variable "docdb_username" {
  description = "DocumentDB master username"
  type        = string
  default     = "copadmin"
  sensitive   = true
}

variable "docdb_password" {
  description = "DocumentDB master password"
  type        = string
  sensitive   = true
}

variable "docdb_instance_class" {
  description = "DocumentDB instanstyp"
  type        = string
  default     = "db.t3.medium"
}

variable "docdb_instance_count" {
  description = "Antal DocumentDB instanser"
  type        = number
  default     = 2
}

# ---------- Secrets ----------

variable "anthropic_api_key" {
  description = "Anthropic API-nyckel för COP LLM Agent"
  type        = string
  sensitive   = true
}

# ---------- SSL ----------

variable "acm_certificate_arn" {
  description = "ACM Certificate ARN för HTTPS"
  type        = string
}
