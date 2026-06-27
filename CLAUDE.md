# CLAUDE.md — AI 광고 제작 파이프라인 (Cue)
> Claude Code 프로젝트 메모리. 이 문서는 제품·아키텍처·파이프라인·연결 모델/API·빌드 순서를 정의한다.
> 모델 문자열과 가격은 빠르게 변하므로, **빌드 시점에 각 공식 문서로 최종 확인**한다 (값은 2026년 6월 기준 참고치).

---

## 1. 제품 개요
제품 정보를 입력하면 **콘티 → 이미지 → 영상 → 편집**까지 자동 생성하되, 각 생성 단계마다 사람이 개입해 수정하는 광고 제작 도구.

**핵심 철학: 가치는 생성(generation)이 아니라 광고 지능(ad intelligence)에 있다.**
영상·이미지 모델 API는 누구나 동일하게 호출한다. 차별점은 (a) 어떤 훅·컨셉이 전환되는지 판단하는 컨셉 엔진과 (b) 사람이 빠르게 원하는 결과로 수렴시키는 human-in-the-loop UX다. 생성 모듈은 교체 가능한 부품으로 취급한다.

**출력 포맷 3종 (공유 코어 + 포맷별 어댑터):**
- `static_image` — 정적 이미지 광고 (배너·SNS 피드). 파이프라인 1~6단계.
- `ugc_video` — UGC/토킹헤드 영상 광고. 1~9단계.
- `cinematic_video` — 시네마틱/제품 데모 영상. 1~9단계.

세 포맷은 1~3단계(브리프·스펙·콘티)를 100% 공유하고, 4단계부터 갈라진다.

---

## 2. 아키텍처 원칙
1. **Provider 추상화** — 모든 생성 모델은 `providers/`의 공통 인터페이스 뒤에 둔다. 모델 교체가 코드 한 줄이 되도록.
2. **Human-in-the-loop** — 4·6·7·8단계는 생성 후 반드시 사람 승인/수정 게이트를 둔다. 자동 진행 금지.
3. **결정론적 조립 우선** — 8단계 "편집"은 90% ffmpeg/Pillow 결정론적 작업 + 10% AI 보조.
4. **상태·버전 영속화** — 프로젝트 상태를 디스크에 JSON으로 저장. 사람 개입마다 버전 생성.
5. **비용 게이팅** — 7단계(컷별 영상)가 비용 절벽. 6단계 전체 이미지 승인 후에만 실행.
6. **로컬 GPU 불필요** — 모든 생성은 클라우드 API. 로컬은 ffmpeg 조립뿐.

(상세 명세는 원본 CLAUDE.md 참조. 본 구현은 Phase 1 MVP = 1~6단계 + 8단계 텍스트/로고 합성, `static_image` 포맷.)

---

## 구현 현황 (이 리포)
- **Phase 1 (MVP) — 완료**: 1~6 + 8단계(합성). `static_image`.
- **Phase 2 — 완료**: 7단계 image-to-video(Seedance, 키 없으면 ffmpeg Ken Burns) + 컷별 텍스트 번인 + 8단계 xfade 조립 + 음악 베드 믹스 + 9단계 플랫폼 인코딩. `ugc_video`/`cinematic_video`.
  - 영상 생성은 **비동기 잡 큐**(`core/jobs.py`) + 폴링(`GET /api/jobs/{id}`) + 진행률 UI. 컷별 재생성 지원.
  - **잡 히스토리(Phase 3c)**: `GET /api/jobs?project_id=` → 최근 잡 목록(`queue.list_jobs`), UI 상단 "≡ Jobs" 모달(종류·상태·진행률·실행 중 취소).
  - **잡 취소/interrupt(ComfyUI 차용)**: `POST /api/jobs/{id}/cancel` → 협조적 취소. 큐 대기 중이면 즉시, 실행 중이면 컷/클립 루프의 다음 체크포인트에서 중단(완료분은 보존). 긴 컷·영상 잡의 진행률 블록 "■ Stop" 버튼이 실제로 서버 잡을 멈춤. `queue.cancel/is_cancelled`, step5·step7 `should_cancel`.
- **Phase 3 — 대부분**: 이미지 provider 자동 fallback, 멀티 플랫폼 export(9:16/1:1/4:5), **영상 모델 라우팅·음악 모델 라우팅(ElevenLabs/MiniMax)**. (업스케일 Lanczos 베이스라인+Topaz 라우팅, 비트싱크 구현 — 전 항목 완성)
  - **이미지 모델 옵션**: Qwen(RunPod) · Nano Banana · Flux(fal) · **Krea 2 Turbo(fal, `image_krea.py` — FAL_KEY 공용)** · Free(Pollinations). 키 없으면 각자 mock. per-image picker(Studio/Recipe)는 `/api/providers/image-models`에서 동적 생성 → 새 provider 추가 시 자동 노출.
  - **fal 진단(Phase 2)**: fal 호출은 공용 `_fal.py`(fal_image_call/fal_diagnose)로 실패 사유 캡처. 키가 있는데 403(잔액부족) 등으로 실패하면 조용히 mock 폴백하지 않고 `ImageResult.meta.{fell_back,reason}`로 표면화 + `GET /api/providers/fal/test`(Flux·Krea 자가진단) + UI "Test fal" 버튼/폴백 toast 경고.
  - **영상(오픈소스 우선)**: 기본 라우트 `opensource` = RunPod 호스팅 오픈소스 I2V(Wan 2.2/LTX/SVD, `video_runpod.py`, 이미지 Qwen-RunPod 패턴 미러). `RUNPOD_VIDEO_ENDPOINT` 없으면 ffmpeg Ken Burns 무료 폴백 → 키 없이도 실클립 생성. 무료 폴백은 컷 camera에 맞춰 줌(in/out)·팬(좌/우/상/하)을 컷별로 다양화(`still_to_kenburns` motion + step7 `_motion_for`)해 단조로움을 피함. 대안: Seedance/Kling/Veo(fal, 유료). 연결 자가진단 `GET /api/providers/video/test`.
