"""Validate the website boundary and retain only fields needed for retrieval."""

import hashlib
import json
import re
import unicodedata

from pydantic import BaseModel, Field, field_validator

BASE_URL = "https://www.agnoshealth.com"


def clean_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = value.translate(dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff")))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()).strip()


class Tag(BaseModel):
    name: str
    type: str = "unknown"
    disease_key: str | None = None
    symptom_key: str | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return clean_text(value)


class DoctorAnswer(BaseModel):
    id: int = Field(gt=0)
    content_text: str
    pub_date: str | None = None
    edit_date: str | None = None

    @field_validator("content_text")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        return clean_text(value)


class SourceForum(BaseModel):
    id: int = Field(gt=0)
    content_text: str = Field(min_length=1)
    pub_date: str | None = None
    user_age: int | None = Field(default=None, ge=0)
    user_gender: str | None = None
    disease_key: str | None = None
    disease_text: str | None = None
    default_tags: list[Tag]
    custom_tags: list[Tag]
    is_doctor_answered: bool


class SourceEntry(BaseModel):
    # Other fields (including ordinary comments and author profiles) are ignored.
    forum: SourceForum
    doctor_comments: list[DoctorAnswer]


class ListingPage(BaseModel):
    forums: list[dict]
    total: int = Field(ge=0)
    page: int = Field(ge=1)


class ForumPost(BaseModel):
    id: int
    url: str
    question: str
    published_at: str | None
    age: int | None
    gender: str | None
    disease_key: str | None
    disease: str | None
    tags: list[Tag]
    doctor_answers: list[DoctorAnswer]
    source_answered: bool

    @property
    def has_answer(self) -> bool:
        return self.source_answered and bool(self.doctor_answers)

    @property
    def content_hash(self) -> str:
        # Engagement, crawl times and edit timestamps do not change the embedding.
        content = self.model_dump(exclude={"url", "published_at", "doctor_answers"})
        content["doctor_answers"] = [
            {"id": answer.id, "content_text": answer.content_text} for answer in self.doctor_answers
        ]
        encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse_entry(raw: dict) -> ForumPost:
    entry = SourceEntry.model_validate(raw)
    forum = entry.forum
    question = clean_text(forum.content_text)
    if not question:
        raise ValueError("empty_question")
    answers = sorted((a for a in entry.doctor_comments if a.content_text), key=lambda a: a.id)
    if forum.is_doctor_answered and not answers:
        # Never overwrite a complete stored answer with an incomplete website response.
        raise ValueError("answered_without_doctor_text")
    unique_tags = {tag.model_dump_json(): tag for tag in forum.default_tags + forum.custom_tags if tag.name}
    tags = [unique_tags[key] for key in sorted(unique_tags)]
    return ForumPost(
        id=forum.id,
        url=f"{BASE_URL}/forums/x/{forum.id}",
        question=question,
        published_at=forum.pub_date,
        age=forum.user_age,
        gender=forum.user_gender,
        disease_key=forum.disease_key,
        disease=clean_text(forum.disease_text) if forum.disease_text else None,
        tags=tags,
        doctor_answers=answers,
        source_answered=forum.is_doctor_answered,
    )
