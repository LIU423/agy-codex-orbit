"""Three-mode model routing policy persisted in `${PLUGIN_DATA}/settings.json`."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

from server.delegation_store import _atomic_write_json, resolve_data_dir

RoutingMode = Literal["silent_low_cost", "ask", "silent_best"]
TaskTier = Literal["light", "standard", "complex"]
UiLocale = Literal["auto", "zh-CN", "zh-TW", "en", "ja"]
ReasoningEffortSetting = Literal["auto", "low", "medium", "high", "xhigh", "max", "ultra"]

VALID_ROUTING_MODES: tuple[RoutingMode, ...] = ("silent_low_cost", "ask", "silent_best")
VALID_TASK_TIERS: tuple[TaskTier, ...] = ("light", "standard", "complex")
VALID_UI_LOCALES = ("auto", "zh", "en", "ja")
VALID_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max", "ultra")
VALID_REASONING_EFFORT_SETTINGS: tuple[ReasoningEffortSetting, ...] = (
    "auto", "low", "medium", "high", "xhigh", "max", "ultra",
)
_CANONICAL_UI_LOCALES: tuple[UiLocale, ...] = ("auto", "zh-CN", "zh-TW", "en", "ja")
DEFAULT_ROUTING_MODE: RoutingMode = "ask"
DEFAULT_UI_LOCALE: UiLocale = "auto"
DEFAULT_REASONING_EFFORT: ReasoningEffortSetting = "auto"

MODEL_SOL = "gpt-6.2-sol"
MODEL_ASTRA = "gpt-6.2-astra"
DEFAULT_QUOTA_GUARDRAIL_THRESHOLD_PCT = 15

ROUTING_MODE_LABELS: dict[RoutingMode, str] = {
    "silent_low_cost": "Eco",
    "ask": "Balanced",
    "silent_best": "Max",
}


@dataclass(frozen=True)
class OrbitSettings:
    routing_mode: RoutingMode = DEFAULT_ROUTING_MODE
    ui_locale: UiLocale = DEFAULT_UI_LOCALE
    default_model: str = MODEL_SOL
    escalation_model: str = MODEL_ASTRA
    quota_guardrail_enabled: bool = True
    quota_guardrail_threshold_pct: int = DEFAULT_QUOTA_GUARDRAIL_THRESHOLD_PCT
    reasoning_effort: ReasoningEffortSetting = DEFAULT_REASONING_EFFORT

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


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
    guardrail_triggered: bool = False
    guardrail_note: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"routing_mode_label": ROUTING_MODE_LABELS[self.routing_mode], **asdict(self)}


def _normalize_ui_locale(value: object) -> UiLocale | None:
    if not isinstance(value, str):
        return None
    if value in _CANONICAL_UI_LOCALES:
        return cast(UiLocale, value)
    low = value.strip().lower().replace("_", "-")
    if low == "auto":
        return "auto"
    if low == "en" or low.startswith("en-"):
        return "en"
    if low == "ja" or low.startswith("ja-"):
        return "ja"
    if low in ("zh", "zh-cn", "zh-hans", "zh-sg"):
        return "zh-CN"
    if low in ("zh-tw", "zh-hk", "zh-mo", "zh-hant"):
        return "zh-TW"
    return None


def _normalize_model(value: object, fallback: str) -> str:
    return (value.strip() or fallback) if isinstance(value, str) else fallback


def _normalize_reasoning_effort(value: object) -> ReasoningEffortSetting | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower() or DEFAULT_REASONING_EFFORT
    if normalized in VALID_REASONING_EFFORT_SETTINGS:
        return cast(ReasoningEffortSetting, normalized)
    return None


def _normalize_quota_threshold(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(5, min(value, 50))
    return DEFAULT_QUOTA_GUARDRAIL_THRESHOLD_PCT


def _settings_path(data_dir: Path | str | None) -> Path:
    return resolve_data_dir(data_dir) / "settings.json"


def _read_settings(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _parse_settings(raw: dict[str, object]) -> OrbitSettings:
    mode = raw.get("routing_mode")
    enabled = raw.get("quota_guardrail_enabled")
    return OrbitSettings(
        routing_mode=cast(RoutingMode, mode) if mode in VALID_ROUTING_MODES else DEFAULT_ROUTING_MODE,
        ui_locale=_normalize_ui_locale(raw.get("ui_locale")) or DEFAULT_UI_LOCALE,
        default_model=_normalize_model(raw.get("default_model"), MODEL_SOL),
        escalation_model=_normalize_model(raw.get("escalation_model"), MODEL_ASTRA),
        quota_guardrail_enabled=enabled if isinstance(enabled, bool) else True,
        quota_guardrail_threshold_pct=_normalize_quota_threshold(raw.get("quota_guardrail_threshold_pct")),
        reasoning_effort=_normalize_reasoning_effort(raw.get("reasoning_effort")) or DEFAULT_REASONING_EFFORT,
    )


def load_settings(data_dir: Path | str | None = None) -> OrbitSettings:
    return _parse_settings(_read_settings(_settings_path(data_dir)))


class _Unset:
    pass


_UNSET = _Unset()


def save_settings(
    data_dir: Path | str | None = None,
    *,
    routing_mode: str | None = None,
    ui_locale: str | None = None,
    default_model: str | None | _Unset = _UNSET,
    escalation_model: str | None | _Unset = _UNSET,
    quota_guardrail_enabled: bool | None = None,
    quota_guardrail_threshold_pct: int | None = None,
    reasoning_effort: str | None | _Unset = _UNSET,
) -> OrbitSettings:
    """Persist supplied fields atomically. Blank or None resets model and effort settings."""
    path = _settings_path(data_dir)
    raw = _read_settings(path)
    if routing_mode is not None:
        if routing_mode not in VALID_ROUTING_MODES:
            raise ValueError(f"Invalid routing_mode {routing_mode!r}. Choose from {', '.join(VALID_ROUTING_MODES)}.")
        raw["routing_mode"] = routing_mode
    if ui_locale is not None:
        normalized = _normalize_ui_locale(ui_locale)
        if normalized is None:
            raise ValueError(f"Invalid ui_locale {ui_locale!r}. Choose from {', '.join(_CANONICAL_UI_LOCALES)}.")
        raw["ui_locale"] = normalized
    if default_model is not _UNSET:
        raw["default_model"] = default_model
    if escalation_model is not _UNSET:
        raw["escalation_model"] = escalation_model
    if reasoning_effort is not _UNSET:
        effort = (
            DEFAULT_REASONING_EFFORT if reasoning_effort is None else _normalize_reasoning_effort(reasoning_effort)
        )
        if effort is None:
            raise ValueError(
                f"Invalid reasoning_effort {reasoning_effort!r}. "
                f"Choose from {', '.join(VALID_REASONING_EFFORT_SETTINGS)}."
            )
        raw["reasoning_effort"] = effort
    if quota_guardrail_enabled is not None:
        if not isinstance(quota_guardrail_enabled, bool):
            raise ValueError("quota_guardrail_enabled must be a boolean.")
        raw["quota_guardrail_enabled"] = quota_guardrail_enabled
    if quota_guardrail_threshold_pct is not None:
        if not isinstance(quota_guardrail_threshold_pct, int) or isinstance(quota_guardrail_threshold_pct, bool):
            raise ValueError("quota_guardrail_threshold_pct must be an integer.")
        raw["quota_guardrail_threshold_pct"] = quota_guardrail_threshold_pct
    settings = _parse_settings(raw)
    raw.update(settings.to_dict())
    _atomic_write_json(path, raw)
    return settings


def load_routing_mode(data_dir: Path | str | None = None) -> RoutingMode:
    return load_settings(data_dir).routing_mode


def save_routing_mode(mode: str, data_dir: Path | str | None = None) -> RoutingMode:
    return save_settings(data_dir, routing_mode=mode).routing_mode


def load_ui_locale(data_dir: Path | str | None = None) -> UiLocale:
    return load_settings(data_dir).ui_locale


def save_ui_locale(locale: str, data_dir: Path | str | None = None) -> UiLocale:
    return save_settings(data_dir, ui_locale=locale).ui_locale


def resolve_route(
    *,
    routing_mode: RoutingMode,
    task_tier: TaskTier = "standard",
    isolation_mode: str = "workspace-write",
    default_model: str | None = None,
    escalation_model: str | None = None,
    quota_remaining_5h: float | None = None,
    quota_guardrail_enabled: bool = True,
    quota_guardrail_threshold_pct: int = DEFAULT_QUOTA_GUARDRAIL_THRESHOLD_PCT,
    reasoning_effort_override: str | None = None,
) -> RouteDecision:
    """Resolve the model, reasoning effort, and confirmation for a task tier."""
    explicit_effort = None
    if reasoning_effort_override is not None:
        normalized = _normalize_reasoning_effort(reasoning_effort_override)
        if normalized is None:
            raise ValueError(
                f"Invalid reasoning_effort {reasoning_effort_override!r}. "
                f"Choose from {', '.join(VALID_REASONING_EFFORT_SETTINGS)}."
            )
        if normalized != DEFAULT_REASONING_EFFORT:
            explicit_effort = normalized
    eff_default = _normalize_model(default_model, MODEL_SOL)
    eff_escalation = _normalize_model(escalation_model, MODEL_ASTRA)
    is_read_only = isolation_mode == "read-only"
    threshold = _normalize_quota_threshold(quota_guardrail_threshold_pct)
    low_quota = (
        f"Only {quota_remaining_5h:g}% of the 5-hour quota remains (threshold {threshold}%)."
        if quota_guardrail_enabled and quota_remaining_5h is not None and quota_remaining_5h < threshold
        else None
    )

    if routing_mode == "silent_low_cost":
        if task_tier == "light":
            effort = "low" if is_read_only else "medium"
        elif task_tier == "standard":
            effort = "medium"
        else:
            effort = "high"
        return RouteDecision(
            routing_mode, task_tier, eff_default, explicit_effort or effort, False,
            reason=(
                "Eco uses the default model with the selected effort."
                if explicit_effort else "Eco uses the default model and sets effort by task tier."
            ),
        )

    if task_tier == "light":
        effort = explicit_effort or "medium"
        return RouteDecision(
            routing_mode, task_tier, eff_default, effort, False,
            reason=f"Light tasks use the default model at {effort} effort.",
        )

    if routing_mode == "silent_best":
        effort = explicit_effort or ("high" if task_tier == "standard" else "xhigh")
        if low_quota and eff_escalation != eff_default:
            return RouteDecision(
                routing_mode, task_tier, eff_default, effort, False,
                recommended_model=eff_escalation,
                reason="Max uses the default model when quota is low.",
                guardrail_triggered=True,
                guardrail_note=f"{low_quota} Using {eff_default} to conserve quota.",
            )
        return RouteDecision(
            routing_mode, task_tier, eff_escalation, effort, False,
            reason="Max uses the escalation model for standard and complex tasks.",
        )

    if task_tier == "standard":
        return RouteDecision(
            routing_mode, task_tier, eff_default, explicit_effort or ("medium" if is_read_only else "high"), False,
            reason="Balanced uses the default model for standard tasks.",
        )

    return RouteDecision(
        routing_mode, task_tier, eff_default, explicit_effort or "xhigh", True,
        recommended_model=eff_escalation,
        recommended_effort=explicit_effort or ("high" if is_read_only else "xhigh"),
        fallback_model=eff_default,
        fallback_effort=explicit_effort or "xhigh",
        reason="Balanced asks before using the escalation model for complex tasks.",
        guardrail_triggered=low_quota is not None,
        guardrail_note=f"{low_quota} Consider staying on {eff_default}." if low_quota else None,
    )
