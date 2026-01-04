import json
import html
from pathlib import Path
from datetime import datetime


ROOT = Path(__file__).parent
JSON_PATH = ROOT / "deals_today.json"
DOCS_DIR = ROOT / "docs"
OUTPUT_PATH = DOCS_DIR / "index.html"

def load_deals():
    if not JSON_PATH.exists():
        raise SystemExit(f"JSON deals file not found: {JSON_PATH}")
    with JSON_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_verified_at(deal):
    ts = deal.get("verified_at") or ""
    try:
        # Most likely ISO 8601
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def get_last_updated_text(deals):
    # Use the newest verified_at if available; otherwise now()
    times = [t for t in (parse_verified_at(d) for d in deals) if t is not None]
    if times:
        latest = max(times)
    else:
        latest = datetime.now()

    # Example: December 07, 2025 at 11:20 AM
    return latest.strftime("%B %d, %Y at %I:%M %p")


def retailer_classes(retailer: str):
    """
    Returns (pill_class, button_class) based on retailer.
    """
    r = (retailer or "").lower()
    if "walmart" in r:
        return "retailer-pill retailer-walmart", "view-button view-walmart"
    if "kroger" in r:
        return "retailer-pill retailer-kroger", "view-button view-kroger"
    if "target" in r:
        return "retailer-pill retailer-target", "view-button view-target"
    return "retailer-pill retailer-generic", "view-button view-generic"

def normalize_retailer_value(raw: str) -> str:
    r = (raw or "").strip().lower()
    if "walmart" in r:
        return "walmart"
    if "kroger" in r:
        return "kroger"
    if "target" in r:
        return "target"
    return r  # fallback

def normalise_availability(raw):
    if not isinstance(raw, str):
        return "Check store availability", "availability check"
    val = raw.strip().lower()
    if val == "in_store":
        return "In-store", "availability in-store"
    if val == "both":
        return "In-store & online", "availability both"
    if val == "online_only":
        return "Online only", "availability online-only"
    # Unknown / blank / other → generic helpful message
    return "Check store availability", "availability check"


def _norm_str(val) -> str:
    """Normalize string values for grouping."""
    return (val or "").strip()


def _norm_float_key(val):
    """Normalize numeric values for grouping keys (so 1.9 and 1.90 group together)."""
    try:
        if val is None:
            return None
        return round(float(val), 4)
    except Exception:
        return None


def extract_flavor_from_product_name(product_name: str) -> str:
    """
    Extract flavor from product name.
    Example: "Quest Protein Chips Chili Lime (8ct)" -> "Chili Lime"
    Example: "ALOHA Protein Bar Peanut Butter Chocolate (1ct)" -> "Peanut Butter Chocolate"
    
    Strategy: Remove pack size, then try to identify common product type words
    and extract everything after them as the flavor.
    """
    if not product_name:
        return ""
    
    import re
    # Remove pack size in parentheses at the end
    name_part = re.sub(r'\s*\([^)]+\)\s*$', '', product_name).strip()
    
    # Common product type keywords that typically come before flavor
    product_types = [
        "protein bar", "protein chips", "energy drink", "bar", "chips",
        "cookies", "crackers", "drink", "beverage", "snack"
    ]
    
    # Try to find product type and extract flavor after it
    name_lower = name_part.lower()
    for ptype in sorted(product_types, key=len, reverse=True):  # Try longer matches first
        if ptype in name_lower:
            idx = name_lower.find(ptype)
            # Get everything after the product type
            flavor_part = name_part[idx + len(ptype):].strip()
            if flavor_part:
                return flavor_part
    
    # If no product type found, assume the last 2-3 words are the flavor
    # (after removing brand which is typically first word)
    words = name_part.split()
    if len(words) >= 3:
        # Skip first word (brand) and take the rest
        return " ".join(words[1:])
    
    return name_part


def extract_brand_from_product_name(product_name: str) -> str:
    """
    Extract brand from product name (usually first word or two).
    Example: "Quest Protein Chips Chili Lime (8ct)" -> "Quest"
    Example: "ALOHA Protein Bar Peanut Butter Chocolate (1ct)" -> "ALOHA"
    """
    if not product_name:
        return ""
    parts = product_name.split()
    if len(parts) >= 1:
        return parts[0]
    return ""


def extract_base_product_name(product_name: str) -> str:
    """
    Extract base product name without flavor and pack size.
    Example: "Quest Protein Chips Chili Lime (8ct)" -> "Quest Protein Chips"
    Example: "ALOHA Protein Bar Peanut Butter Chocolate (1ct)" -> "ALOHA Protein Bar"
    """
    if not product_name:
        return ""
    
    import re
    # Remove pack size in parentheses
    name_part = re.sub(r'\s*\([^)]+\)\s*$', '', product_name).strip()
    
    # Common product type keywords
    product_types = [
        "protein bar", "protein chips", "energy drink", "bar", "chips",
        "cookies", "crackers", "drink", "beverage", "snack"
    ]
    
    # Try to find product type and extract base name up to that point
    name_lower = name_part.lower()
    for ptype in sorted(product_types, key=len, reverse=True):
        if ptype in name_lower:
            idx = name_lower.find(ptype)
            # Get everything up to and including the product type
            base_part = name_part[:idx + len(ptype)].strip()
            if base_part:
                return base_part
    
    # Fallback: return first 2-3 words (brand + product type)
    words = name_part.split()
    if len(words) >= 2:
        return " ".join(words[:2])
    return name_part


def remove_pack_size_from_name(product_name: str) -> str:
    """
    Remove pack size indicators like (1ct), (8ct), etc. from product name.
    Example: "Quest Protein Chips (8ct)" -> "Quest Protein Chips"
    Example: "ALOHA Protein Bar (1ct)" -> "ALOHA Protein Bar"
    """
    if not product_name:
        return ""
    
    import re
    # Remove pack size in parentheses at the end (e.g., (1ct), (8ct), (12 pack), etc.)
    cleaned = re.sub(r'\s*\([^)]*(?:ct|pack|count|pk)[^)]*\)\s*$', '', product_name, flags=re.IGNORECASE).strip()
    return cleaned


