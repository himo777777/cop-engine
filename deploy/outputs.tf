# ============================================================================
# COP Engine — Terraform Outputs
# ============================================================================

output "alb_dns_name" {
  description = "ALB DNS-namn (peka din domän hit med CNAME)"
  value       = aws_lb.cop.dns_name
}

output "alb_zone_id" {
  description = "ALB hosted zone ID (för Route53 alias)"
  value       = aws_lb.cop.zone_id
}

output "ecr_repository_url" {
  description = "ECR repository URL (för docker push)"
  value       = aws_ecr_repository.cop.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster namn"
  value       = aws_ecs_cluster.cop.name
}

output "ecs_service_name" {
  description = "ECS service namn"
  value       = aws_ecs_service.cop.name
}

output "docdb_endpoint" {
  description = "DocumentDB cluster endpoint"
  value       = aws_docdb_cluster.cop.endpoint
  sensitive   = true
}

output "cloudwatch_log_group" {
  description = "CloudWatch log group"
  value       = aws_cloudwatch_log_group.cop.name
}

output "vpc_id" {
  description = "VPC ID"
  value       = aws_vpc.cop.id
}

output "api_url" {
  description = "Full API URL"
  value       = "https://${aws_lb.cop.dns_name}"
}

output "deploy_command" {
  description = "Kommando för att deploya ny version"
  value       = <<-EOT
    # 1. Bygg och tagga Docker image
    docker build -t ${aws_ecr_repository.cop.repository_url}:latest ../

    # 2. Logga in till ECR
    aws ecr get-login-password --region ${var.aws_region} | docker login --username AWS --password-stdin ${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com

    # 3. Push image
    docker push ${aws_ecr_repository.cop.repository_url}:latest

    # 4. Uppdatera ECS service
    aws ecs update-service --cluster ${aws_ecs_cluster.cop.name} --service ${aws_ecs_service.cop.name} --force-new-deployment
  EOT
}
