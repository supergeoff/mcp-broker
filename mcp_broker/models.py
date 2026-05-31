from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    sub: Mapped[str] = mapped_column(String(255), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    litellm_key: Mapped["UserLiteLLMKey | None"] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    secrets: Mapped[list["UserSecret"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class UserLiteLLMKey(Base):
    __tablename__ = "user_litellm_keys"

    user_sub: Mapped[str] = mapped_column(ForeignKey("users.sub"), primary_key=True)
    enc_value: Mapped[str] = mapped_column(Text, nullable=False)

    user: Mapped[User] = relationship(back_populates="litellm_key")


class UserSecret(Base):
    __tablename__ = "user_secrets"
    __table_args__ = (UniqueConstraint("user_sub", "header_name", name="uq_user_secret_header"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_sub: Mapped[str] = mapped_column(ForeignKey("users.sub"), index=True)
    header_name: Mapped[str] = mapped_column(String(128), nullable=False)
    enc_value: Mapped[str] = mapped_column(Text, nullable=False)

    user: Mapped[User] = relationship(back_populates="secrets")
