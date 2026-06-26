"""hero → 컷 일관성 로직 (CLAUDE.md §4.5 — 엔지니어링 난이도 90%).

providers 와 분리해 방법(레퍼런스/시드/편집)을 교체 가능하게 둔다.
현재 전략: REFERENCE — hero_image 를 모든 컷 생성의 reference image 로 전달.
(향후 SEED / EDIT 전략 추가 가능. 파이프라인은 build_references() 만 호출.)
"""
from __future__ import annotations

import hashlib
from enum import Enum
from typing import List, Optional


class ConsistencyStrategy(str, Enum):
    reference = "reference"   # hero 를 레퍼런스로 (Nano Banana 14 ref)
    seed = "seed"            # 동일 시드 고정
    edit = "edit"           # hero 편집 파생


DEFAULT_STRATEGY = ConsistencyStrategy.reference
STRATEGIES = tuple(s.value for s in ConsistencyStrategy)


def coerce(strategy) -> ConsistencyStrategy:
    try:
        return ConsistencyStrategy(strategy)
    except ValueError:
        return DEFAULT_STRATEGY


def project_seed(project_id: str) -> int:
    """Deterministic per-project seed for the SEED strategy (locks the look across shots)."""
    return int(hashlib.sha256(project_id.encode()).hexdigest()[:8], 16)


def build_references(
    hero_bytes: Optional[bytes],
    extra: Optional[List[bytes]] = None,
    strategy: ConsistencyStrategy = DEFAULT_STRATEGY,
    max_refs: int = 14,
) -> List[bytes]:
    """컷 생성에 넣을 레퍼런스 이미지 목록을 구성."""
    refs: List[bytes] = []
    if strategy == ConsistencyStrategy.reference and hero_bytes:
        refs.append(hero_bytes)
    if extra:
        refs.extend(extra)
    return refs[:max_refs]


def consistency_prompt_suffix(strategy: ConsistencyStrategy = DEFAULT_STRATEGY) -> str:
    """Consistency instruction appended to each shot prompt (English — sent to the model)."""
    if strategy == ConsistencyStrategy.reference:
        return (
            " IMPORTANT: keep the EXACT SAME main subject (the same person's face/identity"
            " and/or the same product) as the attached reference image: identical character,"
            " product, outfit, color palette and lighting. Only change the scene, camera angle,"
            " pose and composition described above. Same subject, new shot."
        )
    if strategy == ConsistencyStrategy.seed:
        return (
            " Keep the SAME main subject (same person/product, identity, outfit, colour and"
            " lighting) across every shot; vary only the scene, angle and pose. Same subject, new shot."
        )
    if strategy == ConsistencyStrategy.edit:
        return (
            " Preserve the subject, product and styling of the base image;"
            " only change the scene/composition described above."
        )
    return ""
