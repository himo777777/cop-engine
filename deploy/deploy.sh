#!/usr/bin/env bash
# ============================================================================
# COP Engine — Deploy Script
# ============================================================================
# Användning:
#   ./deploy.sh [miljö] [kommando]
#
# Miljöer:  dev | staging | prod
# Kommandon:
#   init        — Första gången: skapa S3 + DynamoDB för Terraform state
#   plan        — Visa vad Terraform kommer göra
#   apply       — Kör Terraform apply
#   push        — Bygg + push Docker image till ECR
#   release     — Push + force new ECS deployment
#   destroy     — Riv ner allt (VARNING: produktionsdata försvinner!)
#   status      — Visa ECS service status
#   logs        — Visa senaste CloudWatch-loggar
# ============================================================================

set -euo pipefail

# --- Konfiguration ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV="${1:-prod}"
CMD="${2:-status}"
AWS_REGION="${AWS_REGION:-eu-north-1}"
PROJECT="cop"
ECR_REPO="${PROJECT}-api"

# --- Färger ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[COP]${NC} $1"; }
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1" >&2; }

# --- Verifiering ---
check_deps() {
    local missing=()
    for cmd in aws docker terraform jq; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done

    if [ ${#missing[@]} -gt 0 ]; then
        err "Saknade verktyg: ${missing[*]}"
        err "Installera dem och försök igen."
        exit 1
    fi

    # Verifiera AWS-credentials
    if ! aws sts get-caller-identity &>/dev/null; then
        err "Inga giltiga AWS-credentials. Kör: aws configure"
        exit 1
    fi

    ok "Alla beroenden verifierade"
}

# --- Terraform State Bootstrap ---
init_backend() {
    log "Skapar Terraform state backend..."

    # S3 bucket
    aws s3api create-bucket \
        --bucket "${PROJECT}-terraform-state" \
        --region "$AWS_REGION" \
        --create-bucket-configuration LocationConstraint="$AWS_REGION" \
        2>/dev/null || true

    aws s3api put-bucket-versioning \
        --bucket "${PROJECT}-terraform-state" \
        --versioning-configuration Status=Enabled

    aws s3api put-bucket-encryption \
        --bucket "${PROJECT}-terraform-state" \
        --server-side-encryption-configuration \
        '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

    # DynamoDB lock table
    aws dynamodb create-table \
        --table-name "${PROJECT}-terraform-locks" \
        --attribute-definitions AttributeName=LockID,AttributeType=S \
        --key-schema AttributeName=LockID,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST \
        --region "$AWS_REGION" \
        2>/dev/null || true

    ok "Terraform backend redo"

    # Terraform init
    cd "$SCRIPT_DIR"
    terraform init
    ok "Terraform initialiserad"
}

# --- Terraform Plan ---
tf_plan() {
    cd "$SCRIPT_DIR"
    log "Kör terraform plan (${ENV})..."
    terraform plan -var-file="terraform.tfvars" -out=tfplan
    ok "Plan klar — granska ovan"
}

# --- Terraform Apply ---
tf_apply() {
    cd "$SCRIPT_DIR"

    if [ ! -f tfplan ]; then
        warn "Ingen plan hittad — kör plan först"
        tf_plan
    fi

    log "Applicerar Terraform (${ENV})..."
    if [ "$ENV" = "prod" ]; then
        warn "⚠️  PRODUKTION — Bekräfta med 'yes'"
        terraform apply tfplan
    else
        terraform apply -auto-approve tfplan
    fi

    ok "Terraform apply klar"
    terraform output
}

# --- Docker Push ---
docker_push() {
    local ACCOUNT_ID
    ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
    local ECR_URL="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

    log "Bygger Docker image..."
    docker build -t "${ECR_REPO}:latest" "$PROJECT_DIR"
    ok "Docker image byggt"

    log "Loggar in till ECR..."
    aws ecr get-login-password --region "$AWS_REGION" | \
        docker login --username AWS --password-stdin \
        "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

    log "Taggar och pushar..."
    docker tag "${ECR_REPO}:latest" "${ECR_URL}:latest"
    docker tag "${ECR_REPO}:latest" "${ECR_URL}:$(date +%Y%m%d-%H%M%S)"
    docker push "${ECR_URL}:latest"
    docker push "${ECR_URL}:$(date +%Y%m%d-%H%M%S)"

    ok "Image pushat till ECR: ${ECR_URL}:latest"
}

# --- ECS Release ---
ecs_release() {
    docker_push

    local CLUSTER="${PROJECT}-cluster"
    local SERVICE="${PROJECT}-api"

    log "Triggar ny ECS deployment..."
    aws ecs update-service \
        --cluster "$CLUSTER" \
        --service "$SERVICE" \
        --force-new-deployment \
        --region "$AWS_REGION" \
        --query 'service.deployments[0].{status:status,desired:desiredCount,running:runningCount}' \
        --output table

    ok "Ny deployment startad — övervaka med: ./deploy.sh $ENV status"
}

# --- Destroy ---
tf_destroy() {
    cd "$SCRIPT_DIR"

    if [ "$ENV" = "prod" ]; then
        err "🛑 STOP! Du försöker riva PRODUKTION."
        echo -n "Skriv 'destroy-prod' för att bekräfta: "
        read -r confirm
        if [ "$confirm" != "destroy-prod" ]; then
            err "Avbrutet."
            exit 1
        fi
    fi

    warn "Rivning av alla resurser i ${ENV}..."
    terraform destroy -var-file="terraform.tfvars"
}

# --- Status ---
show_status() {
    local CLUSTER="${PROJECT}-cluster"
    local SERVICE="${PROJECT}-api"

    log "ECS Service Status:"
    aws ecs describe-services \
        --cluster "$CLUSTER" \
        --services "$SERVICE" \
        --region "$AWS_REGION" \
        --query 'services[0].{
            status:status,
            desired:desiredCount,
            running:runningCount,
            pending:pendingCount,
            deployments:deployments[*].{status:status,desired:desiredCount,running:runningCount,rollout:rolloutState}
        }' \
        --output table 2>/dev/null || warn "Service ej hittad — kör apply först"

    echo ""
    log "Senaste tasks:"
    aws ecs list-tasks \
        --cluster "$CLUSTER" \
        --service-name "$SERVICE" \
        --region "$AWS_REGION" \
        --query 'taskArns' \
        --output table 2>/dev/null || true
}

# --- Loggar ---
show_logs() {
    local LOG_GROUP="/ecs/${PROJECT}"

    log "Senaste 50 loggrader:"
    aws logs tail "$LOG_GROUP" \
        --since 1h \
        --format short \
        --region "$AWS_REGION" 2>/dev/null || warn "Inga loggar hittade"
}

# --- Main ---
main() {
    echo ""
    echo "═══════════════════════════════════════════"
    echo "   COP Engine — Cloud Deploy (${ENV})"
    echo "═══════════════════════════════════════════"
    echo ""

    check_deps

    case "$CMD" in
        init)    init_backend ;;
        plan)    tf_plan ;;
        apply)   tf_apply ;;
        push)    docker_push ;;
        release) ecs_release ;;
        destroy) tf_destroy ;;
        status)  show_status ;;
        logs)    show_logs ;;
        *)
            err "Okänt kommando: $CMD"
            echo "Användning: ./deploy.sh [dev|staging|prod] [init|plan|apply|push|release|destroy|status|logs]"
            exit 1
            ;;
    esac
}

main