def group_deals(rows: list[dict]) -> list[dict]:
    """
    Group individual SKU rows into "deal families" so that each card represents:
      - same retailer
      - same section (Food / Drinks)
      - same brand (extracted from product_name)
      - same base product_name (without flavor)
      - same pack_size (pack_count + pack_unit)
      - same price / baseline (old_price)

    Flavors that share this key are stacked into one group. Each flavor stores
    its name and retailer_url for clickable links.

    Returns grouped deals with flavor_data containing {name, url} pairs.
    """
    groups: dict[tuple, dict] = {}

    for row in rows:
        retailer = _norm_str(row.get("retailer"))
        section = _norm_str(row.get("section"))
        product_name = _norm_str(row.get("product_name"))
        # Use brand from JSON if available, otherwise extract from product_name
        brand = _norm_str(row.get("brand")) or extract_brand_from_product_name(product_name)
        pack_count = row.get("pack_count")
        pack_unit = _norm_str(row.get("pack_unit"))
        pack_size = f"{pack_count}{pack_unit}" if pack_count and pack_unit else ""
        
        # Use old_price as baseline, new_price as price
        price_raw = row.get("new_price")
        baseline_raw = row.get("old_price")
        
        price_key = _norm_float_key(price_raw)
        baseline_key = _norm_float_key(baseline_raw)
        
        # Use product_name directly (it's now the base name without flavor from CSV)
        base_product = product_name
        
        key = (
            retailer.lower(),
            section.lower(),
            brand.lower(),
            base_product.lower(),
            pack_size.lower(),
            price_key,
            baseline_key,
        )

        if key not in groups:
            # Start a new group using a shallow copy of the row
            g = dict(row)
            g["flavor_data"] = []  # List of {name, url} dicts
            g["group_size"] = 0
            groups[key] = g

        g = groups[key]

        # Get flavor from JSON if available, otherwise try to extract from product_name
        flavor_name = _norm_str(row.get("flavor")) or extract_flavor_from_product_name(product_name)
        retailer_url = _norm_str(row.get("retailer_url") or row.get("canonical_url") or "")
        
        # Check if this flavor already exists in the group (shouldn't happen, but handle it)
        existing_flavor = next((f for f in g["flavor_data"] if f["name"].lower() == flavor_name.lower()), None)
        if not existing_flavor and flavor_name and retailer_url:
            g["flavor_data"].append({
                "name": flavor_name,
                "url": retailer_url
            })
            g["group_size"] = len(g["flavor_data"])

    # Finalize flavor_sample / flavor_extra_count for display
    for g in groups.values():
        flavors = g.get("flavor_data") or []
        if flavors:
            # Sort flavors for consistent display
            flavors.sort(key=lambda x: x["name"].lower())
            sample = flavors[:2]
            extra = max(0, len(flavors) - len(sample))
        else:
            sample = []
            extra = 0
        g["flavor_sample"] = sample
        g["flavor_extra_count"] = extra
        g["flavor_extra_data"] = flavors[2:] if len(flavors) > 2 else []

    return list(groups.values())


def get_badge(deal):
    """
    Decide badge label & class from percent_off.
    Tier system:
    - ≥25% → 💎 Diamond Deal (badge-elite)
    - 20–24.99% → 🔥 Fire Deal (badge-strong)
    - 10–19.99% → 💪 Strong Deal (badge-protein)
    - 0–9.99% → 🏷️ On Sale (badge-everyday)
    """
    try:
        percent_off = float(deal.get("percent_off", 0.0))
    except Exception:
        percent_off = 0.0

    # Check for Diamond Deal tier (≥25%)
    if percent_off >= 25.0:
        return "💎 Diamond Deal", "badge badge-elite"
    # Check for Fire Deal tier (20–24.99%)
    if percent_off >= 20.0:
        return "🔥 Fire Deal", "badge badge-strong"
    # Check for Strong Deal tier (10–19.99%)
    if percent_off >= 10.0:
        return "💪 Strong Deal", "badge badge-protein"
    # Check for On Sale tier (0–9.99%)
    if percent_off >= 0.0:
        return "🏷️ On Sale", "badge badge-everyday"
    # No badge for negative percentages
    return "", ""


def format_streak(deal):
    streak = deal.get("streak_days")
    try:
        if streak is None:
            return ""
        streak_int = int(streak)
        if streak_int < 1:
            return ""
    except Exception:
        return ""
    day_word = "day" if streak_int == 1 else "days"
    return f"Day {streak_int} of this deal"


def build_card_html(deal):
    # Use product_name directly from the deal (from CSV, not combined)
    # Remove pack size indicators since we have a pack pill overlay
    product_name = deal.get("product_name", "")
    product_name_cleaned = remove_pack_size_from_name(product_name)
    name = html.escape(product_name_cleaned)
    retailer = html.escape(deal.get("retailer", ""))
    category = html.escape(deal.get("category", ""))
    old_price = deal.get("old_price")
    new_price = deal.get("new_price")
    percent_off = float(deal.get("percent_off", 0.0))
    image_url = html.escape(deal.get("image_url", ""))
    retailer_url = html.escape(deal.get("retailer_url", "#"))
    
    # Get flavor data if this is a grouped deal
    flavor_sample = deal.get("flavor_sample", [])
    flavor_extra_count = deal.get("flavor_extra_count", 0)
    flavor_extra_data = deal.get("flavor_extra_data", [])

    # Normalized values for filters (used in data-* attributes)
    section_raw = (deal.get("section") or "").strip().lower()
    category_raw = (deal.get("category") or "").strip().lower()
    retailer_raw = normalize_retailer_value(deal.get("retailer") or "")

    section_attr = html.escape(section_raw)
    category_attr = html.escape(category_raw)
    retailer_attr = html.escape(retailer_raw)

    # Retailer specific styling
    pill_class, button_class = retailer_classes(deal.get("retailer", ""))

    # Availability
    availability_text, availability_class = normalise_availability(
        deal.get("availability", "")
    )

    # Badge
    badge_label, badge_class = get_badge(deal)

    # Streak
    streak_text = format_streak(deal)

    # Pack size pill
    pack_count = deal.get("pack_count")
    pack_unit = deal.get("pack_unit")
    pack_pill_html = ""
    try:
        if pack_count is not None and pack_unit:
            pack_pill_html = (
                f"<div class='pack-pill'>"
                f"<span class='pack-count'>{int(pack_count)}</span> "
                f"<span class='pack-unit'>{html.escape(pack_unit)}</span>"
                f"</div>"
            )
    except Exception:
        pack_pill_html = ""

    # Make sure pricing can be cast safely
    try:
        new_price_val = float(new_price) if new_price is not None else 0.0
    except Exception:
        new_price_val = 0.0

    old_price_html = ""
    if old_price is not None:
        try:
            old_price_val = float(old_price)
            if old_price_val > new_price_val:
                old_price_html = (
                    f"<span class='old-price'>${old_price_val:.2f}</span>"
                )
        except Exception:
            old_price_html = ""

    availability_html = ""
    if availability_text:
        availability_html = f"<div class='{availability_class}'>{availability_text}</div>"

    streak_html = ""
    if streak_text:
        streak_html = f"<div class='streak'>{streak_text}</div>"

    badge_html = ""
    if badge_label and badge_class:
        badge_html = f"<div class='{badge_class}'>{badge_label}</div>"

    # Build flavor display HTML
    flavor_html = ""
    if flavor_sample or flavor_extra_count > 0:
        # Build flavor sample display
        flavor_names = [html.escape(f.get("name", "")) for f in flavor_sample if f.get("name")]
        flavor_display = ", ".join(flavor_names)
        
        # Generate unique ID for this card's flavor expand section
        import hashlib
        card_id = hashlib.md5(f"{retailer}{name}{new_price_val}".encode()).hexdigest()[:8]
        
        if flavor_extra_count > 0:
            # Has extra flavors - make expandable
            flavor_html = f"""
            <div class="flavor-info">
                <span class="flavor-label">Available in: </span>
                <span class="flavor-sample">{flavor_display}</span>
                <button class="flavor-expand-link" data-card-id="{card_id}" aria-expanded="false" aria-controls="flavors-{card_id}">
                    + {flavor_extra_count} more flavor{'s' if flavor_extra_count > 1 else ''}
                </button>
                <div class="flavor-list-expanded" id="flavors-{card_id}" style="display: none;">
                    {''.join([f'<a href="{html.escape(f.get("url", "#"))}" target="_blank" rel="noopener noreferrer" class="flavor-link">{html.escape(f.get("name", ""))}</a>' for f in flavor_extra_data])}
                </div>
            </div>
            """
        else:
            # Just the sample flavors, no expansion needed
            flavor_html = f"""
            <div class="flavor-info">
                <span class="flavor-label">Available in: </span>
                <span class="flavor-sample">{flavor_display}</span>
            </div>
            """

    return f"""
    <div class="card"
         data-section="{section_attr}"
         data-category="{category_attr}"
         data-retailer="{retailer_attr}"
         data-price="{new_price_val}"
         data-percent="{float(percent_off) if percent_off is not None else 0.0}">
        <div class="card-image-wrap">
            <img src="{image_url}" alt="{name}" class="card-image"/>
            {pack_pill_html}
            {badge_html}
        </div>
        <div class="card-content">
            <div class="card-meta-row">
                <div class="{pill_class}">{retailer}</div>
                <div class="meta-category">{category}</div>
            </div>
            <div class="card-pricing">
                {old_price_html}
                <span class="new-price">${new_price_val:.2f}</span>
                <span class="percent-off">{percent_off:.0f}% OFF</span>
            </div>
            <div class="card-title">{name}</div>
            {flavor_html}
            <div class="card-footer">
                {availability_html}
                {streak_html}
            </div>
            <a class="{button_class}" href="{retailer_url}" target="_blank" rel="noopener noreferrer">
                View on {retailer}.com
            </a>
        </div>
    </div>
    """


