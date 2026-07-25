from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator
from urllib.parse import urlparse


class AuditStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class AuditRequest(BaseModel):
    url: str = Field(..., description="Absolute URL to audit, e.g. https://example.com")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("url must not be empty")
        if len(value) > 2048:
            raise ValueError("url exceeds maximum length of 2048 characters")

        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("url must use http or https scheme")
        if not parsed.netloc:
            raise ValueError("url must include a host")
        return value


class AuditResult(BaseModel):
    url: str
    status_code: Optional[int] = None
    response_time_ms: Optional[float] = None
    content_type: Optional[str] = None
    content_length_bytes: Optional[int] = None
    title: Optional[str] = None
    has_hsts: Optional[bool] = None
    has_content_security_policy: Optional[bool] = None
    error: Optional[str] = None


class AuditJobResponse(BaseModel):
    job_id: str
    status: AuditStatus
    url: str
    cached: bool = False
    result: Optional[AuditResult] = None


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    request_id: str
    error: ErrorDetail
