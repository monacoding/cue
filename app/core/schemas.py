"""핵심 데이터 모델 — 모든 단계가 이 객체를 읽고 쓴다 (CLAUDE.md §3).

AdSpec      = 2단계 산출물 ("레이어")
Storyboard  = 3단계 산출물 ("콘티")
ProjectState = 전 파이프라인 영속 상태 + 버전 (core/state.py 에서 사용)
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class OutputFormat(str, Enum):
    static_image = "static_image"
    ugc_video = "ugc_video"
    cinematic_video = "cinematic_video"


class PipelineStep(int, Enum):
    brief = 1
    spec = 2
    storyboard = 3
    hero_image = 4
    shot_images = 5
    image_edit = 6
    shot_video = 7
    assemble = 8
    encode = 9


class StepStatus(str, Enum):
    pending = "pending"        # 아직 생성 안 됨
    generated = "generated"    # 생성됨, 사람 검토 대기
    approved = "approved"      # 사람 승인됨
    blocked = "blocked"        # 게이팅으로 차단됨


# ---------------------------------------------------------------------------
# AdSpec (2단계 산출물 = "레이어")
# ---------------------------------------------------------------------------
class Product(BaseModel):
    name: str = ""
    description: str = ""
    key_message: str = ""
    image_urls: List[str] = Field(default_factory=list)
    # concrete, verifiable proof claims extracted from the source (e.g. "40-hour battery",
    # "100-night trial") — surfaced into hooks/storyboard/CTA to raise specificity & proof.
    proof_points: List[str] = Field(default_factory=list)


class Brand(BaseModel):
    logo_url: str = ""
    palette: List[str] = Field(default_factory=list)
    fonts: List[str] = Field(default_factory=list)


class Audio(BaseModel):
    voiceover: bool = True
    music_mood: str = ""
    language: str = "en"
    music_model: str = "elevenlabs"  # elevenlabs | minimax (registry routing)
    music_volume: float = 1.0        # 0..2 multiplier on the music bed at mux time
    music_muted: bool = False        # mute the music track entirely


class AdSpec(BaseModel):
    project_id: str
    product: Product = Field(default_factory=Product)
    format: OutputFormat = OutputFormat.static_image
    aspect_ratio: str = "9:16"          # 9:16 | 1:1 | 16:9 | 4:5
    duration_sec: int = 15
    shot_count: int = 5                  # duration_sec 에서 파생, 사람 조정 가능
    platform: str = "instagram_reels"    # instagram_reels | tiktok | youtube_shorts | feed
    tone: str = ""
    brief_notice: str = ""               # transient UX note (e.g. a URL couldn't be fetched)
    last_render: Optional[Dict[str, Any]] = None   # static render recipe → re-composite per export ratio
    brand: Brand = Field(default_factory=Brand)
    audio: Audio = Field(default_factory=Audio)

    @staticmethod
    def derive_shot_count(duration_sec: int) -> int:
        """길이에서 컷 수 파생 (대략 3초/컷, 최소 3 최대 12)."""
        return max(3, min(12, round(duration_sec / 3)))


# ---------------------------------------------------------------------------
# Storyboard (3단계 산출물 = "콘티") — 산문 금지, 컷별 구조 데이터
# ---------------------------------------------------------------------------
class OnScreenText(BaseModel):
    text_ko: str = ""
    position: str = ""       # top | center | bottom | ...
    style_ref: str = ""


class AudioCue(BaseModel):
    vo_ko: str = ""
    sfx: str = ""
    music_beat: str = ""


class Shot(BaseModel):
    id: str
    description: str = ""                  # 시각적 묘사
    camera: str = "static"                 # dolly_in | static | pan_left | tracking | ...
    duration_sec: float = 3
    transition: str = "cut"                # cut | crossfade | whip_pan
    on_screen_text: OnScreenText = Field(default_factory=OnScreenText)
    audio_cue: AudioCue = Field(default_factory=AudioCue)
    image_prompt: str = ""                 # 이미지 생성용 프롬프트
    consistency_anchor: str = "hero_image"
    label_color: str = ""                  # editor clip color label (organization)
    gap_before: float = 0.0                # empty timeline space before this clip (NLE gap)


class Storyboard(BaseModel):
    shots: List[Shot] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Concept (3단계 선택 — 가중 루브릭 컨셉 평가용)
# ---------------------------------------------------------------------------
class Concept(BaseModel):
    id: str
    hook: str
    angle: str
    rationale: str = ""
    score: float = 0.0
    score_breakdown: Dict[str, float] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Assets — 생성된 이미지/영상 산출물 추적
# ---------------------------------------------------------------------------
class GenerationRecipe(BaseModel):
    """A reproducible record of how an image was generated (ComfyUI-inspired §1).

    Every generated/edited image carries the full recipe so any output can be
    *reproduced* (same seed → same image) or *remixed* (change one field, lock the
    rest) — the human-in-the-loop convergence UX. The recipe is also embedded into
    the PNG's metadata (core/recipe.py) so a downloaded image travels with its
    provenance and can be dragged back in to restore the recipe.
    """
    v: int = 1                       # recipe format version
    app: str = "cue"
    kind: str = "image"              # image | edit
    shot_id: str = ""                # "hero" or a shot id
    base_prompt: str = ""            # prompt BEFORE background/consistency suffix (remix re-derives)
    prompt: str = ""                 # final prompt sent to the model (exact reproduce)
    provider: str = ""               # provider that produced it (e.g. "nano-banana·mock")
    model: str = "auto"              # picker key: auto | qwen | nano_banana | flux | free
    background: str = ""             # background scene key (trained-LoRA caption)
    aspect_ratio: str = "9:16"
    seed: Optional[int] = None       # app-owned seed (reproducibility)
    strategy: str = ""               # consistency strategy (shots): reference | seed | edit
    ref: str = ""                    # consistency anchor used (e.g. "hero")
    instruction: str = ""            # natural-language edit instruction (kind=edit)
    created_at: str = ""             # ISO8601


class ImageAsset(BaseModel):
    shot_id: str                     # "hero" 또는 shot_id
    url: str                         # /storage/... 경로
    prompt: str = ""
    provider: str = ""
    status: StepStatus = StepStatus.generated
    revision: int = 0                # 편집 반복 횟수
    seed: Optional[int] = None       # app-owned generation seed (reproducibility)
    model: str = ""                  # image-model picker key used (auto/qwen/…)
    background: str = ""             # background scene key used
    recipe: Optional["GenerationRecipe"] = None  # full reproducible recipe (ComfyUI-inspired)


class VideoAsset(BaseModel):
    shot_id: str
    url: str
    provider: str = ""
    status: StepStatus = StepStatus.generated


class ExportVariant(BaseModel):
    ratio: str           # 9:16 | 1:1 | 4:5 | 16:9
    url: str
    kind: str = "image"  # image | video


class ABVariant(BaseModel):
    """An ad rendered with a specific concept's hook — for A/B comparison (CLAUDE.md §1)."""
    concept_id: str
    hook: str
    angle: str = ""
    url: str
    score: float = 0.0


