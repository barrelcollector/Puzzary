from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Table, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


puzzle_tag_links = Table(
    "puzzle_tag_links",
    Base.metadata,
    Column("puzzle_id", ForeignKey("puzzles.id"), primary_key=True),
    Column("tag_id", ForeignKey("puzzle_tags.id"), primary_key=True),
)


word_bank_tag_links = Table(
    "word_bank_tag_links",
    Base.metadata,
    Column("word_bank_id", ForeignKey("word_banks.id"), primary_key=True),
    Column("tag_id", ForeignKey("puzzle_tags.id"), primary_key=True),
)


dictionary_word_tag_links = Table(
    "dictionary_word_tag_links",
    Base.metadata,
    Column("word_id", ForeignKey("dictionary_words.id"), primary_key=True),
    Column("tag_id", ForeignKey("puzzle_tags.id"), primary_key=True),
)


dictionary_definition_tag_links = Table(
    "dictionary_definition_tag_links",
    Base.metadata,
    Column("definition_id", ForeignKey("dictionary_definitions.id"), primary_key=True),
    Column("tag_id", ForeignKey("puzzle_tags.id"), primary_key=True),
)


user_role_links = Table(
    'user_role_links', Base.metadata,
    Column('user_id', ForeignKey('users.id'), primary_key=True),
    Column('role_id', ForeignKey('permission_roles.id'), primary_key=True),
)


class PermissionRole(Base):
    __tablename__ = 'permission_roles'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    permissions: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(String(255), default='')


class PermissionAudit(Base):
    __tablename__ = 'permission_audit'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    subject_type: Mapped[str] = mapped_column(String(40))
    subject_id: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(80))
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    reset_token_hash: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    reset_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    api_token_hash: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    api_token_prefix: Mapped[str] = mapped_column(String(12), default="")
    api_token_last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    permissions_initialized: Mapped[bool] = mapped_column(Boolean, default=False)
    permission_overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    roles: Mapped[list[PermissionRole]] = relationship(secondary=user_role_links)

    def can(self, permission: str) -> bool:
        from app.permissions import effective_permissions
        return permission in effective_permissions(self)

    @property
    def can_manage(self) -> bool:
        from app.permissions import management_access
        return management_access(self)

    attempts: Mapped[list[PuzzleAttempt]] = relationship(back_populates="user")
    progress: Mapped[list[PuzzleProgress]] = relationship(back_populates="user")
    photos: Mapped[list[PuzzlePhoto]] = relationship(back_populates="user")
    created_puzzles: Mapped[list[Puzzle]] = relationship(back_populates="creator")


class Puzzle(Base):
    __tablename__ = "puzzles"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    puzzle_type: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(160))
    theme: Mapped[str] = mapped_column(String(80), index=True)
    difficulty: Mapped[str] = mapped_column(String(40), index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("puzzle_categories.id"), nullable=True, index=True)
    word_bank_id: Mapped[int | None] = mapped_column(ForeignKey("word_banks.id"), nullable=True, index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    prompt: Mapped[str] = mapped_column(Text, default="")
    print_payload: Mapped[dict] = mapped_column(JSON)
    play_payload: Mapped[dict] = mapped_column(JSON)
    solution_payload: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(80), default="local")
    puzzle_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    auto_archive_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    category: Mapped[PuzzleCategory | None] = relationship(back_populates="puzzles")
    word_bank: Mapped[WordBank | None] = relationship(back_populates="puzzles")
    creator: Mapped[User | None] = relationship(back_populates="created_puzzles")
    tags: Mapped[list[PuzzleTag]] = relationship(secondary=puzzle_tag_links, back_populates="puzzles")
    hints: Mapped[list[PuzzleHint]] = relationship(back_populates="puzzle", cascade="all, delete-orphan")
    attempts: Mapped[list[PuzzleAttempt]] = relationship(back_populates="puzzle", cascade="all, delete-orphan")
    progress: Mapped[list[PuzzleProgress]] = relationship(back_populates="puzzle", cascade="all, delete-orphan")
    photos: Mapped[list[PuzzlePhoto]] = relationship(back_populates="puzzle", cascade="all, delete-orphan")
    group_solves: Mapped[list[GroupSolve]] = relationship(cascade="all, delete-orphan")


