"""
SQLAlchemy-backed TargetRepo.

Three separate databases, because that is how the system is deployed: the AR2
registry, the Hub account store, and Pancake. Pass three Sessions; they may point
at the same SQLite file in a test and at three servers in production.

Enforces exactly the same constraints as InMemoryRepo, translating the database's
IntegrityError into ConstraintViolation so callers handle one exception type and
the reports read identically whichever repo is in use.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    FieldList,
    GeoID,
    GeoIDBlockingCell,
    GeoIDParentEdge,
    GeoIDRegimeAlias,
    HubUser,
    ImportCheckpoint,
    ListArtifact,
    ListMemberEdge,
    PancakeUser,
)
from .repo import (
    AliasRow,
    ConstraintViolation,
    FieldListRow,
    GeoIdRow,
    HubAccount,
)

# Migrated accounts have no password. This is not a usable credential -- it is a
# non-verifying placeholder so the NOT NULL constraint is satisfied, and every
# such account is created inactive and must go through password reset.
UNUSABLE_PASSWORD = "!migrated-no-login"


class SqlAlchemyRepo:
    """TargetRepo over real databases. Same semantics as InMemoryRepo."""

    def __init__(self, ar2: Session, hub: Session, pancake: Session,
                 *, batch_size: int = 500):
        self.ar2 = ar2
        self.hub = hub
        self.pancake = pancake
        self.batch_size = batch_size
        self._pending = 0

    # -- AR2 registry ----------------------------------------------------

    def find_candidates(self, blocking_keys: list[str]) -> list[tuple[str, list[str]]]:
        if not blocking_keys:
            return []
        geo_ids = self.ar2.scalars(
            select(GeoIDBlockingCell.geo_id)
            .where(GeoIDBlockingCell.cell_token.in_(blocking_keys))
            .distinct()
        ).all()
        if not geo_ids:
            return []
        rows = self.ar2.execute(
            select(GeoID.geo_id, GeoID.geo_data).where(GeoID.geo_id.in_(geo_ids))
        ).all()
        out = []
        for geo_id, geo_data in rows:
            tokens = (geo_data or {}).get("20") or (geo_data or {}).get("v2") or []
            if tokens:
                out.append((geo_id, list(tokens)))
        return out

    def get_geoid(self, geo_id: str) -> GeoIdRow | None:
        row = self.ar2.scalar(select(GeoID).where(GeoID.geo_id == geo_id))
        return _to_geoid_row(row) if row else None

    def find_by_content_hash(self, content_hash: str) -> GeoIdRow | None:
        row = self.ar2.scalar(select(GeoID).where(GeoID.content_hash == content_hash))
        return _to_geoid_row(row) if row else None

    def insert_geoid(self, row: GeoIdRow) -> None:
        # Pre-check so the reports name the constraint, rather than surfacing a
        # dialect-specific IntegrityError string.
        for column, value, label in (
            (GeoID.geo_id, row.geo_id, "geo_id UNIQUE"),
            (GeoID.geo_id_short, row.geo_id_short, "geo_id_short UNIQUE"),
            (GeoID.content_hash, row.content_hash, "content_hash UNIQUE"),
        ):
            if self.ar2.scalar(select(GeoID.geo_id).where(column == value)):
                raise ConstraintViolation("geo_ids", label, str(value))

        self.ar2.add(GeoID(
            geo_id=row.geo_id,
            geo_id_short=row.geo_id_short,
            content_hash=row.content_hash,
            field_name=row.field_name,
            country=row.country,
            area_ha_approx=row.area_ha_approx,
            crop=row.crop,
            s2_level=row.s2_level,
            s2_cells=row.tokens,
            geo_data={"v2": row.tokens, "13": row.blocking_keys},
            mask_level="L0",
        ))
        self.ar2.add_all([
            GeoIDBlockingCell(geo_id=row.geo_id, cell_token=token)
            for token in row.blocking_keys
        ])
        self._maybe_flush()

    def upsert_alias(self, row: AliasRow) -> None:
        existing = self.ar2.scalar(
            select(GeoIDRegimeAlias).where(GeoIDRegimeAlias.v1_geo_id == row.v1_geo_id)
        )
        if existing is not None:
            if existing.v2_geo_id != row.v2_geo_id:
                raise ConstraintViolation(
                    "geo_id_regime_alias", "v1_geo_id UNIQUE",
                    f"{row.v1_geo_id} already maps to {existing.v2_geo_id}, "
                    f"cannot remap to {row.v2_geo_id}")
            return
        self.ar2.add(GeoIDRegimeAlias(
            v1_geo_id=row.v1_geo_id,
            v2_geo_id=row.v2_geo_id,
            v1_kind=row.v1_kind,
            relation=row.relation,
        ))
        self._maybe_flush()

    def resolve_v1(self, v1_geo_id: str) -> str | None:
        return self.ar2.scalar(
            select(GeoIDRegimeAlias.v2_geo_id)
            .where(GeoIDRegimeAlias.v1_geo_id == v1_geo_id)
        )

    def record_parent(self, child_geo_id: str, parent_geo_id: str) -> None:
        if child_geo_id == parent_geo_id:
            raise ConstraintViolation("geo_id_parent_edge", "child != parent",
                                      child_geo_id)
        exists = self.ar2.scalar(
            select(GeoIDParentEdge.id).where(
                GeoIDParentEdge.child_geo_id == child_geo_id,
                GeoIDParentEdge.parent_geo_id == parent_geo_id,
            )
        )
        if exists:
            return
        self.ar2.add(GeoIDParentEdge(child_geo_id=child_geo_id,
                                     parent_geo_id=parent_geo_id))
        self._maybe_flush()

    # -- Hub + Pancake ---------------------------------------------------

    def upsert_hub_account(self, acct: HubAccount) -> None:
        if not acct.email:
            raise ConstraintViolation("hub.users", "email NOT NULL", acct.hub_account_id)
        if not acct.first_name or not acct.last_name:
            raise ConstraintViolation("hub.users", "first_name/last_name NOT NULL",
                                      acct.hub_account_id)

        # client_id is String(50) in the hub. Postgres truncates nothing -- it
        # raises -- so catch it here where the report can name the account.
        if len(acct.hub_account_id) > 50:
            raise ConstraintViolation("hub.users", "client_id length <= 50",
                                      acct.hub_account_id)

        existing = self.hub.scalar(
            select(HubUser).where(HubUser.client_id == acct.hub_account_id))
        if existing is not None:
            return

        clash = self.hub.scalar(select(HubUser).where(HubUser.email == acct.email))
        if clash is not None:
            raise ConstraintViolation("hub.users", "email UNIQUE",
                                      f"{acct.email} already held by {clash.client_id}")

        self.hub.add(HubUser(
            first_name=acct.first_name,
            last_name=acct.last_name,
            email=acct.email,
            phone=acct.phone,
            password_hash=UNUSABLE_PASSWORD,
            client_id=acct.hub_account_id,
            is_active=False,
        ))
        try:
            self.hub.flush()
        except IntegrityError as exc:
            self.hub.rollback()
            raise ConstraintViolation("hub.users", "integrity", str(exc.orig)) from exc

        # Pancake mirrors the hub account on first authenticated request; the
        # import creates it up front so the run is deterministic.
        if not self.pancake.scalar(
            select(PancakeUser).where(PancakeUser.hub_account_id == acct.hub_account_id)
        ):
            self.pancake.add(PancakeUser(hub_account_id=acct.hub_account_id,
                                         email=acct.email))
            self.pancake.flush()

    def create_fieldlist(self, row: FieldListRow) -> None:
        owner = self.pancake.scalar(
            select(PancakeUser).where(
                PancakeUser.hub_account_id == row.owner_hub_account_id)
        )
        if owner is None:
            raise ConstraintViolation("pancake.fieldlists", "owner_id FK",
                                      row.owner_hub_account_id)

        existing = self.pancake.scalar(
            select(FieldList).where(FieldList.list_id == row.list_id,
                                    FieldList.owner_id == owner.id)
        )
        if existing is not None:
            return  # UNIQUE (list_id, owner_id): idempotent re-run

        fl = FieldList(list_id=row.list_id, name=row.name, owner_id=owner.id)
        self.pancake.add(fl)
        
        la = self.ar2.scalar(
            select(ListArtifact).where(ListArtifact.list_id == row.list_id)
        )
        if la is None:
            la = ListArtifact(list_id=row.list_id)
            self.ar2.add(la)
            self.ar2.add_all([
                ListMemberEdge(list_id=row.list_id, geoid=g) for g in set(row.geoids)
            ])
            self.ar2.flush()
        
        self.pancake.flush()

    # -- checkpointing ---------------------------------------------------

    def checkpoint(self, phase: str, processed: int, last_source_id: str | None = None,
                   note: str | None = None) -> None:
        row = self.pancake.scalar(
            select(ImportCheckpoint).where(ImportCheckpoint.phase == phase))
        if row is None:
            row = ImportCheckpoint(phase=phase)
            self.pancake.add(row)
        row.processed = processed
        row.last_source_id = last_source_id
        row.note = note
        self.commit()

    def commit(self) -> None:
        self.ar2.commit()
        self.hub.commit()
        self.pancake.commit()
        self._pending = 0

    def _maybe_flush(self) -> None:
        self._pending += 1
        if self._pending >= self.batch_size:
            self.commit()

    # -- introspection for reports --------------------------------------

    def owners_of(self, geo_id: str) -> set[str]:
        list_ids = self.ar2.scalars(
            select(ListMemberEdge.list_id)
            .where(ListMemberEdge.geoid == geo_id)
        ).all()
        if not list_ids:
            return set()
            
        rows = self.pancake.execute(
            select(PancakeUser.hub_account_id)
            .join(FieldList, FieldList.owner_id == PancakeUser.id)
            .where(FieldList.list_id.in_(list_ids))
            .distinct()
        ).all()
        return {r[0] for r in rows}

    def multi_owner_geoids(self) -> dict[str, set[str]]:
        edges = self.ar2.execute(
            select(ListMemberEdge.geoid, ListMemberEdge.list_id)
        ).all()
        list_ids = {e.list_id for e in edges}
        
        if not list_ids:
            return {}
            
        owners = self.pancake.execute(
            select(FieldList.list_id, PancakeUser.hub_account_id)
            .join(PancakeUser, FieldList.owner_id == PancakeUser.id)
            .where(FieldList.list_id.in_(list_ids))
        ).all()
        list_owners = {}
        for lid, acc in owners:
            list_owners.setdefault(lid, set()).add(acc)
            
        by_geo: dict[str, set[str]] = {}
        for geo_id, list_id in edges:
            for acc in list_owners.get(list_id, set()):
                by_geo.setdefault(geo_id, set()).add(acc)
        return {g: o for g, o in by_geo.items() if len(o) > 1}

    @property
    def aliases(self) -> dict:
        """Presence check used by import_profiles() to enforce phase ordering."""
        count = self.ar2.scalar(select(GeoIDRegimeAlias.id).limit(1))
        return {"_": count} if count else {}


def _to_geoid_row(row: GeoID) -> GeoIdRow:
    geo_data = row.geo_data or {}
    return GeoIdRow(
        geo_id=row.geo_id,
        geo_id_short=row.geo_id_short,
        content_hash=row.content_hash,
        tokens=list(geo_data.get("v2") or []),
        blocking_keys=list(geo_data.get("13") or []),
        area_ha_approx=row.area_ha_approx,
        country=row.country,
        field_name=row.field_name,
        crop=row.crop,
        s2_level=row.s2_level or 20,
    )
