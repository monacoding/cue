# RunPod 배포 가이드 — Qwen-Image (베이스 먼저, LoRA는 나중에)

> 목표: RunPod Serverless에 Qwen-Image 워커를 띄우고, Cue 앱에 연결해서 실제 이미지를 생성한다.
> 지금은 **베이스 모델만** 올린다. 명동 LoRA는 학습이 끝나면 볼륨에 파일만 추가하면 자동으로 붙는다(코드 수정 불필요).
>
> 전체 흐름: **A. 네트워크 볼륨에 모델 다운로드 → B. 워커 이미지 배포 → C. 엔드포인트 생성 → D. Cue 연결·테스트**

---

## 0. 준비물 (계정/키)

1. **RunPod 계정** — https://runpod.io 가입 + 결제수단 등록(크레딧 충전). 서버리스는 초 단위 과금.
2. **RunPod API Key** — RunPod 콘솔 → Settings → API Keys → `+ Create` → 키 복사. (Cue가 이 키로 호출)
3. **이미지 레지스트리 중 택1** (워커 도커 이미지를 RunPod이 pull 할 곳):
   - **추천: GitHub 빌드** — RunPod이 GitHub 레포의 Dockerfile을 직접 빌드. Mac(Apple Silicon)에서 CUDA 이미지 크로스빌드하는 고생을 피함. Cue 레포가 이미 GitHub(`monacoding/cue`)에 있어 바로 가능.
   - 대안: **Docker Hub** — 로컬에서 빌드·푸시. Mac에서는 `--platform linux/amd64` 필요(아래 B-2 참고).

> 비용 감각: Qwen-Image(20B급)는 24GB+ VRAM 권장 → A5000/A6000/L40/4090 계열. 콜드스타트에 모델 로딩 1~3분. 실사용 시 워커 1개 warm 유지 권장.

---

## A. 네트워크 볼륨에 Qwen-Image 베이스 모델 올리기

워커 이미지에는 모델을 **굽지 않는다**(너무 큼). 대신 Network Volume에 한 번 받아두고 엔드포인트에 마운트한다.

### A-1. 볼륨 생성
RunPod 콘솔 → **Storage → Network Volume → New** :
- Region: GPU가 많은 곳(예: `US-OR` 등 — 엔드포인트도 같은 region이어야 마운트됨)
- Size: **100GB**(베이스 ~60GB + LoRA 여유). 부족하면 키워도 됨.
- 이름: `cue-qwen`

### A-2. 임시 GPU Pod로 모델 다운로드
볼륨에 파일을 넣으려면 그 볼륨을 마운트한 Pod가 한 번 필요하다.

RunPod 콘솔 → **Pods → Deploy** :
- 위에서 만든 Network Volume `cue-qwen` 선택(→ `/workspace` 또는 `/runpod-volume`에 마운트됨; 마운트 경로 확인)
- 아무 저렴한 GPU(다운로드만 할 거라 작은 것도 OK)
- 템플릿: `runpod/pytorch` 계열

Pod의 웹터미널(또는 SSH)에서 — **마운트 경로가 `/workspace`라고 가정**(콘솔에서 실제 경로 확인 후 맞춰서):

```
pip install -U "huggingface_hub[cli]"
huggingface-cli download Qwen/Qwen-Image --local-dir /workspace/Qwen-Image
mkdir -p /workspace/loras
ls -R /workspace/Qwen-Image | head
```

> 받고 나면 `/workspace/Qwen-Image/` 아래에 `transformer/ text_encoder/ vae/ tokenizer/ scheduler/ ...` 서브폴더가 보여야 한다. handler.py 가 정확히 이 구조(`MODEL_DIR/transformer` 등)를 기대한다.
> 다운로드 끝나면 이 임시 Pod는 **Terminate**(볼륨은 남는다). LoRA 파일도 나중에 같은 방식으로 `/workspace/loras/myeongdong.safetensors`에 올리면 됨.

---

## B. 워커 이미지 배포

### 경로 1 (추천) — RunPod GitHub 빌드
1. 변경분 푸시: `runpod/` 폴더가 GitHub에 올라가 있어야 함.
   ```
   cd ~/Cue/ad-factory
   git add runpod && git commit -m "RunPod Qwen worker" && git push
   ```
2. RunPod 콘솔 → **Serverless → New Endpoint → Import Git Repository** (GitHub 연동/인증)
   - Repo: `monacoding/cue`
   - **Dockerfile path / context**: `ad-factory/runpod` (Dockerfile 위치)
   - RunPod이 빌드를 수행 → 완료되면 그대로 C로 진행(엔드포인트 설정이 이어짐).

