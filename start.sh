#!/bin/bash
# ============================================================
# COP Engine — Startup Script
# ============================================================
# Användning:
#   ./start.sh              # Starta alla tjänster
#   ./start.sh dev          # Utvecklingsläge (utan nginx)
#   ./start.sh api-only     # Bara API (ingen MongoDB/nginx)
#   ./start.sh stop         # Stoppa allt
#   ./start.sh logs         # Visa loggar
#   ./start.sh status       # Visa status
# ============================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

COMPOSE="docker compose"

# Skapa .env om den saknas
if [ ! -f .env ]; then
    echo -e "${YELLOW}⚠️  .env saknas — kopierar från .env.example${NC}"
    cp .env.example .env
    echo -e "${YELLOW}   Redigera .env med dina inställningar${NC}"
fi

# Skapa SSL-mapp (för nginx)
mkdir -p ssl

case "${1:-up}" in

    up|start)
        echo -e "${GREEN}🚀 Startar COP Engine (alla tjänster)...${NC}"
        $COMPOSE up -d --build
        echo ""
        echo -e "${GREEN}✅ COP Engine körs!${NC}"
        echo -e "   API:       http://localhost:${COP_API_PORT:-8000}/health"
        echo -e "   Dashboard: http://localhost:${NGINX_HTTP_PORT:-80}/dashboard"
        echo -e "   MongoDB:   localhost:${MONGO_PORT:-27017}"
        ;;

    dev)
        echo -e "${GREEN}🔧 Startar COP Engine (utvecklingsläge)...${NC}"
        $COMPOSE up -d --build cop-api cop-mongo
        echo ""
        echo -e "${GREEN}✅ Dev-läge körs!${NC}"
        echo -e "   API: http://localhost:${COP_API_PORT:-8000}/health"
        ;;

    api-only|api)
        echo -e "${GREEN}⚡ Startar bara COP API...${NC}"
        $COMPOSE up -d --build cop-api
        echo ""
        echo -e "${GREEN}✅ API körs!${NC}"
        echo -e "   http://localhost:${COP_API_PORT:-8000}/health"
        ;;

    stop|down)
        echo -e "${YELLOW}🛑 Stoppar COP Engine...${NC}"
        $COMPOSE down
        echo -e "${GREEN}✅ Stoppat${NC}"
        ;;

    restart)
        echo -e "${YELLOW}🔄 Startar om COP Engine...${NC}"
        $COMPOSE down
        $COMPOSE up -d --build
        echo -e "${GREEN}✅ Omstart klar${NC}"
        ;;

    logs)
        $COMPOSE logs -f --tail=100 ${2:-}
        ;;

    status)
        echo -e "${GREEN}📊 COP Engine Status${NC}"
        echo "================================"
        $COMPOSE ps
        echo ""
        echo "--- Health Check ---"
        curl -s http://localhost:${COP_API_PORT:-8000}/health 2>/dev/null | python3 -m json.tool || echo -e "${RED}API ej nåbar${NC}"
        ;;

    test)
        echo -e "${GREEN}🧪 Testar COP Engine...${NC}"
        echo ""
        echo "1. Health check..."
        curl -s http://localhost:${COP_API_PORT:-8000}/health | python3 -m json.tool
        echo ""
        echo "2. Generate test schedule..."
        curl -s -X POST http://localhost:${COP_API_PORT:-8000}/schedule/generate \
            -H "Content-Type: application/json" \
            -d '{"config_id": "kristianstad", "num_weeks": 2, "time_limit_seconds": 30}' \
            | python3 -m json.tool
        echo ""
        echo -e "${GREEN}✅ Test klart${NC}"
        ;;

    clean)
        echo -e "${RED}🗑️  Rensar alla COP-data och volymer...${NC}"
        read -p "Är du säker? (y/N) " confirm
        if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
            $COMPOSE down -v
            echo -e "${GREEN}✅ Rensat${NC}"
        else
            echo "Avbrutet"
        fi
        ;;

    *)
        echo "COP Engine — Startup Script"
        echo ""
        echo "Användning: ./start.sh [kommando]"
        echo ""
        echo "Kommandon:"
        echo "  up/start    Starta alla tjänster (default)"
        echo "  dev         Utvecklingsläge (API + MongoDB)"
        echo "  api-only    Bara API"
        echo "  stop/down   Stoppa alla tjänster"
        echo "  restart     Starta om"
        echo "  logs [svc]  Visa loggar (valfri: cop-api, cop-mongo, cop-nginx)"
        echo "  status      Visa status och health check"
        echo "  test        Kör API-tester"
        echo "  clean       Radera alla data och volymer"
        ;;
esac
