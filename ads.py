"""Budget/biaya iklan per toko, per bulan, per channel — input manual (data/<toko>/ads.json)."""
import os
import json

DEFAULT_CHANNELS = ["Iklan Shopee CPAS", "Iklan Meta Ads", "Iklan Shopee Ads", "Iklan Tiktok Ads"]
# atribusi default (aturan kantor: Meta Reg + CPAS + Shopee Ads dorong penjualan Shopee; TikTok Ads → TikTok/Tokopedia)
DEFAULT_ATTR = {"Iklan Shopee CPAS": "shopee", "Iklan Meta Ads": "shopee", "Iklan Shopee Ads": "shopee", "Iklan Tiktok Ads": "tiktok"}


def load(toko):
    """Iklan per toko dari database. `toko` = nama toko."""
    import db
    return db.ads_load(toko, {"channels": list(DEFAULT_CHANNELS), "attribution": dict(DEFAULT_ATTR), "months": {}, "notes": "", "prorate": False})


def save(toko, data):
    clean = {
        "channels": [str(c).strip() for c in data.get("channels", DEFAULT_CHANNELS) if str(c).strip()],
        "attribution": {k: (v if v in ("shopee", "tiktok", "all") else "all") for k, v in (data.get("attribution") or {}).items()},
        "months": {},
        "notes": data.get("notes", ""),
        "prorate": bool(data.get("prorate", False)),
    }
    for m, row in (data.get("months") or {}).items():
        if not m:
            continue
        clean["months"][m] = {}
        for c, v in (row or {}).items():
            try:
                clean["months"][m][c] = float(v) if v not in (None, "") else 0.0
            except (TypeError, ValueError):
                clean["months"][m][c] = 0.0
    import db
    db.ads_save(toko, clean)
    return clean


def spend_for(ads, month, platform="all"):
    """Total iklan bulan tsb yang diatribusikan ke filter platform. → (total, {channel: nilai})."""
    row = (ads.get("months") or {}).get(month) or {}
    attr = ads.get("attribution") or {}
    want = {"all": None, "shopee": {"shopee"}, "tiktok": {"tiktok"}, "tokopedia": {"tiktok"}, "tiktok_all": {"tiktok"}}.get(platform, None)
    detail = {}
    for c in ads.get("channels", []):
        v = float(row.get(c, 0) or 0)
        a = attr.get(c, "all")
        if want is None or a in want or a == "all":
            detail[c] = v
    return sum(detail.values()), detail