def build_page_html(deals):
    # Group deals into deal families
    grouped_deals = group_deals(deals)
    total = len(grouped_deals)
    last_updated = get_last_updated_text(deals)

    # Sort: retailer → category → best savings (default order on page load)
    def sort_key(d):
        return (
            (d.get("retailer") or "").lower(),
            (d.get("category") or "").lower(),
            -float(d.get("percent_off") or 0.0),
            float(d.get("new_price") or 0.0),
        )

    deals_sorted = sorted(grouped_deals, key=sort_key)
    cards_html = "\n".join(build_card_html(d) for d in deals_sorted)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <title>SnackBuddy Daily Deals</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
        :root {{
            --bg: #f3f4f6;
            --card-bg: #ffffff;
            --border-subtle: #e5e7eb;
            --text-main: #111827;
            --text-muted: #6b7280;
            --blue: #2563eb;
            --green: #16a34a;
            --red: #dc2626;
            --orange: #f97316;
            --yellow: #facc15;
            --shadow-soft: 0 8px 20px rgba(15, 23, 42, 0.08);
            --radius-lg: 16px;
            --radius-pill: 999px;
            --navy: #0f172a;
        }}

        * {{
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI",
                sans-serif;
            background-color: var(--bg);
            color: var(--text-main);
        }}

        .page {{
            max-width: 1100px;
            margin: 0 auto 40px auto;
            padding: 0 16px 40px 16px;
        }}

        /* ============================ */
        /* SECTION 1A: SITE HEADER NAV  */
        /* ============================ */

        .sb-header {{
            position: sticky;
            top: 0;
            z-index: 40;
            background-color: #ffffff;
            border-bottom: 1px solid var(--border-subtle);
        }}

        .sb-header-inner {{
            max-width: 1100px;
            margin: 0 auto;
            padding: 10px 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}

        .sb-header-left {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        /* Logo image (replaces placeholder box) */
        .sb-logo {{
            width: 32px;
            height: 32px;
            border-radius: 8px;
            object-fit: contain;
            display: block;
        }}

        .sb-header-title {{
            font-weight: 600;
            font-size: 18px;
            color: var(--text-main);
        }}

        .sb-menu-button {{
            border: none;
            background: transparent;
            font-size: 20px;
            cursor: pointer;
        }}

        /* Slide-out nav drawer */

        .sb-nav-backdrop {{
            position: fixed;
            inset: 0;
            background: rgba(15, 23, 42, 0.35);
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.18s ease-out;
            z-index: 39;
        }}

        .sb-nav-backdrop.open {{
            opacity: 1;
            pointer-events: auto;
        }}

        .sb-nav-drawer {{
            position: fixed;
            top: 0;
            right: 0;
            height: 100vh;
            width: 260px;
            background: #ffffff;
            box-shadow: -8px 0 20px rgba(15,23,42,0.25);
            transform: translateX(100%);
            transition: transform 0.2s ease-out;
            z-index: 40;
            display: flex;
            flex-direction: column;
        }}

        .sb-nav-drawer.open {{
            transform: translateX(0%);
        }}

        .sb-nav-inner {{
            padding: 16px 18px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            height: 100%;
        }}

        .sb-nav-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 8px;
        }}

        .sb-nav-header-left {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .sb-nav-title {{
            font-weight: 600;
            font-size: 18px;
        }}

        .sb-nav-close {{
            border: none;
            background: transparent;
            font-size: 20px;
            cursor: pointer;
        }}

        .sb-nav-link {{
            display: block;
            padding: 8px 0;
            font-size: 14px;
            color: var(--text-main);
            text-decoration: none;
        }}

        .sb-nav-link:hover {{
            color: var(--blue);
        }}

        .sb-nav-footer {{
            margin-top: auto;
            font-size: 12px;
            color: var(--text-muted);
            line-height: 1.35;
        }}

/* ============================ */
/* FILTER DRAWER (LEFT)         */
/* ============================ */

.sb-filter-open{{
  border: 1px solid var(--border-subtle);
  background: #ffffff;
  border-radius: 12px;
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 800;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}}

.sb-filter-icon{{
  font-size: 16px;
  line-height: 1;
}}

.sb-filter-open:hover{{
  filter: brightness(0.98);
}}

.sb-filter-open:active{{
  transform: translateY(1px);
}}

.sb-filter-open:hover{{ filter: brightness(0.98); }}

.sb-filter-count{{
  font-weight: 900;
  color: var(--text-muted);
}}

.sb-filter-backdrop{{
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.35);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.18s ease-out;
  z-index: 39;
}}

.sb-filter-backdrop.open{{
  opacity: 1;
  pointer-events: auto;
}}

.sb-filter-drawer{{
  position: fixed;
  top: 0;
  left: 0;
  height: 100vh;
  width: min(340px, 92vw);
  background: #ffffff;
  box-shadow: 8px 0 20px rgba(15,23,42,0.25);
  transform: translateX(-105%);
  transition: transform 0.2s ease-out;
  z-index: 40;
  display: flex;
  flex-direction: column;
}}

.sb-filter-drawer.open{{
  transform: translateX(0%);
}}

.sb-filter-inner{{
  padding: 16px 16px;
  display: flex;
  flex-direction: column;
  height: 100%;
  gap: 12px;
}}