class GroupSolve(Base):
    __tablename__ = "group_solves"
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    code: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    puzzle_id: Mapped[str] = mapped_column(ForeignKey("puzzles.id"), index=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    participant_limit: Mapped[int] = mapped_column(Integer, default=0)
    approval_required: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_guests: Mapped[bool] = mapped_column(Boolean, default=False)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    cell_versions: Mapped[dict] = mapped_column(JSON, default=dict)
    puzzle_states: Mapped[dict] = mapped_column(JSON, default=dict)
    puzzle_started_revision: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    members: Mapped[list[GroupMember]] = relationship(cascade="all, delete-orphan")
    actions: Mapped[list[GroupAction]] = relationship(cascade="all, delete-orphan")


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_group_member"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("group_solves.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    guest_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    guest_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    focus: Mapped[str] = mapped_column(String(80), default="")
    presence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GroupAction(Base):
    __tablename__ = "group_actions"
    __table_args__ = (UniqueConstraint("group_id", "action_id", name="uq_group_action"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("group_solves.id"), index=True)
    action_id: Mapped[str] = mapped_column(String(36))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    guest_member_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    changes: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PublishedIssue(Base):
    __tablename__ = "published_issues"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    modules: Mapped[list] = mapped_column(JSON, default=list)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PuzzleHint(Base):
    __tablename__ = "puzzle_hints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    puzzle_id: Mapped[str] = mapped_column(ForeignKey("puzzles.id"), index=True)
    order: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    puzzle: Mapped[Puzzle] = relationship(back_populates="hints")


class PuzzleAttempt(Base):
    __tablename__ = "puzzle_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    puzzle_id: Mapped[str] = mapped_column(ForeignKey("puzzles.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    answer: Mapped[str] = mapped_column(Text)
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    puzzle: Mapped[Puzzle] = relationship(back_populates="attempts")
    user: Mapped[User | None] = relationship(back_populates="attempts")


class PuzzleProgress(Base):
    __tablename__ = "puzzle_progress"
    __table_args__ = (UniqueConstraint("puzzle_id", "user_id", name="uq_puzzle_progress_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    puzzle_id: Mapped[str] = mapped_column(ForeignKey("puzzles.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    progress_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    puzzle: Mapped[Puzzle] = relationship(back_populates="progress")
    user: Mapped[User] = relationship(back_populates="progress")


class PuzzlePhoto(Base):
    __tablename__ = "puzzle_photos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    puzzle_id: Mapped[str] = mapped_column(ForeignKey("puzzles.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    file_path: Mapped[str] = mapped_column(String(500))
    original_filename: Mapped[str] = mapped_column(String(255))
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    puzzle: Mapped[Puzzle] = relationship(back_populates="photos")
    user: Mapped[User | None] = relationship(back_populates="photos")


class PuzzleDictionary(Base):
    __tablename__ = "puzzle_dictionaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    words: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WordBank(Base):
    __tablename__ = "word_banks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("puzzle_categories.id"), nullable=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    category: Mapped[PuzzleCategory | None] = relationship(back_populates="word_banks")
    tags: Mapped[list[PuzzleTag]] = relationship(secondary=word_bank_tag_links, back_populates="word_banks")
    word_links: Mapped[list[WordBankWord]] = relationship(
        back_populates="word_bank",
        cascade="all, delete-orphan",
        order_by="WordBankWord.position",
    )
    puzzles: Mapped[list[Puzzle]] = relationship(back_populates="word_bank")


class DictionaryWord(Base):
    __tablename__ = "dictionary_words"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_text: Mapped[str] = mapped_column(String(80))
    clue: Mapped[str] = mapped_column(Text, default="")
    part_of_speech: Mapped[str] = mapped_column(String(40), default="")
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    banned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tags: Mapped[list[PuzzleTag]] = relationship(secondary=dictionary_word_tag_links, back_populates="dictionary_words")
    bank_links: Mapped[list[WordBankWord]] = relationship(back_populates="word", cascade="all, delete-orphan")
    definitions: Mapped[list[DictionaryDefinition]] = relationship(
        back_populates="word",
        cascade="all, delete-orphan",
        order_by="DictionaryDefinition.position",
    )


class DictionaryDefinition(Base):
    __tablename__ = "dictionary_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word_id: Mapped[int] = mapped_column(ForeignKey("dictionary_words.id"), index=True)
    definition: Mapped[str] = mapped_column(Text)
    part_of_speech: Mapped[str] = mapped_column(String(40), default="")
    example: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(120), default="local")
    position: Mapped[int] = mapped_column(Integer, default=0)
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    word: Mapped[DictionaryWord] = relationship(back_populates="definitions")
    tags: Mapped[list[PuzzleTag]] = relationship(secondary=dictionary_definition_tag_links, back_populates="dictionary_definitions")


class WordBankWord(Base):
    __tablename__ = "word_bank_words"
    __table_args__ = (UniqueConstraint("word_bank_id", "word_id", name="uq_word_bank_word"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word_bank_id: Mapped[int] = mapped_column(ForeignKey("word_banks.id"), index=True)
    word_id: Mapped[int] = mapped_column(ForeignKey("dictionary_words.id"), index=True)
    definition_id: Mapped[int | None] = mapped_column(ForeignKey("dictionary_definitions.id"), nullable=True, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    word_bank: Mapped[WordBank] = relationship(back_populates="word_links")
    word: Mapped[DictionaryWord] = relationship(back_populates="bank_links")
    definition: Mapped[DictionaryDefinition | None] = relationship()


class PuzzleCategory(Base):
    __tablename__ = "puzzle_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    puzzles: Mapped[list[Puzzle]] = relationship(back_populates="category")
    word_banks: Mapped[list[WordBank]] = relationship(back_populates="category")


class PuzzleTag(Base):
    __tablename__ = "puzzle_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    tag_type: Mapped[str] = mapped_column(String(80), default="General", index=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    puzzles: Mapped[list[Puzzle]] = relationship(secondary=puzzle_tag_links, back_populates="tags")
    word_banks: Mapped[list[WordBank]] = relationship(secondary=word_bank_tag_links, back_populates="tags")
    dictionary_words: Mapped[list[DictionaryWord]] = relationship(secondary=dictionary_word_tag_links, back_populates="tags")
    dictionary_definitions: Mapped[list[DictionaryDefinition]] = relationship(
        secondary=dictionary_definition_tag_links,
        back_populates="tags",
    )

    @property
    def label(self) -> str:
        if not self.tag_type or self.tag_type == "General":
            return self.name
        return f"{self.tag_type}: {self.name}"


class ContentItem(Base):
    __tablename__ = "content_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_type: Mapped[str] = mapped_column(String(60), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(120), default="local")
    item_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DictionaryImportSource(Base):
    __tablename__ = "dictionary_import_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    source_url: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(40), default="auto")
    source_label: Mapped[str] = mapped_column(String(160), default="")
    source_api_token: Mapped[str] = mapped_column(Text, default="")
    sync_interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    max_items: Mapped[int] = mapped_column(Integer, default=500)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
