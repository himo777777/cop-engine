# COP Engine — Cloud Deploy Guide

## Arkitektur (AWS)

```
Internet → ALB (HTTPS) → ECS Fargate (2-6 tasks) → DocumentDB
                             ↓
                       CloudWatch Logs
```

**Region:** eu-north-1 (Stockholm) — låg latens för svenska sjukhus.

## Snabbstart

```bash
# 1. Kopiera och fyll i variabler
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars

# 2. Initiera Terraform backend (en gång)
./deploy.sh prod init

# 3. Granska plan
./deploy.sh prod plan

# 4. Skapa infrastruktur
./deploy.sh prod apply

# 5. Bygg + deploya Docker image
./deploy.sh prod release
```

## Kommandon

| Kommando | Beskrivning |
|----------|-------------|
| `./deploy.sh prod init` | Skapa S3 + DynamoDB för state |
| `./deploy.sh prod plan` | Visa Terraform plan |
| `./deploy.sh prod apply` | Applicera infrastruktur |
| `./deploy.sh prod push` | Bygg + push Docker image |
| `./deploy.sh prod release` | Push + ny ECS deployment |
| `./deploy.sh prod status` | Visa service status |
| `./deploy.sh prod logs` | Visa senaste loggar |
| `./deploy.sh prod destroy` | Riv ner allt |

## Kostnadskalkyl (prod)

| Resurs | Typ | ~Kostnad/mån |
|--------|-----|-------------|
| ECS Fargate | 2 tasks × 1vCPU × 2GB | ~$60 |
| DocumentDB | 2 × db.t3.medium | ~$120 |
| ALB | Application LB | ~$20 |
| NAT Gateway | 1 st | ~$35 |
| CloudWatch | 30 dagars loggar | ~$5 |
| ECR | Docker images | ~$2 |
| **Totalt** | | **~$242/mån** |

## CI/CD (GitHub Actions)

Placera `github-actions-ci-cd.yml` i `.github/workflows/cop-deploy.yml`.

**Secrets att konfigurera i GitHub:**
- `AWS_ROLE_ARN` — IAM role med OIDC trust

**Flöde:**
- PR → kör tester
- Push develop → test + deploy staging
- Push main → test + deploy prod

## Skalning

Auto-scaling: 2–6 tasks baserat på CPU (>70%) eller minne (>80%).
Solver-tunga anrop skalar automatiskt.
