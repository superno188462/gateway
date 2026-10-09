"""输入规范与供应商无关；供应商适配器负责转换成具体上游协议。"""

import ipaddress
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TextEmbeddingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=100_000)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text 不能为空")
        return value


class ImageUrl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8_000)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return validate_media_url(value)


class VideoUrl(ImageUrl):
    fps: float | None = Field(default=None, ge=0.2, le=5)
    max_frame_tokens: int | None = Field(default=None, ge=128, le=640)
    max_video_tokens: int | None = Field(default=None, ge=10_240, le=204_800)
    min_frame_tokens: int | None = Field(default=None, ge=16, le=128)
    min_frames: int | None = Field(default=None, ge=5, le=16)

    @field_validator("url")
    @classmethod
    def validate_video_extension(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() != "https" or not parsed.path.lower().endswith(
            (".mp4", ".avi", ".mov")
        ):
            raise ValueError("视频 URL 必须是 HTTPS，且路径以 .mp4、.avi 或 .mov 结尾")
        return value


class ImageEmbeddingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["image_url"]
    image_url: ImageUrl


class VideoEmbeddingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["video_url"]
    video_url: VideoUrl


EmbeddingInput = Annotated[
    TextEmbeddingInput | ImageEmbeddingInput | VideoEmbeddingInput,
    Field(discriminator="type"),
]


def validate_media_url(value: str) -> str:
    cleaned = value.strip()
    parsed = urlsplit(cleaned)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(char.isspace() for char in cleaned)
    ):
        raise ValueError("媒体地址必须是可公开访问的 HTTPS URL")
    hostname = parsed.hostname.casefold()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        raise ValueError("媒体地址必须指向公网可访问的主机")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (not address.is_global):
        raise ValueError("媒体地址必须指向公网可访问的主机")
    return cleaned


def input_modality(inputs: list[str | dict[str, object]]) -> str:
    modalities: set[str] = set()
    for item in inputs:
        if isinstance(item, str):
            modalities.add("text")
        else:
            modalities.add(
                {"text": "text", "image_url": "image", "video_url": "video"}.get(
                    str(item.get("type")), "unknown"
                )
            )
    return next(iter(modalities)) if len(modalities) == 1 else "mixed"