.sb-filter-header{{
  display: flex;
  align-items: center;
  justify-content: space-between;
}}

.sb-filter-title{{
  font-weight: 900;
  font-size: 18px;
}}

.sb-filter-close{{
  border: none;
  background: transparent;
  font-size: 22px;
  cursor: pointer;
}}

.sb-filter-groups{{
  overflow: auto;
  padding-right: 6px;
}}

.sb-filter-group{{
  border: 1px solid var(--border-subtle);
  border-radius: 14px;
  padding: 10px 10px;
  margin-bottom: 10px;
}}

.sb-filter-group-header{{
  display: flex;
  align-items: center;
  justify-content: space-between;
  cursor: pointer;
  user-select: none;
}}

.sb-filter-group-title{{
  font-weight: 900;
  font-size: 13px;
}}

.sb-filter-group-meta{{
  font-size: 12px;
  color: var(--text-muted);
  font-weight: 800;
}}

.sb-filter-options{{
  margin-top: 10px;
  display: none;
}}

.sb-filter-group.open .sb-filter-options{{
  display: grid;
  gap: 8px;
}}

.sb-check{{
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}}

.sb-check input{{
  width: 16px;
  height: 16px;
}}

.sb-filter-footer{{
  margin-top: auto;
  display: flex;
  gap: 10px;
}}

.sb-filter-clear{{
  flex: 1;
  border: 1px solid var(--border-subtle);
  background: #ffffff;
  border-radius: 12px;
  padding: 10px 12px;
  font-weight: 900;
  cursor: pointer;
}}

.sb-filter-apply{{
  flex: 1;
  border: none;
  background: var(--navy);
  color: #ffffff;
  border-radius: 12px;
  padding: 10px 12px;
  font-weight: 900;
  cursor: pointer;
}}

.sb-filter-actions{{
  margin-top: auto;
  display: flex;
  gap: 10px;
}}

.sb-filter-action{{
  flex: 1;
  border-radius: 12px;
  padding: 10px 12px;
  font-weight: 800;
  cursor: pointer;
  border: 1px solid var(--border-subtle);
}}

.sb-filter-action.primary{{
  background: var(--navy);
  color: #fff;
  border-color: var(--navy);
}}

.sb-filter-action.secondary{{
  background: #fff;
  color: var(--text-main);
}}

        /* ============================ */
        /* SECTION 1B: HERO BANNER      */
        /* ============================ */

        .sb-hero {{
            margin-top: 8px;
        }}

        .sb-hero-bg {{
            max-width: 1100px;
            margin: 0 auto;
            border-radius: 18px;
            padding: 16px 18px;
            position: relative;
            overflow: hidden;
            background: linear-gradient(135deg, #0ea5e9, #10b981); /* blue → green */
            color: #ffffff;
            box-shadow: 0 10px 30px rgba(15, 23, 42, 0.25);
        }}

        /* subtle pattern using CSS only */
        .sb-hero-pattern {{
            position: absolute;
            inset: 0;
            opacity: 0.12;
            background-image:
                radial-gradient(circle at 0 0, rgba(255,255,255,0.4) 1px, transparent 1px),
                radial-gradient(circle at 20px 20px, rgba(255,255,255,0.2) 1px, transparent 1px);
            background-size: 40px 40px;
            pointer-events: none;
        }}

        .sb-hero-content {{
            position: relative;
            z-index: 1;
        }}

        .sb-hero-kicker {{
            font-size: 12px;
            opacity: 0.92;
            margin-bottom: 4px;
        }}

        .sb-hero-title {{
            font-weight: 700;
            font-size: 20px;
            margin: 4px 0;
        }}

        @media (min-width: 768px) {{
            .sb-hero-title {{
                font-size: 24px;
            }}
        }}

        .sb-hero-subtitle {{
            font-size: 14px;
            margin: 4px 0 10px;
            opacity: 0.95;
        }}

        .sb-hero-cta {{
            display: inline-block;
            padding: 8px 16px;
            border-radius: var(--radius-pill);
            background-color: var(--navy); /* dark navy CTA */
            color: #ffffff;
            text-decoration: none;
            font-size: 13px;
            font-weight: 600;
        }}

        .sb-hero-cta:hover {{
            filter: brightness(1.02);
        }}

        /* =================================== */
        /* SECTION 1C: FILTERS + COUNT ROW      */
        /* =================================== */

        .sb-utility-row {{
            max-width: 1100px;
            margin: 12px auto 12px;
            padding: 0 16px;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}

        @media (min-width: 640px) {{
            .sb-utility-row {{
                flex-direction: row;
                align-items: center;
                justify-content: space-between;
            }}
        }}

        .pill-label,
        .count-pill {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 8px 12px;
            border-radius: var(--radius-pill);
            font-size: 13px;
        }}

        .pill-label {{
            border: 1px solid var(--border-subtle);
            background-color: #f9fafb;
            color: var(--text-main);
            box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
        }}

        .count-pill {{
            background-color: var(--navy);
            color: #ffffff;
            font-weight: 600;
        }}

        .filter-controls {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-items: center;
        }}

        .sb-filter {{
            padding: 6px 10px;
            border-radius: 12px;
            border: 1px solid var(--border-subtle);
            background-color: #ffffff;
            font-size: 13px;
            min-width: 140px;
        }}

        /* ============================ */
        /* ACTIVE FILTERS INDICATOR     */
        /* ============================ */

        .sb-active-filters {{
            max-width: 1100px;
            margin: 0 auto 12px;
            padding: 0 16px;
            display: none; /* JS will show when filters are active */
            align-items: center;
            justify-content: space-between;
            gap: 12px;
        }}

        .sb-active-left {{
            display: flex;
            align-items: center;
            gap: 10px;
            min-width: 0;
        }}

        .sb-active-label {{
            font-size: 12px;
            font-weight: 700;
            color: var(--text-muted);
        }}

        .sb-chip-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }}

        .sb-chip {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 6px 10px;
            border-radius: var(--radius-pill);
            background: #ffffff;
            border: 1px solid var(--border-subtle);
            font-size: 12px;
            font-weight: 700;
            color: var(--text-main);
            box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
        }}

        .sb-chip small {{
            font-size: 11px;
            font-weight: 800;
            color: var(--text-muted);
        }}

        .sb-chip button {{
            border: none;
            background: transparent;
            cursor: pointer;
            font-size: 14px;
            line-height: 1;
            padding: 0;
            color: var(--text-muted);
        }}

        .sb-chip button:hover {{
            color: var(--text-main);
        }}

        .sb-clear-all {{
            border: 1px solid var(--border-subtle);
            background: #ffffff;
            border-radius: 12px;
            padding: 8px 12px;
            font-size: 12px;
            font-weight: 800;
            cursor: pointer;
        }}

        .sb-clear-all:hover {{
            filter: brightness(0.98);
        }}

        @media (max-width: 640px) {{
            .sb-active-filters {{
                align-items: flex-start;
                flex-direction: column;
            }}
            .sb-clear-all {{
                width: 100%;
            }}
        }}

