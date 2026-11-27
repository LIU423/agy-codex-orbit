"""Three-mode model routing policy persisted in `${PLUGIN_DATA}/settings.json`."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from server.delegation_store import resolve_data_dir

RoutingMode = Literal["silent_low_cost", "ask", "silent_best"]
TaskTier = Literal["light", "standard", "complex"]
UiLocale = Literal["auto", "zh", "en", "ja"]

VALID_ROUTING_MODES: tuple[RoutingMode, ...] = ("silent_low_cost", "ask", "silent_best")
VALID_TASK_TIERS: tuple[TaskTier, ...] = ("light", "standard", "complex")
VALID_UI_LOCALES: tuple[UiLocale, ...] = ("auto", "zh", "en", "ja")
DEFAULT_ROUTING_MODE: RoutingMode = "ask"
DEFAULT_UI_LOCALE: UiLocale = "auto"

MODEL_SOL = "gpt-6.2-sol"
MODEL_ASTRA = "gpt-6.2-astra"

ROUTING_MODE_LABELS: dict[RoutingMode, str] = {
    "silent_low_cost": "节能",
    "ask": "均衡",
    "silent_best": "极致",
}


@dataclass(frozen=True)
class RouteDecision:
    routing_mode: RoutingMode
    task_tier: TaskTier
    model: str
    reasoning_effort: str
    requires_confirmation: bool
    recommended_model: str | None = None
    recommended_effort: str | None = None
    fallback_model: str | None = None
    fallback_effort: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "routing_mode": self.routing_mode,
            "routing_mode_label": ROUTING_MODE_LABELS[self.routing_mode],
            "task_tier": self.task_tier,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "requires_confirmation": self.requires_confirmation,
            "recommended_model": self.recommended_model,
            "recommended_effort": self.recommended_effort,
            "fallback_model": self.fallback_model,
            "fallback_effort": self.fallback_effort,
            "reason": self.reason,
        }


def _settings_path(data_dir: Path | str | None = None) -> Path:
    return resolve_data_dir(data_dir) / "settings.json"


def load_routing_mode(data_dir: Path | str | None = None) -> RoutingMode:
    path = _settings_path(data_dir)
    if not path.is_file():
        return DEFAULT_ROUTING_MODE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        val = raw.get("routing_mode") if isinstance(raw, dict) else None
        if val in VALID_ROUTING_MODES:
            return val  # type: ignore[return-value]
    except (OSError, json.JSONDecodeError):
        pass
    return DEFAULT_ROUTING_MODE


def save_routing_mode(mode: str, data_dir: Path | str | None = None) -> RoutingMode:
    if mode not in VALID_ROUTING_MODES:
        raise ValueError(
            f"Invalid routing_mode {mode!r}; expected one of {', '.join(VALID_ROUTING_MODES)}."
        )
    typed_mode: RoutingMode = mode  # type: ignore[assignment]
    path = _settings_path(data_dir)
    existing: dict[str, object] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}

    existing["routing_mode"] = typed_mode
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return typed_mode


def load_ui_locale(data_dir: Path | str | None = None) -> UiLocale:
    path = _settings_path(data_dir)
    if not path.is_file():
        return DEFAULT_UI_LOCALE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        val = raw.get("ui_locale") if isinstance(raw, dict) else None
        if val in VALID_UI_LOCALES:
            return val  # type: ignore[return-value]
    except (OSError, json.JSONDecodeError):
        pass
    return DEFAULT_UI_LOCALE


def save_ui_locale(locale: str, data_dir: Path | str | None = None) -> UiLocale:
    if locale not in VALID_UI_LOCALES:
        raise ValueError(
            f"Invalid ui_locale {locale!r}; expected one of {', '.join(VALID_UI_LOCALES)}."
        )
    typed_locale: UiLocale = locale  # type: ignore[assignment]
    path = _settings_path(data_dir)
    existing: dict[str, object] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}

    existing["ui_locale"] = typed_locale
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return typed_locale


def resolve_route(
    *,
    routing_mode: RoutingMode,
    task_tier: TaskTier = "standard",
    isolation_mode: str = "workspace-write",
) -> RouteDecision:
    """Resolve model, reasoning_effort, and user-confirmation requirement for a task tier."""
    is_read_only = isolation_mode == "read-only"

    if routing_mode == "silent_low_cost":
        if task_tier == "light":
            return RouteDecision(
                routing_mode=routing_mode,
                task_tier=task_tier,
                model=MODEL_SOL,
                reasoning_effort="low" if is_read_only else "medium",
                requires_confirmation=False,
                reason="节能模式：轻量任务直接使用 gpt-6.2-sol 低消耗档位。",
            )
        if task_tier == "standard":
            return RouteDecision(
                routing_mode=routing_mode,
                task_tier=task_tier,
                model=MODEL_SOL,
                reasoning_effort="medium",
                requires_confirmation=False,
                reason="节能模式：常规后端任务使用 gpt-6.2-sol (medium)。",
            )
        return RouteDecision(
            routing_mode=routing_mode,
            task_tier=task_tier,
            model=MODEL_SOL,
            reasoning_effort="high",
            requires_confirmation=False,
            reason="节能模式：复杂任务保持 gpt-6.2-sol (high)，不升级 Astra。",
        )

    if routing_mode == "silent_best":
        if task_tier == "light":
            return RouteDecision(
                routing_mode=routing_mode,
                task_tier=task_tier,
                model=MODEL_SOL,
                reasoning_effort="medium",
                requires_confirmation=False,
                reason="极致模式：轻量查询/单测用 gpt-6.2-sol (medium) 保证速度与精准度。",
            )
        if task_tier == "standard":
            return RouteDecision(
                routing_mode=routing_mode,
                task_tier=task_tier,
                model=MODEL_ASTRA,
                reasoning_effort="high",
                requires_confirmation=False,
                reason="极致模式：常规后端开发/深度审查直接使用 gpt-6.2-astra (high)。",
            )
        return RouteDecision(
            routing_mode=routing_mode,
            task_tier=task_tier,
            model=MODEL_ASTRA,
            reasoning_effort="xhigh",
            requires_confirmation=False,
            reason="极致模式：复杂架构/跨模块疑难排查直接使用 gpt-6.2-astra (xhigh)。",
        )

    # Default: "ask" (均衡模式)
    if task_tier == "light":
        return RouteDecision(
            routing_mode=routing_mode,
            task_tier=task_tier,
            model=MODEL_SOL,
            reasoning_effort="medium",
            requires_confirmation=False,
            reason="均衡模式：轻量任务静默使用 gpt-6.2-sol (medium)。",
        )
    if task_tier == "standard":
        return RouteDecision(
            routing_mode=routing_mode,
            task_tier=task_tier,
            model=MODEL_SOL,
            reasoning_effort="medium" if is_read_only else "high",
            requires_confirmation=False,
            reason="均衡模式：常规任务静默使用 gpt-6.2-sol。",
        )
    return RouteDecision(
        routing_mode=routing_mode,
        task_tier=task_tier,
        model=MODEL_SOL,
        reasoning_effort="xhigh",
        requires_confirmation=True,
        recommended_model=MODEL_ASTRA,
        recommended_effort="high" if is_read_only else "xhigh",
        fallback_model=MODEL_SOL,
        fallback_effort="xhigh",
        reason="均衡模式：任务属于复杂架构/跨模块疑难排查，建议升级 gpt-6.2-astra，需结合当前剩余额度由用户拍板。",
    )
