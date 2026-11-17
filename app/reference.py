"""Reference data: closed catalogs, canonical suppliers with their aliases, and review rules.

It is configuration, not data that grows, so it is loaded from `data/reference_data.json` at
startup and kept in memory.
"""

from pathlib import Path

from pydantic import BaseModel

from app.normalization import supplier_name_key


class CanonicalSupplier(BaseModel):
    canonical_supplier: str
    country: str
    supplier_tax_id: str
    known_aliases: list[str]
    typical_category: str
    typical_business_unit: str

    def name_variants(self) -> dict[str, str]:
        """Matching key -> spelling, for the canonical name and every known alias."""
        variants: dict[str, str] = {}
        for spelling in [self.canonical_supplier, *self.known_aliases]:
            key = supplier_name_key(spelling)
            if key is not None:
                variants.setdefault(key, spelling)
        return variants


class ReviewRule(BaseModel):
    rule_id: str
    description: str


class CountryHint(BaseModel):
    country: str
    notes: str


class ReferenceData(BaseModel):
    categories: list[str]
    business_units: list[str]
    canonical_suppliers: list[CanonicalSupplier]
    review_rules: list[ReviewRule]
    country_hints: list[CountryHint]

    def find_supplier(self, name: str) -> CanonicalSupplier | None:
        """The reference supplier whose canonical name has the same matching key as `name`.

        Only canonical names count, not aliases: it answers "is this a known canonical name,
        written differently?", e.g. 'Orange Espagne SA' for ORANGE ESPAGNE SA.
        """
        key = supplier_name_key(name)
        for supplier in self.canonical_suppliers:
            if supplier_name_key(supplier.canonical_supplier) == key:
                return supplier
        return None


def load_reference_data(path: Path) -> ReferenceData:
    return ReferenceData.model_validate_json(path.read_text(encoding="utf-8"))