/* ============================ */
/* FILTER DRAWER (LEFT)         */
/* ============================ */

.sb-filter-open{{
  border: 1px solid var(--border-subtle);
  background: #ffffff;
  border-radius: 12px;
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 800;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}}

.sb-filter-mascot{{
  width: 18px;
  height: 18px;
  object-fit: contain;
  display: inline-block;
}}

.sb-filter-drawer{{
  position: fixed;
  top: 0;
  left: 0;
  width: 280px;
  height: 100vh;
  background: #ffffff;
  transform: translateX(-100%);
  transition: transform 0.22s ease-out;
  z-index: 40;
}}

.sb-filter-drawer.open{{
  transform: translateX(0);
}}

.sb-filter-inner{{
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}}

.sb-filter-header{{
  display: flex;
  justify-content: space-between;
  align-items: center;
}}

.sb-filter-title{{
  font-weight: 800;
  font-size: 16px;
}}

/* ============================ */
/* SEPARATE BACKDROP FOR FILTER */
/* ============================ */

.sb-filter-backdrop{{
  position: fixed;
  inset: 0;
  background: rgba(15,23,42,0.35);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.18s ease-out;
  z-index: 39;
}}

.sb-filter-backdrop.open{{
  opacity: 1;
  pointer-events: auto;
}}


        /* ============================ */
        /* SECTION 2: DEAL CARD GRID    */
        /* ============================ */

        .card-grid {{
            max-width: 1100px;
            margin: 0 auto;
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 14px;
        }}

        .card {{
            background-color: var(--card-bg);
            border-radius: var(--radius-lg);
            border: 1px solid var(--border-subtle);
            box-shadow: var(--shadow-soft);
            padding: 10px 10px 12px 10px;
            display: flex;
            flex-direction: column;
        }}

        .card-image-wrap {{
            position: relative;
            padding: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 120px;
        }}

        .card-image {{
            max-width: 100%;
            max-height: 110px;
            object-fit: contain;
        }}

        .pack-pill {{
            position: absolute;
            bottom: 6px;
            right: 8px;
            background-color: #111827;
            color: #ffffff;
            border-radius: 999px;
            padding: 2px 8px;
            font-size: 11px;
            display: inline-flex;
            align-items: center;
            gap: 2px;
        }}

        .pack-count {{
            font-weight: 700;
            text-transform: uppercase;
            font-size: 11px;
        }}

        .pack-unit {{
            text-transform: uppercase;
            font-size: 10px;
        }}

        .badge {{
            position: absolute;
            top: 6px;
            left: 8px;
            border-radius: var(--radius-pill);
            padding: 2px 8px;
            font-size: 11px;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }}

        .badge-strong {{
            background-color: #fff7ed;
            color: var(--orange);
            border: 1px solid #fed7aa;
        }}

        .badge-regular {{
            background-color: #fefce8;
            color: #854d0e;
            border: 1px solid #facc15;
        }}

        .badge-elite {{
            background-color: #eff6ff;
            color: #1e40af;
            border: 1px solid #93c5fd;
        }}

        .badge-protein {{
            background-color: #fefce8;
            color: #854d0e;
            border: 1px solid #facc15;
        }}

        .badge-everyday {{
            background-color: #f5e6d3;
            color: #7c3a00;
            border: 1px solid #d4a574;
        }}

        .card-content {{
            padding: 4px 6px 0 6px;
            display: flex;
            flex-direction: column;
            gap: 6px;
        }}

        .card-meta-row {{
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 11px;
        }}

        .retailer-pill {{
            padding: 3px 8px;
            border-radius: var(--radius-pill);
            font-size: 11px;
            font-weight: 600;
        }}

        .retailer-walmart {{
            background-color: #0071CE;
            color: #F9C80E;
        }}

        .retailer-kroger {{
            background-color: #003087;
            color: #ffffff;
        }}

        .retailer-target {{
            background-color: #CC0000;
            color: #ffffff;
        }}

        .retailer-generic {{
            background-color: #6b7280;
            color: #ffffff;
        }}

        .meta-category {{
            color: var(--text-muted);
        }}

        .card-title {{
            font-size: 14px;
            font-weight: 600;
            line-height: 1.25;
            min-height: 34px;
            margin-bottom: 0;
        }}

        .card-pricing {{
            display: flex;
            align-items: baseline;
            gap: 6px;
            font-size: 13px;
            margin-top: 0;
            margin-bottom: 0;
        }}

        .old-price {{
            color: #ef4444;
            text-decoration: line-through;
        }}

        .new-price {{
            color: var(--text-main);
            font-weight: 700;
        }}

        .percent-off {{
            color: var(--green);
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.03em;
        }}

        .card-footer {{
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 4px;
            margin-top: 2px;
            font-size: 11px;
        }}

        .availability.check {{
            font-style: italic;
            color: var(--text-muted);
        }}

        .streak {{
            color: var(--text-muted);
            font-style: italic;
        }}

        .view-button {{
            margin-top: 8px;
            display: block;
            text-align: center;
            padding: 7px 10px;
            border-radius: 12px;
            font-size: 13px;
            font-weight: 600;
            text-decoration: none;
        }}

        .view-walmart {{
            background-color: #0071CE;
            color: #F9C80E;
        }}

        .view-kroger {{
            background-color: #003087;
            color: #ffffff;
        }}

        .view-target {{
            background-color: #CC0000;
            color: #ffffff;
        }}

        .view-generic {{
            background-color: #4b5563;
            color: #ffffff;
        }}

        .view-button:hover {{
            filter: brightness(0.95);
        }}

        /* ============================ */
        /* FLAVOR EXPANDABLE LIST       */
        /* ============================ */

        .flavor-info {{
            font-size: 12px;
            color: var(--text-main);
            margin-top: 0;
            margin-bottom: 0;
            line-height: 1.4;
        }}

        .flavor-label {{
            color: var(--text-muted);
        }}

        .flavor-sample {{
            color: var(--text-main);
        }}

        .flavor-expand-link {{
            background: none;
            border: none;
            color: var(--blue);
            cursor: pointer;
            font-size: 12px;
            padding: 0;
            margin-left: 4px;
            text-decoration: underline;
            font-weight: 600;
        }}

        .flavor-expand-link:hover {{
            color: #1e40af;
        }}

        .flavor-list-expanded {{
            margin-top: 6px;
            display: flex;
            flex-direction: column;
            gap: 4px;
        }}

        .flavor-link {{
            color: var(--blue);
            text-decoration: none;
            font-size: 12px;
            padding: 2px 0;
        }}

        .flavor-link:hover {{
            color: #1e40af;
            text-decoration: underline;
        }}

        /* ============================ */
        /* SECTION 3: INFO SECTIONS     */
        /* ============================ */

        .sb-info {{
            max-width: 1100px;
            margin: 18px auto 0;
        }}

        .sb-info-card {{
            background: #ffffff;
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-lg);
            box-shadow: var(--shadow-soft);
            padding: 14px 14px;
            margin-top: 14px;
        }}

        .sb-info-title {{
            font-size: 16px;
            font-weight: 800;
            margin: 0 0 6px 0;
        }}

        .sb-info-text {{
            margin: 0;
            color: var(--text-main);
            line-height: 1.55;
            font-size: 14px;
        }}

        .sb-subscribe-row {{
            margin-top: 10px;
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: center;
        }}

        .sb-email {{
            flex: 1;
            min-width: 220px;
            padding: 10px 12px;
            border-radius: 12px;
            border: 1px solid var(--border-subtle);
            font-size: 14px;
        }}

        .sb-subscribe-btn {{
            padding: 10px 14px;
            border-radius: 12px;
            border: none;
            background: var(--navy);
            color: #ffffff;
            font-weight: 800;
            cursor: pointer;
            font-size: 14px;
        }}

        .sb-subscribe-btn:hover {{
            filter: brightness(1.03);
        }}

