"""Shapes shared by the API, the database layer and the AI step. Mirrors frontend/src/types.ts."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

UrgencyLevel = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
LocationType = Literal["basement", "home", "street", "car", "other"]
ReportStatus = Literal["new", "dispatched", "resolved"]
AIStatus = Literal["pending", "done", "failed"]
LocationSource = Literal["gps", "typed", "spoken", "seed", "storm"]
InputType = Literal["voice", "text"]
UiLanguage = Literal["en", "ar", "es"]


class Hazards(BaseModel):
    electrical: bool = False
    sewage: bool = False
    gas: bool = False
    structural: bool = False


class PeopleAtRisk(BaseModel):
    elderly: bool = False
    children: bool = False
    disabled: bool = False
    medical: bool = False  # someone needs medical help now
    trapped: bool = False  # someone cannot get out on their own
    count: Optional[int] = None  # people at the location, if the reporter said


class Needs(BaseModel):
    evacuation: bool = False
    pumping: bool = False
    medical: bool = False
    supplies: bool = False


class Extraction(BaseModel):
    """Everything the AI step pulls out of one report. The keyword fallback produces the same shape."""

    language: str = Field(description="ISO 639-1 code of the language the reporter used, e.g. 'ar', 'es', 'en'")
    transcript_original: str = Field(description="Verbatim transcript in the reporter's own language and script")
    transcript_english: str = Field(description="Faithful English translation of the transcript")
    ai_summary: str = Field(description="One short English line for responders: who, what, how bad")
    confirmation_message: str = Field(
        description="One short, calm sentence in the reporter's language confirming what was understood"
    )
    water_depth_cm: Optional[int] = Field(default=None, description="Deepest water mentioned, in centimeters")
    location_type: LocationType = "other"
    location_hint: Optional[str] = Field(
        default=None, description="Street, cross streets or landmark the reporter mentioned, if any"
    )
    water_in_living_space: bool = False
    water_rising: bool = False
    hazards: Hazards = Field(default_factory=Hazards)
    people_at_risk: PeopleAtRisk = Field(default_factory=PeopleAtRisk)
    needs: Needs = Field(default_factory=Needs)


class Report(BaseModel):
    """A report as the API returns it."""

    id: int
    created_at: str  # ISO 8601, UTC, e.g. "2026-10-04T15:04:05Z"
    updated_at: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    accuracy_m: Optional[float] = None
    location_source: Optional[LocationSource] = None
    address_text: Optional[str] = None
    ui_language: UiLanguage = "en"
    input_type: InputType = "voice"
    language: Optional[str] = None
    transcript_original: Optional[str] = None
    transcript_english: Optional[str] = None
    ai_summary: Optional[str] = None
    confirmation_message: Optional[str] = None
    location_hint: Optional[str] = None
    water_depth_cm: Optional[int] = None
    location_type: Optional[LocationType] = None
    water_in_living_space: bool = False
    water_rising: bool = False
    hazards: Hazards = Field(default_factory=Hazards)
    people_at_risk: PeopleAtRisk = Field(default_factory=PeopleAtRisk)
    needs: Needs = Field(default_factory=Needs)
    urgency_score: Optional[int] = None  # None while ai_status is "pending"
    urgency_level: Optional[UrgencyLevel] = None
    urgency_reasons: list[str] = Field(default_factory=list)
    status: ReportStatus = "new"
    ai_status: AIStatus = "pending"
    ai_engine: Optional[str] = None
    ai_error: Optional[str] = None
    ai_latency_ms: Optional[int] = None
    audio_url: Optional[str] = None
    photo_url: Optional[str] = None
    is_simulated: bool = False


class StatusUpdate(BaseModel):
    status: ReportStatus


class PublicUrlUpdate(BaseModel):
    public_url: Optional[str] = None


class AppConfig(BaseModel):
    public_url: Optional[str]
    report_url: Optional[str]
    ai_enabled: bool
    ai_model: Optional[str]
    storm_running: bool
    storm_injected: int
    map_center: tuple[float, float]
    map_zoom: int


class StormState(BaseModel):
    running: bool
    injected: int


class Hotspot(BaseModel):
    label: str
    lat: float
    lng: float
    radius_m: float
    level: UrgencyLevel
    report_ids: list[int]


class Briefing(BaseModel):
    text: str
    hotspots: list[Hotspot]
    generated_at: str
    engine: str
