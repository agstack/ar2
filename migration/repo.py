"""
Target-side repository: AR2 registry, Hub accounts, Pancake profiles.

The in-memory implementation enforces the REAL constraints read off the shipped
schemas, so violations surface here rather than as an IntegrityError halfway
through Monday's import:

  ar2 geo_ids          geo_id UNIQUE, geo_id_short UNIQUE, content_hash UNIQUE
  ar2 geo_id_regime_alias  NEW TABLE -- see the note below
  ar2-hub users        email UNIQUE NOT NULL, phone UNIQUE NOT NULL,
                       first_name/last_name/password_hash NOT NULL,
                       is_active defaults False
  pancake users        hub_account_id UNIQUE
  pancake fieldlists   UNIQUE (list_id, owner_id)
  pancake fieldlist_members  UNIQUE (fieldlist_id, geoid)

THE MISSING TABLE. `GeoIDAlias` in ar2 does NOT carry a v1 -> v2 mapping. Its
columns are (canonical_geo_id, alias_content_hash, relation): it maps a *content
hash* of resubmitted WKT to a canonical GeoID, for deduplicating resubmissions.
There is nowhere to record "v1 identifier X is now v2 identifier Y", and that
mapping is the join key for the entire user migration -- TerraPipe's profile rows
point at v1 GeoIDs. A new table is required:

    class GeoIDRegimeAlias(Base):
        __tablename__ = "geo_id_regime_alias"
        id            = Column(UUID, primary_key=True, default=uuid.uuid4)
        v1_geo_id     = Column(String(128), index=True, nullable=False, unique=True)
        v2_geo_id     = Column(String(64), ForeignKey("geo_ids.geo_id"), index=True,
                               nullable=False)
        v1_kind       = Column(String(16))    # l13_hash | l20_hash | uuid
        v1_regime     = Column(String(8), default="v1")
        v2_regime     = Column(String(8), default="v2")
        relation      = Column(Enum("same_as", "child_of", name="regime_relation_enum"))
        created_at    = Column(DateTime, default=get_utc_now)

String(128) on v1_geo_id, not 64: v1 identifiers include 36-char UUIDs as well as
64-char hashes. This table must resolve forever -- AR 1.0 issued these to real
users over years and they persist in exports, printed references and TerraPipe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ConstraintViolation(Exception):
    """A target-schema constraint would be violated. Carries the column."""

    def __init__(self, table: str, constraint: str, detail: str):
        self.table, self.constraint, self.detail = table, constraint, detail
        super().__init__(f"{table}.{constraint}: {detail}")


@dataclass
class GeoIdRow:
    geo_id: str
    geo_id_short: str
    content_hash: str
    tokens: list[str]
    blocking_keys: list[str]
    area_ha_approx: float | None = None
    country: str | None = None
    field_name: str | None = None
    crop: str | None = None
    s2_level: int = 20
    regime: str = "v2"


@dataclass
class AliasRow:
    v1_geo_id: str
    v2_geo_id: str
    v1_kind: str
    relation: str = "same_as"


@dataclass
class HubAccount:
    hub_account_id: str
    email: str
    phone: str
    first_name: str
    last_name: str
    is_active: bool = False


@dataclass
class FieldListRow:
    list_id: str
    name: str
    owner_hub_account_id: str
    geoids: list[str] = field(default_factory=list)


class TargetRepo(Protocol):
    def find_candidates(self, blocking_keys: list[str]) -> list[tuple[str, list[str]]]: ...
    def get_geoid(self, geo_id: str) -> GeoIdRow | None: ...
    def find_by_content_hash(self, content_hash: str) -> GeoIdRow | None: ...
    def insert_geoid(self, row: GeoIdRow) -> None: ...
    def upsert_alias(self, row: AliasRow) -> None: ...
    def resolve_v1(self, v1_geo_id: str) -> str | None: ...
    def upsert_hub_account(self, acct: HubAccount) -> None: ...
    def create_fieldlist(self, row: FieldListRow) -> None: ...


class InMemoryRepo:
    """Constraint-enforcing in-memory target. Used by the tests and the dry run."""

    def __init__(self) -> None:
        self.geoids: dict[str, GeoIdRow] = {}
        self.by_short: dict[str, str] = {}
        self.by_content: dict[str, str] = {}
        self.by_blocking: dict[str, set[str]] = {}
        self.aliases: dict[str, AliasRow] = {}
        self.hub_accounts: dict[str, HubAccount] = {}
        self.hub_emails: dict[str, str] = {}
        self.hub_phones: dict[str, str] = {}
        self.fieldlists: dict[tuple[str, str], FieldListRow] = {}
        self.parent_edges: dict[str, str] = {}   # child v2 geo_id -> parent v2 geo_id

    # -- AR2 registry ----------------------------------------------------

    def find_candidates(self, blocking_keys: list[str]) -> list[tuple[str, list[str]]]:
        hits: set[str] = set()
        for key in blocking_keys:
            hits |= self.by_blocking.get(key, set())
        return [(gid, self.geoids[gid].tokens) for gid in sorted(hits)]

    def get_geoid(self, geo_id: str) -> GeoIdRow | None:
        return self.geoids.get(geo_id)

    def find_by_content_hash(self, content_hash: str) -> GeoIdRow | None:
        gid = self.by_content.get(content_hash)
        return self.geoids.get(gid) if gid else None

    def insert_geoid(self, row: GeoIdRow) -> None:
        if row.geo_id in self.geoids:
            raise ConstraintViolation("geo_ids", "geo_id UNIQUE", row.geo_id)
        if row.geo_id_short in self.by_short:
            raise ConstraintViolation(
                "geo_ids", "geo_id_short UNIQUE",
                f"{row.geo_id_short} already held by {self.by_short[row.geo_id_short]}")
        if row.content_hash in self.by_content:
            raise ConstraintViolation(
                "geo_ids", "content_hash UNIQUE",
                f"identical geometry already registered as {self.by_content[row.content_hash]}")
        self.geoids[row.geo_id] = row
        self.by_short[row.geo_id_short] = row.geo_id
        self.by_content[row.content_hash] = row.geo_id
        for key in row.blocking_keys:
            self.by_blocking.setdefault(key, set()).add(row.geo_id)

    def upsert_alias(self, row: AliasRow) -> None:
        existing = self.aliases.get(row.v1_geo_id)
        if existing and existing.v2_geo_id != row.v2_geo_id:
            raise ConstraintViolation(
                "geo_id_regime_alias", "v1_geo_id UNIQUE",
                f"{row.v1_geo_id} already maps to {existing.v2_geo_id}, "
                f"cannot remap to {row.v2_geo_id}")
        self.aliases[row.v1_geo_id] = row

    def resolve_v1(self, v1_geo_id: str) -> str | None:
        a = self.aliases.get(v1_geo_id)
        return a.v2_geo_id if a else None

    def record_parent(self, child_geo_id: str, parent_geo_id: str) -> None:
        """Record nesting (P6). The child keeps its own identity."""
        if child_geo_id == parent_geo_id:
            raise ConstraintViolation("geo_id_parent_edge", "child != parent",
                                      child_geo_id)
        self.parent_edges[child_geo_id] = parent_geo_id

    # -- Hub + Pancake ---------------------------------------------------

    def upsert_hub_account(self, acct: HubAccount) -> None:
        if not acct.email:
            raise ConstraintViolation("hub.users", "email NOT NULL", acct.hub_account_id)
        if not acct.first_name or not acct.last_name:
            raise ConstraintViolation(
                "hub.users", "first_name/last_name NOT NULL", acct.hub_account_id)

        prior_email = self.hub_emails.get(acct.email)
        if prior_email and prior_email != acct.hub_account_id:
            raise ConstraintViolation("hub.users", "email UNIQUE",
                                      f"{acct.email} already held by {prior_email}")
        
        self.hub_accounts[acct.hub_account_id] = acct
        self.hub_emails[acct.email] = acct.hub_account_id
        if acct.phone:
            self.hub_phones[acct.phone] = acct.hub_account_id

    def create_fieldlist(self, row: FieldListRow) -> None:
        key = (row.list_id, row.owner_hub_account_id)
        if key in self.fieldlists:
            return  # UNIQUE (list_id, owner_id): idempotent re-run, not an error
        if row.owner_hub_account_id not in self.hub_accounts:
            raise ConstraintViolation("pancake.fieldlists", "owner_id FK",
                                      row.owner_hub_account_id)
        self.fieldlists[key] = row

    # -- introspection for reports --------------------------------------

    def owners_of(self, geo_id: str) -> set[str]:
        return {
            fl.owner_hub_account_id
            for fl in self.fieldlists.values()
            if geo_id in fl.geoids
        }

    def multi_owner_geoids(self) -> dict[str, set[str]]:
        counts: dict[str, set[str]] = {}
        for fl in self.fieldlists.values():
            for gid in fl.geoids:
                counts.setdefault(gid, set()).add(fl.owner_hub_account_id)
        return {g: o for g, o in counts.items() if len(o) > 1}