.sb-subscribe-btn{{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  text-decoration: none;
}}

.sb-subscribe-alt{{
  font-size: 13px;
  color: var(--text-muted);
  text-decoration: underline;
  font-weight: 600;
}}

        .sb-note {{
            margin-top: 10px;
            font-size: 13px;
            color: var(--text-muted);
            line-height: 1.45;
        }}

        @media (max-width: 640px) {{
            .page {{
                padding-top: 8px;
            }}
        }}
    </style>
</head>
<!-- NAV backdrop (right drawer) -->
<div class="sb-nav-backdrop" id="sb-nav-backdrop"></div>

<!-- FILTER backdrop (left drawer) -->
<div class="sb-filter-backdrop" id="sb-filter-backdrop"></div>

<!-- NAV DRAWER (RIGHT) -->
<nav class="sb-nav-drawer" id="sb-nav-drawer">
  <div class="sb-nav-inner">
    <div class="sb-nav-header">
      <div class="sb-nav-header-left">
        <img src="assets/logo-transparent.png" alt="SnackBuddy logo" class="sb-logo" />
        <span class="sb-nav-title">SnackBuddy</span>
      </div>
      <button class="sb-nav-close" aria-label="Close menu">&times;</button>
    </div>

    <a href="#deals-list" class="sb-nav-link">All Deals</a>
    <a href="#about" class="sb-nav-link">About SnackBuddy</a>
    <a href="#how-it-works" class="sb-nav-link">How SnackBuddy Works</a>
    <a href="#subscribe" class="sb-nav-link">Get Daily Deal Emails</a>

    <div class="sb-nav-footer">
      SnackBuddy helps you spot better-for-you snack deals.
    </div>
  </div>
</nav>

<!-- FILTER backdrop (left drawer) -->
<div class="sb-filter-backdrop" id="sb-filter-backdrop"></div>

<!-- FILTER DRAWER (LEFT) -->
<aside class="sb-filter-drawer" id="sb-filter-drawer">
  <div class="sb-filter-inner">
    <div class="sb-filter-header">
      <span class="sb-filter-title">Filters</span>
      <button id="sb-filter-close" aria-label="Close filters">&times;</button>
    </div>

    <!-- JS will render the filter groups (Retailer / Section / Category) into here -->
    <div id="sb-filter-groups"></div>

    <div class="sb-filter-actions">
      <button id="sb-filter-clear" type="button" class="sb-filter-action secondary">Clear</button>
      <button id="sb-filter-apply" type="button" class="sb-filter-action primary">Apply</button>
    </div>
  </div>
</aside>

<!-- Filters Drawer Backdrop + Drawer -->
<div class="sb-filter-backdrop" id="sb-filter-backdrop"></div>

<aside class="sb-filter-drawer" id="sb-filter-drawer" aria-label="Filters">
  <div class="sb-filter-inner">
    <div class="sb-filter-header">
      <div class="sb-filter-title">Filters</div>
      <button class="sb-filter-close" id="sb-filter-close" aria-label="Close filters">&times;</button>
    </div>

    <div class="sb-filter-groups" id="sb-filter-groups">
      <!-- JS injects groups + checkboxes -->
    </div>

    <div class="sb-filter-footer">
      <button class="sb-filter-clear" id="sb-filter-clear" type="button">Clear</button>
      <button class="sb-filter-apply" id="sb-filter-apply" type="button">Apply</button>
    </div>
  </div>
</aside>


    <!-- ============================ -->
    <!-- SECTION 1A: NAVBAR           -->
    <!-- ============================ -->
    <header class="sb-header">
        <div class="sb-header-inner">
            <div class="sb-header-left">
                <img src="assets/logo-transparent.png" alt="SnackBuddy logo" class="sb-logo" />
                <span class="sb-header-title">SnackBuddy</span>
            </div>
            <button class="sb-menu-button" aria-label="Menu" aria-expanded="false">☰</button>
        </div>
    </header>

    <main class="page">
        <!-- ============================ -->
        <!-- SECTION 1B: HERO BANNER      -->
        <!-- ============================ -->
        <section class="sb-hero">
            <div class="sb-hero-bg">
                <div class="sb-hero-pattern"></div>
                <div class="sb-hero-content">
                    <div class="sb-hero-kicker">
                        SnackBuddy • Last updated: {last_updated} (local time)
                    </div>
                    <h1 class="sb-hero-title">Don&apos;t overpay for the snacks you already buy.</h1>
                    <p class="sb-hero-subtitle">
                        SnackBuddy tracks price drops on better-for-you snacks and drinks—so you can stock up when it&apos;s worth it.
                    </p>
                    <a href="#deals-list" class="sb-hero-cta">
                        See today&apos;s deals →
                    </a>
                </div>
            </div>
        </section>

<!-- =================================== -->
<!-- SECTION 1C: FILTERS + DEAL COUNT ROW -->
<!-- =================================== -->
<section class="sb-utility-row">
  <div class="filter-controls">
<button class="sb-filter-open" id="sb-filter-open" type="button">
  <img
    src="assets/icons/raccoon-detective.png"
    alt=""
    class="sb-filter-mascot"
  />
  Filters <span id="sb-filter-count">(0)</span>
</button>

    <!-- Sort stays -->
    <select id="sort-deals" class="sb-filter">
      <option value="best">Sort: Best Deals</option>
      <option value="price_asc">Sort: Price (low → high)</option>
      <option value="price_desc">Sort: Price (high → low)</option>
    </select>
  </div>

  <div id="deal-count" class="count-pill">
    👀 Showing {total} deals
  </div>
