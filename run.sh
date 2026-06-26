#!/usr/bin/env bash
# Cue 실행 스크립트 — venv 준비 + 의존성 설치 + 서버 기동
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "▶ venv 생성..."
  python3 -m venv .venv
fi
source .venv/bin/activate

echo "▶ 의존성 설치..."
pip install -q -U pip
pip install -q -r requirements.txt

if [ ! -f ".env" ]; then
  echo "▶ .env 없음 → .env.example 복사 (키 없이 mock 모드로 동작)"
  cp .env.example .env
fi

echo "▶ http://localhost:8000 에서 Cue 시작"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
