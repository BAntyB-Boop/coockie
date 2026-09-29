"""API request/response models and the versioned streaming protocol.

Preview protocol (``/ws/preview``, version 1):
  1. Text frame  : {"v":1,"type":"preview_meta","frame_id":N,"view":...,"width":...,...}
  2. Binary frame: 16-byte header ``struct(">4sBxxxQ")`` = (b"CRPV", version, frame_id)
                   followed by JPEG bytes. Clients match image and metadata by frame_id.
Events protocol (``/ws/events``): text JSON {"v":1,"type":"status"|"telemetry"|"log"|...}.
Authentication (LAN mode only): first client message {"type":"auth","token":"..."}.
"""
from __future__ import annotations

import re
import struct
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from . import PROTOCOL_VERSION
from .config import PROFILE_NAME_RE

PREVIEW_MAGIC = b"CRPV"
PREVIEW_HEADER = struct.Struct(">4sBxxxQ")
ENDPOINT_RE = re.compile(r"^(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.\-]{1,253}):\d{1,5}$")


def pack_preview(frame_id: int, jpeg: bytes) -> bytes:
    return PREVIEW_HEADER.pack(PREVIEW_MAGIC, PROTOCOL_VERSION, frame_id) + jpeg


def unpack_preview(data: bytes) -> tuple[int, int, bytes]:
    magic, version, frame_id = PREVIEW_HEADER.unpack_from(data)
    if magic != PREVIEW_MAGIC:
        raise ValueError("bad preview magic")
    return version, frame_id, data[PREVIEW_HEADER.size:]


class DryRunRequest(BaseModel):
    enabled: bool


class TestInputRequest(BaseModel):
    action: Literal["jump", "slide"]


class AdbEndpointRequest(BaseModel):
    endpoint: str
    code: Optional[str] = Field(default=None)

    @field_validator("endpoint")
    @classmethod
    def _ep(cls, v: str) -> str:
        v = v.strip()
        if not ENDPOINT_RE.match(v):
            raise ValueError("endpoint must be host:port")
        port = int(v.rsplit(":", 1)[1])
        if not 1 <= port <= 65535:
            raise ValueError("port out of range")
        return v

    @field_validator("code")
    @classmethod
    def _code(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not re.fullmatch(r"\d{6}", v.strip()):
            raise ValueError("pairing code must be 6 digits")
        return v.strip() if v else v


class ControlResponse(BaseModel):
    ok: bool
    state: str
    reasons: list[str] = []


ROLES = ("danger", "no_hearts", "tap", "result", "item_screen", "map_select", "running", "map", "roll", "item")


class ScreenEntry(BaseModel):
    """A taught autoplay screen; coords are capture-frame px (frame bounds are checked on save)."""
    name: str
    role: Literal[ROLES]
    box: list[int] = Field(min_length=4, max_length=4)
    tap: Optional[list[int]] = Field(default=None, min_length=2, max_length=2)
    threshold: float = Field(default=0.8, ge=0.5, le=0.99)
    profile: Optional[str] = None

    @field_validator("profile", mode="before")
    @classmethod
    def _blank(cls, v):
        return v or None

    @model_validator(mode="after")
    def _check(self) -> "ScreenEntry":
        if not PROFILE_NAME_RE.match(self.name):
            raise ValueError("name must match [A-Za-z0-9_-]{1,40}")
        if self.box[0] < 0 or self.box[1] < 0 or self.box[2] < 8 or self.box[3] < 8:
            raise ValueError("box must be x,y >= 0 and w,h >= 8")
        if self.role in ("tap", "result", "no_hearts", "item_screen", "roll") and self.tap is None:
            raise ValueError(f"role {self.role} needs a tap point")
        if self.profile and (self.role != "map" or not PROFILE_NAME_RE.match(self.profile)):
            raise ValueError("profile only for role map and must match [A-Za-z0-9_-]{1,40}")
        return self


class AutoplayStart(BaseModel):
    map: Optional[str] = None
    items: list[str] = []
    loops: int = Field(ge=1, le=9999)
    roll_cap: int = Field(default=10, ge=0, le=100)
    heart_wait_min: float = Field(default=12.0, ge=1, le=120)
    unknown_back_s: float = Field(default=5.0, ge=1, le=60)
    swipe: Optional[list[int]] = Field(default=None, min_length=4, max_length=4)  # frame px x1,y1,x2,y2
