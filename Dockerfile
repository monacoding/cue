# Cue — 배포 컨테이너 (ffmpeg 포함, 결정론적 조립/영상에 필요)
FROM python:3.12-slim

# ffmpeg + ffprobe (영상 조립·인코딩·Ken Burns·음악 베드에 필수)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -U pip && pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY CLAUDE.md README.md ./

# 한글 폰트 경로 (컨테이너는 macOS 폰트가 없으므로 Noto CJK 사용)
ENV CUE_FONT=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc

EXPOSE 8000
# 키 없이도 mock/오프라인으로 동작. 실 모델은 -e ANTHROPIC_API_KEY=... 등으로 주입.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