</section>

        <!-- ============================ -->
        <!-- ACTIVE FILTERS INDICATOR     -->
        <!-- ============================ -->
        <section class="sb-active-filters" id="active-filters" aria-live="polite">
            <div class="sb-active-left">
                <span class="sb-active-label">Active:</span>
                <div class="sb-chip-row" id="active-filter-chips"></div>
            </div>

            <button class="sb-clear-all" id="clear-all-filters" type="button">
                Clear all
            </button>
        </section>

        <!-- ============================ -->
        <!-- SECTION 2: DEAL CARD GRID    -->
        <!-- ============================ -->
        <section class="card-grid" id="deals-list">
            {cards_html}
        </section>

        <!-- ============================ -->
        <!-- SECTION 3A: ABOUT            -->
        <!-- ============================ -->
        <section class="sb-info" id="about">
            <div class="sb-info-card">
                <h2 class="sb-info-title">About SnackBuddy</h2>
                <p class="sb-info-text">
                    SnackBuddy started after I became a dad, money got tighter, and I tried to get healthier at the same time.
                </p>
                <p class="sb-info-text" style="margin-top: 8px;">
                    When you’re trying to eat better, snacks matter. They can either quietly wreck a calorie deficit or help you hit goals like
                    <strong>protein</strong> and <strong>fiber</strong> without much effort. I found myself buying the same products over and over,
                    and I started noticing something: one week they’d be on sale, the next week they wouldn’t. After a while, paying full price
                    started to feel like I was overpaying for things I bought all the time.
                </p>
                <p class="sb-info-text" style="margin-top: 8px;">
                    So I built SnackBuddy to do one simple thing:
                    <strong>track the snacks and drinks people already buy and call out real price drops</strong> —
                    so you can stock up at the right time or try something new when it’s cheaper.
                </p>
            </div>
        </section>

        <!-- ============================ -->
        <!-- SECTION 3B: HOW IT WORKS     -->
        <!-- ============================ -->
        <section class="sb-info" id="how-it-works">
            <div class="sb-info-card">
                <h2 class="sb-info-title">How SnackBuddy Works</h2>
                <p class="sb-info-text">
                    SnackBuddy tracks prices for a growing list of better-for-you snacks and drinks across major retailers.
                </p>
                <p class="sb-info-text" style="margin-top: 8px;">
                    At its core, it’s pretty simple: we keep an eye on specific product pages and pay attention when prices move.
                    When something drops, it shows up here so more people can take advantage of it — without having to constantly
                    check themselves.
                </p>
                <p class="sb-info-text" style="margin-top: 8px;">
                    Don’t see a product or retailer you want tracked yet? Let me know — the list is expanding over time.
                </p>
                <p class="sb-note">
                    Note: Prices and availability can vary by location, especially for in-store items.
                </p>
            </div>
        </section>

            <div class="sb-info-card" id="subscribe">
                <h2 class="sb-info-title">Get Daily Deal Emails</h2>
                <p class="sb-info-text">
                    Want a quick digest of the best finds? Drop your email below.
                </p>

                <div class="sb-subscribe-row">
    <a class="sb-subscribe-btn"
       href="https://forms.gle/YatCfNYrLdALVq1N8"
       target="_blank"
       rel="noopener noreferrer">
        Get Daily Deal Emails
    </a>

    <a class="sb-subscribe-alt"
       href="mailto:savewithsnackbuddy@gmail.com?subject=SnackBuddy%20Daily%20Deals">
        Or email us: savewithsnackbuddy@gmail.com
    </a>
</div>

<div class="sb-note">
    You’ll get a quick daily digest when the best deals are worth grabbing. No spam—ever.
</div>

            </div>
        </section>
    </main>

    <!-- Tiny JS for slide-out menu -->
