#!/usr/bin/env bash
# 服务器部署脚本：拉取 main 分支并重建容器。
# 用法（在服务器 /www/wwwroot/dianzigoubot 目录执行）: bash deploy.sh
set -euo pipefail

git fetch origin
git reset --hard origin/main
docker compose up -d --build
docker compose logs --tail=30 bot