# ---------------------------------------------------------------------------
# ProjectState — 디스크 영속 상태 (core/state.py)
# ---------------------------------------------------------------------------
class StepState(BaseModel):
    step: PipelineStep
    status: StepStatus = StepStatus.pending


class Version(BaseModel):
    version: int
    step: PipelineStep
    label: str = ""
    created_at: str = ""             # ISO8601 (state.py 가 채움)


class ProjectState(BaseModel):
    project_id: str
    title: str = "Untitled Ad"
    format: OutputFormat = OutputFormat.static_image
    current_step: PipelineStep = PipelineStep.brief

    adspec: Optional[AdSpec] = None
    storyboard: Optional[Storyboard] = None
    concepts: List[Concept] = Field(default_factory=list)
    selected_concept_id: Optional[str] = None

    hero_image: Optional[ImageAsset] = None
    shot_images: List[ImageAsset] = Field(default_factory=list)
    shot_videos: List[VideoAsset] = Field(default_factory=list)
    final_outputs: List[str] = Field(default_factory=list)  # 최종 파일 경로
    exports: List["ExportVariant"] = Field(default_factory=list)  # 멀티 비율 export
    ab_variants: List["ABVariant"] = Field(default_factory=list)  # 컨셉별 A/B 변형

    steps: Dict[str, StepState] = Field(default_factory=dict)
    versions: List[Version] = Field(default_factory=list)

    created_at: str = ""
    updated_at: str = ""

    def image_asset(self, shot_id: str) -> Optional["ImageAsset"]:
        """The image for a shot id — the hero when shot_id == 'hero', else the matching shot
        image (None if absent). Single lookup shared by the API, edit and reproduce paths."""
        if shot_id == "hero":
            return self.hero_image
        return next((a for a in self.shot_images if a.shot_id == shot_id), None)

    def step_status(self, step: PipelineStep) -> StepStatus:
        st = self.steps.get(str(int(step)))
        return st.status if st else StepStatus.pending

    def set_step_status(self, step: PipelineStep, status: StepStatus) -> None:
        self.steps[str(int(step))] = StepState(step=step, status=status)


# ---------------------------------------------------------------------------
# API 요청/응답 DTO
# ---------------------------------------------------------------------------
class CreateProjectRequest(BaseModel):
    title: str = "Untitled Ad"
    format: OutputFormat = OutputFormat.static_image
    product_url: str = ""
    product_text: str = ""
    image_urls: List[str] = Field(default_factory=list)


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    key_message: Optional[str] = None
    image_urls: Optional[List[str]] = None


class SpecRequest(BaseModel):
    aspect_ratio: Optional[str] = None
    duration_sec: Optional[int] = None
    shot_count: Optional[int] = None
    platform: Optional[str] = None
    tone: Optional[str] = None
    palette: Optional[List[str]] = None
    logo_url: Optional[str] = None
    music_mood: Optional[str] = None
    music_model: Optional[str] = None
    language: Optional[str] = None


class HeroRequest(BaseModel):
    prompt: Optional[str] = None       # 사람이 룩을 고정하기 위한 프롬프트 override
    model: Optional[str] = None        # per-image model: auto/qwen/nano_banana/flux/free
    background: Optional[str] = None   # per-image background scene (trained LoRA caption key)
    seed: Optional[int] = None         # lock the seed for reproducibility (None → app picks one)


class ShotEditRequest(BaseModel):
    instruction: str                   # 자연어 편집 지시
    model: Optional[str] = None        # per-image model override


class TextOverlay(BaseModel):
    text: str
    position: str = "bottom"           # top | center | bottom
    size: int = 64
    color: str = "#FFFFFF"


class RenderRequest(BaseModel):
    shot_id: Optional[str] = None      # None 이면 hero 사용
    headline: str = ""
    cta: str = ""
    overlays: List[TextOverlay] = Field(default_factory=list)
    show_logo: bool = True


class RemixRequest(BaseModel):
    """Override selected recipe fields; the rest stay locked (ComfyUI-inspired remix)."""
    base_prompt: Optional[str] = None
    background: Optional[str] = None
    seed: Optional[int] = None
    model: Optional[str] = None
    instruction: Optional[str] = None  # for edit-kind recipes
    randomize_seed: bool = False       # pick a fresh seed when no explicit seed is given