<script>
document.addEventListener("DOMContentLoaded", function () {{
  const grid = document.getElementById("deals-list");
  const cards = Array.from(document.querySelectorAll(".card"));

  const sortSelect = document.getElementById("sort-deals");
  const countEl = document.getElementById("deal-count");

  const chipsWrap = document.getElementById("active-filter-chips");
  const clearAllBtn = document.getElementById("clear-all-filters");
  const activeSection = document.getElementById("active-filters");

  /* ============================
     NAV DRAWER (RIGHT)
     ============================ */
  const navBtn = document.querySelector(".sb-menu-button");
  const navDrawer = document.getElementById("sb-nav-drawer");
  const navBackdrop = document.getElementById("sb-nav-backdrop");
  const navClose = document.querySelector(".sb-nav-close");

  function openNav() {{
    if (!navDrawer || !navBackdrop || !navBtn) return;
    navDrawer.classList.add("open");
    navBackdrop.classList.add("open");
    navBtn.setAttribute("aria-expanded", "true");
  }}

  function closeNav() {{
    if (!navDrawer || !navBackdrop || !navBtn) return;
    navDrawer.classList.remove("open");
    navBackdrop.classList.remove("open");
    navBtn.setAttribute("aria-expanded", "false");
  }}

  navBtn?.addEventListener("click", function() {{
    if (navDrawer?.classList.contains("open")) closeNav();
    else openNav();
  }});
  navBackdrop?.addEventListener("click", closeNav);
  navClose?.addEventListener("click", closeNav);

  /* close nav when clicking nav links */
  if (navDrawer) {{
    const navLinks = navDrawer.querySelectorAll("a.sb-nav-link");
    navLinks.forEach(function(link) {{
      link.addEventListener("click", closeNav);
    }});
  }}

  /* ============================
     FILTER DRAWER (LEFT)
     ============================ */
  const openBtn = document.getElementById("sb-filter-open");
  const drawer = document.getElementById("sb-filter-drawer");
  const backdrop = document.getElementById("sb-filter-backdrop");
  const closeBtn = document.getElementById("sb-filter-close");
  const applyBtn = document.getElementById("sb-filter-apply");
  const clearBtn = document.getElementById("sb-filter-clear");
  const groupsWrap = document.getElementById("sb-filter-groups");
  const filterCountEl = document.getElementById("sb-filter-count");

  function openDrawer() {{
    drawer?.classList.add("open");
    backdrop?.classList.add("open");
  }}

  function closeDrawer() {{
    drawer?.classList.remove("open");
    backdrop?.classList.remove("open");
  }}

  openBtn?.addEventListener("click", openDrawer);
  backdrop?.addEventListener("click", closeDrawer);
  closeBtn?.addEventListener("click", closeDrawer);

  /* ============================
     FILTER SYSTEM (checkbox sets)
     ============================ */
  if (!grid || !cards.length || !sortSelect || !countEl || !groupsWrap || !applyBtn || !clearBtn || !filterCountEl) {{
    return;
  }}

  // Preserve original order
  cards.forEach(function(card, idx) {{
    card.dataset.originalIndex = String(idx);
  }});

  function uniq(values) {{
    return Array.from(new Set(values.filter(Boolean))).sort();
  }}

  const retailers = uniq(cards.map(function(c) {{ return c.dataset.retailer; }}));
  const sections  = uniq(cards.map(function(c) {{ return c.dataset.section; }}));
  const categories = uniq(cards.map(function(c) {{ return c.dataset.category; }}));

  const selected = {{
    retailer: new Set(),
    section: new Set(),
    category: new Set(),
  }};

  function titleize(s) {{
    if (!s) return "";
    return s.charAt(0).toUpperCase() + s.slice(1);
  }}

  function updateFilterCount() {{
    const total = selected.retailer.size + selected.section.size + selected.category.size;
    filterCountEl.textContent = "(" + total + ")";
  }}

  function buildGroup(key, title, options) {{
    const group = document.createElement("div");
    group.className = "sb-filter-group open";

    const header = document.createElement("div");
    header.className = "sb-filter-group-header";

    const left = document.createElement("div");
    left.className = "sb-filter-group-title";
    left.textContent = title;

    const meta = document.createElement("div");
    meta.className = "sb-filter-group-meta";
    meta.textContent = "Any";

    header.appendChild(left);
    header.appendChild(meta);

    const body = document.createElement("div");
    body.className = "sb-filter-options";

    function updateMeta() {{
      const n = selected[key].size;
      meta.textContent = n === 0 ? "Any" : (n + " selected");
    }}

    options.forEach(function(optVal) {{
      const label = document.createElement("label");
      label.className = "sb-check";

      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = optVal;

      cb.addEventListener("change", function() {{
        if (cb.checked) selected[key].add(optVal);
        else selected[key].delete(optVal);
        updateMeta();
        updateFilterCount();
      }});

      const span = document.createElement("span");
      span.textContent = titleize(optVal);

      label.appendChild(cb);
      label.appendChild(span);
      body.appendChild(label);
    }});

    header.addEventListener("click", function(e) {{
      if (e.target && e.target.tagName === "INPUT") return;
      group.classList.toggle("open");
    }});

    updateMeta();

    group.appendChild(header);
    group.appendChild(body);

    group._key = key;
    group._updateMeta = updateMeta;
    return group;
  }}

  // Render groups
  groupsWrap.innerHTML = "";
  const groupEls = [
    buildGroup("retailer", "Retailer", retailers),
    buildGroup("section", "Section", sections),
    buildGroup("category", "Category", categories),
  ];
  groupEls.forEach(function(g) {{ groupsWrap.appendChild(g); }});

  function syncCheckboxes(key) {{
    const group = groupEls.find(function(g) {{ return g._key === key; }});
    if (!group) return;
    const inputs = group.querySelectorAll('input[type="checkbox"]');
    inputs.forEach(function(cb) {{
      cb.checked = selected[key].has(cb.value);
    }});
    if (group._updateMeta) group._updateMeta();
  }}

  function renderChips() {{
    if (!chipsWrap || !activeSection || !clearAllBtn) return;

    chipsWrap.innerHTML = "";

    function addChip(label, value, onClear) {{
      const chip = document.createElement("div");
      chip.className = "sb-chip";
      chip.innerHTML =
        "<small>" + label + ":</small> " + value +
        ' <button type="button" aria-label="Clear ' + label + '">×</button>';
      chip.querySelector("button").addEventListener("click", onClear);
      chipsWrap.appendChild(chip);
    }}

    selected.retailer.forEach(function(v) {{
      addChip("Retailer", titleize(v), function() {{
        selected.retailer.delete(v);
        syncCheckboxes("retailer");
        updateFilterCount();
        applyFiltersAndSort();
      }});
    }});

    selected.section.forEach(function(v) {{
      addChip("Section", titleize(v), function() {{
        selected.section.delete(v);
        syncCheckboxes("section");
        updateFilterCount();
        applyFiltersAndSort();
      }});
    }});

    selected.category.forEach(function(v) {{
      addChip("Category", titleize(v), function() {{
        selected.category.delete(v);
        syncCheckboxes("category");
        updateFilterCount();
        applyFiltersAndSort();
      }});
    }});

    activeSection.style.display = chipsWrap.children.length ? "flex" : "none";
  }}

  function applyFiltersAndSort() {{
    const sortMode = sortSelect.value;

    const selR = selected.retailer;
    const selS = selected.section;
    const selC = selected.category;

    let shown = [];

    cards.forEach(function(card) {{
      const r = card.dataset.retailer;
      const s = card.dataset.section;
      const c = card.dataset.category;

      const matchR = selR.size === 0 || selR.has(r);
      const matchS = selS.size === 0 || selS.has(s);
      const matchC = selC.size === 0 || selC.has(c);

      if (matchR && matchS && matchC) {{
        card.style.display = "";
        shown.push(card);
      }} else {{
        card.style.display = "none";
      }}
    }});

    shown.sort(function(a, b) {{
      const aPrice = parseFloat(a.dataset.price || "0");
      const bPrice = parseFloat(b.dataset.price || "0");
      const aPct = parseFloat(a.dataset.percent || "0");
      const bPct = parseFloat(b.dataset.percent || "0");
      const aIdx = parseInt(a.dataset.originalIndex || "0", 10);
      const bIdx = parseInt(b.dataset.originalIndex || "0", 10);

      if (sortMode === "price_asc") {{
        if (aPrice !== bPrice) return aPrice - bPrice;
        return bPct - aPct;
      }}
      if (sortMode === "price_desc") {{
        if (aPrice !== bPrice) return bPrice - aPrice;
        return bPct - aPct;
      }}
      return aIdx - bIdx;
    }});

    shown.forEach(function(card) {{
      grid.appendChild(card);
    }});

    countEl.textContent = "👀 Showing " + shown.length + " deals";
    renderChips();
  }}

  function clearAll() {{
    selected.retailer.clear();
    selected.section.clear();
    selected.category.clear();

    groupEls.forEach(function(g) {{
      const inputs = g.querySelectorAll('input[type="checkbox"]');
      inputs.forEach(function(cb) {{ cb.checked = false; }});
      if (g._updateMeta) g._updateMeta();
    }});

    updateFilterCount();
    applyFiltersAndSort();
  }}

  // Drawer buttons
  applyBtn.addEventListener("click", function() {{
    applyFiltersAndSort();
    closeDrawer();
  }});

  clearBtn.addEventListener("click", clearAll);

  // Existing chip-row Clear all
  clearAllBtn?.addEventListener("click", clearAll);

  sortSelect.addEventListener("change", applyFiltersAndSort);

  // Initial
  updateFilterCount();
  applyFiltersAndSort();

  /* ============================
     FLAVOR EXPAND/COLLAPSE
     ============================ */
  const flavorExpandLinks = document.querySelectorAll(".flavor-expand-link");
  flavorExpandLinks.forEach(function(link) {{
    link.addEventListener("click", function() {{
      const cardId = this.getAttribute("data-card-id");
      const expandedList = document.getElementById("flavors-" + cardId);
      if (!expandedList) return;

      const isExpanded = this.getAttribute("aria-expanded") === "true";
      
      if (isExpanded) {{
        // Collapse
        expandedList.style.display = "none";
        this.setAttribute("aria-expanded", "false");
        const extraCount = this.textContent.match(/\\d+/);
        if (extraCount) {{
          this.textContent = "+ " + extraCount[0] + " more flavor" + (parseInt(extraCount[0]) > 1 ? "s" : "");
        }}
      }} else {{
        // Expand
        expandedList.style.display = "flex";
        this.setAttribute("aria-expanded", "true");
        this.textContent = "Show less";
      }}
    }});
  }});
}});
</script>

</body>
</html>
    """
def main():
    deals = load_deals()
    page_html = build_page_html(deals)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(page_html, encoding="utf-8")
    print("SCRIPT FILE:", Path(__file__).resolve())
    print("OUTPUT PATH:", OUTPUT_PATH.resolve())
    print("WROTE:", OUTPUT_PATH.resolve())
    print("DEALS:", len(deals))

if __name__ == "__main__":
    main()                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              