- **재현성(Generation Recipe, ComfyUI 차용)**: 모든 생성/편집 이미지가 `GenerationRecipe`(seed·prompt·model·background·strategy·provider)를 갖는다. 앱이 seed를 소유(생성 시 항상 기록)해 실제 provider도 재현 가능. recipe는 에셋/상태에 영속될 뿐 아니라 **PNG tEXt 메타데이터에 임베드**(`core/recipe.py`)되어, 다운로드한 이미지가 출처를 갖고 다니고 다시 추출 가능. API: `GET …/assets/{shot}/recipe`, `POST …/reproduce`(동일 seed→동일 이미지), `POST …/remix`(한 필드만 바꾸고 나머지 잠금), `POST …/recipe/extract`(드롭한 PNG에서 recipe 복원). UI: 각 이미지의 🧬 Recipe 모달(seed 잠금/🎲 랜덤 + Reproduce/Remix). 노드그래프 실행엔진은 의도적으로 차용하지 않음(선형 파이프라인 철학 유지). `pipeline/reproduce.py`, `tests/test_recipe.py`.
- **광고 지능**: 가중 루브릭 컨셉 평가(결정론적 채점) → 선택 컨셉이 콘티 훅/서사 시드 → 최종 합성 카피까지 연결.
  - **proof surfacing(specificity)**: `step1_brief._extract_proofs`가 수치/검증가능 클레임을 `Product.proof_points`로 추출 → 콘티 body 캡션이 proof를 우선·중복제거로 표면화(`step3_storyboard`), 일반 CTA일 땐 proof-led CTA("Get the 40-hour battery today", `step8`). 주의: sim_100 패널 점수는 입력 한계로 이미 균형 최적(76.4)이라, 컨셉 루브릭을 두 약축에 맞춰 비틀면 다른 축이 깎여 전체가 퇴보함 → 루브릭은 베이스라인 유지하고 proof는 가시 카피로만 노출(회귀 0). 추가 카피 품질은 실제 LLM(claude_cli) 경로에서.
- **전문가 패널 평가**(`app/eval/expert_panel.py`): 10인 광고 전문가 페르소나가 생성 광고를 채점 → 권장 광고비(예산 tier) 책정. `scripts/sim_100.py`로 100 제품 시뮬레이션·집계해 카피 로직의 체계적 약점을 찾고 개선(평가 주도 개선: 패널 평균 65.7→75.3).
- **UI/언어**: 세련된 다크 SaaS UI, 키보드 네비(←/→), **전 표면 영어**(기본 언어 en).
- **프로젝트 export/import(Phase 3a, ComfyUI '워크플로우=파일')**: `GET /api/projects/{id}/export`→포터블 `.cue.json`(spec/콘티/컨셉), `POST /api/projects/import`→새 프로젝트 복원(`state.import_project`, project_id 재키잉, 생성 에셋은 재생성 대상이라 미번들→스토리보드 단계 착지). UI 상단 ⤓Export/⤒Import.
- **카테고리 프리셋(Phase 3b)**: `core/templates.py` 카테고리별(food/beauty/tech/fitness/fashion/home/saas) tone·platform·duration 프리셋 → `GET /api/templates`, spec 단계 "Category preset" 셀렉트가 폼을 자동 채움. 컨셉 루브릭과 비결합(회귀 회피).
- **API 구조**: 라우트는 도메인별 `app/routers/`(system·projects·pipeline·recipe·jobs·output)로 분리, `app/main.py`는 앱 조립+예외 핸들러만. 공용 `get_state_or_404`는 `app/deps.py`. 이미지 조회는 `ProjectState.image_asset(shot_id)`, data-URI 디코드는 `pipeline.decode_image_data_uri`로 단일화.
- **안전/위생**: 비용 게이팅, shot_count/duration 상한, 입력 검증(400), 원자적 저장+버전, `.gitignore`/`Dockerfile`.
- 키 없으면 각 provider는 결정론적 mock/오프라인 동작 → 키 없이 end-to-end 실행 가능.
- 테스트: `python -m pytest -q` (243개, 보안·잡큐·프로바이더·recipe 재현성 포함, ffmpeg 있으면 영상/오디오/export/async-job 포함). 독립 적대적 버그리뷰 통과.

실행: `cd ad-factory && ./run.sh` 후 http://localhost:8000 접속.
