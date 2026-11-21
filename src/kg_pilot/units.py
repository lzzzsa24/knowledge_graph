"""Conservative unit normalization: never infer a value from a range or prose."""
import re
import pint
from kg_pilot.pipeline import PILOT, jsonl, write_json

REGISTRY = pint.UnitRegistry()
ALIASES = {"nm": "nanometer", "µm": "micrometer", "μm": "micrometer", "um": "micrometer",
           "mm": "millimeter", "°C": "degC", "◦C": "degC", "C": "degC",
           "J/m2": "joule/meter**2", "J/m²": "joule/meter**2", "mΩ": "milliohm", "mΩ": "milliohm",
           "Ω": "ohm", "Ω": "ohm", "MPa": "megapascal", "GPa": "gigapascal", "W": "watt",
           "h": "hour", "min": "minute", "s": "second", "%": "percent", "eV": "electron_volt"}


def normalize_single(value_text, unit):
    if not unit or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value_text.strip()):
        return {"status": "not_a_single_explicit_numeric_quantity"}
    try:
        q = REGISTRY.Quantity(float(value_text), ALIASES.get(unit, unit)).to_base_units()
        return {"status": "normalized", "value_si": q.magnitude, "unit_si": str(q.units)}
    except Exception as exc:
        return {"status": "unit_requires_explicit_mapping", "reason": str(exc)}


def main():
    rows = [{"claim_id": c["claim_id"], "original_value": c["value_text"], "original_unit": c["unit"],
             **normalize_single(c["value_text"], c["unit"])} for c in jsonl(PILOT / "annotations" / "claims.jsonl")]
    write_json(PILOT / "reports" / "unit_normalization.json", rows)
    print("Normalized", sum(x["status"] == "normalized" for x in rows), "of", len(rows), "claims; composite values left intact")


if __name__ == "__main__":
    main()
