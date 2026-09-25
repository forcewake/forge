resource "aws_vpc" "platform" {
  cidr_block           = "10.40.0.0/16"
  enable_dns_hostnames = true
}

resource "aws_ecs_cluster" "services" {
  name = "platform-services"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

output "cluster_name" {
  value = aws_ecs_cluster.services.name
}
