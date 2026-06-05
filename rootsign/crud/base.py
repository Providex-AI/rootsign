from __future__ import annotations

from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

ModelType = TypeVar("ModelType")
CreateSchemaType = TypeVar("CreateSchemaType", bound=BaseModel)


class CRUDBase(Generic[ModelType, CreateSchemaType]):
    """Generic async CRUD operations. The PK column name is inferred per entity."""

    def __init__(self, model: type[ModelType], pk_attr: str):
        self.model = model
        self.pk_attr = pk_attr

    async def get(self, db: AsyncSession, id: UUID) -> ModelType | None:
        stmt = select(self.model).where(getattr(self.model, self.pk_attr) == id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_multi(
        self, db: AsyncSession, *, skip: int = 0, limit: int = 100
    ) -> list[ModelType]:
        stmt = select(self.model).offset(skip).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def create(
        self, db: AsyncSession, *, obj_in: CreateSchemaType, **overrides: Any
    ) -> ModelType:
        payload = self._payload_from_schema(obj_in)
        payload.update(overrides)
        db_obj = self.model(**payload)
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def update(
        self, db: AsyncSession, *, db_obj: ModelType, obj_in: dict[str, Any] | BaseModel
    ) -> ModelType:
        data = obj_in.model_dump(exclude_unset=True) if isinstance(obj_in, BaseModel) else dict(obj_in)
        for field, value in data.items():
            setattr(db_obj, field, value)
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def delete(self, db: AsyncSession, *, id: UUID) -> ModelType | None:
        db_obj = await self.get(db, id)
        if db_obj is None:
            return None
        await db.execute(
            delete(self.model).where(getattr(self.model, self.pk_attr) == id)
        )
        await db.flush()
        return db_obj

    def _payload_from_schema(self, obj_in: BaseModel) -> dict[str, Any]:
        """Translate Pydantic schema fields into ORM constructor kwargs.

        Pydantic schemas use `metadata`; the ORM attribute is `extra_metadata`
        (to avoid colliding with SQLAlchemy Base.metadata).
        """
        payload = obj_in.model_dump()
        if "metadata" in payload and hasattr(self.model, "extra_metadata"):
            payload["extra_metadata"] = payload.pop("metadata")
        return payload