### 경로 2 (대안) — Docker Hub 수동 빌드/푸시
Mac(Apple Silicon)이면 **반드시 amd64 타깃**으로:
```
cd ~/Cue/ad-factory/runpod
docker buildx build --platform linux/amd64 -t YOURUSER/cue-qwen:latest --push .
```
(`YOURUSER`는 Docker Hub 아이디. 처음이면 `docker login` 먼저.)

---

## C. Serverless 엔드포인트 생성

RunPod 콘솔 → **Serverless → New Endpoint** :
- **Container Image**: 경로1이면 빌드 결과 / 경로2면 `YOURUSER/cue-qwen:latest`
- **Network Volume**: `cue-qwen` 선택 → 컨테이너의 `/runpod-volume`에 마운트됨
- **GPU**: 24GB+ (A5000 / A6000 / L40 / 4090)
- **Env Variables** (Dockerfile 기본값과 마운트 경로 맞추기 — **중요**):
  ```
  MODEL_DIR  = /runpod-volume/Qwen-Image
  LORA_PATH  = /runpod-volume/loras/myeongdong.safetensors   # 아직 파일 없어도 됨(베이스만 돌아감)
  LORA_ALPHA = 1.0
  ```
  > 주의: A단계에서 모델을 `/workspace/Qwen-Image`에 받았더라도, **엔드포인트에선 같은 볼륨이 `/runpod-volume`로 마운트**된다. 그래서 `MODEL_DIR`은 `/runpod-volume/Qwen-Image`가 맞다.
- **Workers**: Min 0 (유휴 시 0으로 비용↓) / Max 1~2. 자주 쓰면 Min 1로 warm 유지.
- 생성 후 **Endpoint ID** 복사.

---

## D. Cue 앱에 연결 + 테스트

`~/Cue/ad-factory/.env` 에 (주석 풀고 값 채우기):
```
RUNPOD_API_KEY=<RunPod API 키>
RUNPOD_QWEN_ENDPOINT=<Endpoint ID>
# 선택: 워커 입력 파라미터 덮어쓰기
RUNPOD_QWEN_INPUT={"negative_prompt":"blurry, lowres","num_inference_steps":30,"guidance_scale":4}
# LoRA 학습 끝나면(트리거워드) — 지금은 비워둠
RUNPOD_QWEN_STYLE=
```

앱 실행 후 연결 자가진단:
```
cd ~/Cue/ad-factory && ./run.sh
```
다른 터미널에서:
```
curl http://localhost:8000/api/providers/qwen/test
```
→ `"real_call_ok": true` 면 연결 성공. (첫 호출은 콜드스타트로 1~3분 걸릴 수 있음.)

실패 시 응답의 `error` / `preview`(워커 output 구조)를 보고:
- 이미지 키가 특이하면 `RUNPOD_QWEN_INPUT`/핸들러 응답 키 매핑 조정
- HTTP 4xx/5xx면 RunPod 콘솔 → 엔드포인트 → **Logs**에서 워커 에러 확인(주로 모델 로드 경로/`from_pretrained` 인자)

성공하면 Studio에서 **Image model = Qwen** 선택하고 이미지 생성하면 RunPod 경로로 나간다.

---

## E. (나중에) 명동 LoRA 붙이기

학습 끝나면:
1. `myeongdong.safetensors`를 볼륨의 `/loras/`에 업로드(임시 Pod 다시 띄워서 복사, 또는 학습을 같은 볼륨에서 했으면 이미 있음).
2. 엔드포인트 Env에 `LORA_PATH`가 그 파일을 가리키는지 확인(기본값이 이미 맞음).
3. `.env`의 `RUNPOD_QWEN_STYLE=`에 학습 트리거워드(예: `myeongdong_street`) 입력 → 모든 Qwen 프롬프트에 자동 추가됨.
4. 워커 재시작(엔드포인트가 새 워커를 띄우면 콜드스타트에서 LoRA 로드). 코드 변경 불필요.

---

## 점검 체크리스트
- [ ] handler.py `_load_pipe()`의 `from_pretrained` / `load_lora` 2줄이 실제 DiffSynth-Studio 버전·학습 스크립트와 일치하는가 (버전별로 인자 다름 — 학습 시 쓴 추론 스크립트에서 복사)
- [ ] Network Volume region == Endpoint region
- [ ] `MODEL_DIR`이 `/runpod-volume/Qwen-Image` (마운트 경로 기준)
- [ ] `curl …/qwen/test` → real_call_ok: true
