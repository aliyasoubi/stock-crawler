"""Build the browser script from a reviewed company selection, not a pasted giant list."""
import json
from pathlib import Path
from .kap import SourceError
from .kap_export import FIELDS


def build_browser_script(identities, years, *, max_requests=5, batch_size=25, periods=None, items=None):
    if not identities or len({i.ticker for i in identities}) != len(identities):
        raise SourceError("company selection must be nonempty and unique")
    if not years or len(years) > 5 or len(set(years)) != len(years) or any(y < 2000 or y > 2100 for y in years):
        raise SourceError("select 1-5 unique years between 2000 and 2100")
    if not 1 <= max_requests <= 20 or not 1 <= batch_size <= 25:
        raise SourceError("max requests must be 1-20 and batch size 1-25")
    periods = [4] if periods is None else periods
    items = [v[1] for v in FIELDS.values()] if items is None else items
    if not periods or len(set(periods)) != len(periods) or any(type(p) is not int or p not in (1, 2, 3, 4) for p in periods):
        raise SourceError("periods must be unique integers in 1..4")
    if not items or any(not isinstance(i, str) or not i for i in items):
        raise SourceError("items must contain captured KAP item IDs")
    config = {"companies": [i.model_dump(include={"ticker", "source_company_id", "company_name"}) for i in sorted(identities, key=lambda i: i.ticker)],
        "years": sorted(years), "periods": sorted(periods), "items": list(dict.fromkeys(items)), "maxRequests": max_requests, "batchSize": batch_size}
    root = Path(__file__).parent / "browser"
    # One lexical scope makes pasting a regenerated script into the same console safe.
    return "{\nconst KAP_EXPORT_CONFIG = " + json.dumps(config, ensure_ascii=False) + ";\n" + (root / "core.js").read_text() + (root / "runner.js").read_text() + "\n}\n"
