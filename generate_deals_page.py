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


def normalise_availability(raw):
    if not isinstance(raw, str):
        return "", ""
    val = raw.strip().lower()
    if val == "in_store":
        return "In-store", "availability in-store"
    if val == "both":
        return "In-store & online", "availability both"
    if val == "online_only":
        return "Online only", "availability online-only"
    return "", "availability"


def get_badge(deal):
    """
    Decide badge label & class from deal_strength.
    deal_strength examples: '🔥 strong', '🟡 mild', '', etc.
    """
    strength = (deal.get("deal_strength") or "").strip().lower()

    if "strong" in strength or "🔥" in strength:
        # Strong deals everywhere: 🔥 Strong deal
        return "🔥 Strong deal", "badge badge-strong"
    if "mild" in strength or "🟡" in strength:
        return "Deal", "badge badge-regular"
    # No badge
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
    name = html.escape(deal.get("product_name", ""))
    retailer = html.escape(deal.get("retailer", ""))
    category = html.escape(deal.get("category", ""))
    old_price = deal.get("old_price")
    new_price = deal.get("new_price")
    percent_off = float(deal.get("percent_off", 0.0))
    image_url = html.escape(deal.get("image_url", ""))
    retailer_url = html.escape(deal.get("retailer_url", "#"))

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

    old_price_html = ""
    if old_price is not None:
        try:
            old_price_val = float(old_price)
            if old_price_val > float(new_price):
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

    return f"""
    <div class="card">
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
            <div class="card-title">{name}</div>
            <div class="card-pricing">
                {old_price_html}
                <span class="new-price">${new_price:.2f}</span>
                <span class="percent-off">{percent_off:.0f}% OFF</span>
            </div>
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
    total = len(deals)
    last_updated = get_last_updated_text(deals)

    # Sort: retailer → category → best savings
    def sort_key(d):
        return (
            (d.get("retailer") or "").lower(),
            (d.get("category") or "").lower(),
            -float(d.get("percent_off") or 0.0),
            float(d.get("new_price") or 0.0),
        )

    deals_sorted = sorted(deals, key=sort_key)

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
            margin: 24px auto 40px auto;
            padding: 0 16px;
        }}

        .page-header {{
            margin-bottom: 16px;
        }}

        .page-title {{
            font-size: 26px;
            font-weight: 700;
            margin: 0 0 4px 0;
        }}

        .page-subtitle {{
            font-size: 14px;
            color: var(--text-muted);
            margin: 0 0 6px 0;
        }}

        .timestamp {{
            font-size: 12px;
            color: var(--text-muted);
        }}

        .controls-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 12px;
            font-size: 12px;
            color: var(--text-muted);
        }}

        .pill-label {{
            padding: 4px 10px;
            border-radius: var(--radius-pill);
            background-color: #e5e7eb;
        }}

        .count-pill {{
            padding: 4px 10px;
            border-radius: var(--radius-pill);
            background-color: #e5e7eb;
        }}

        .card-grid {{
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

        /* Brand-true retailer pills */
        .retailer-walmart {{
            background-color: #0071CE; /* Walmart blue */
            color: #F9C80E;           /* Walmart spark gold */
        }}

        .retailer-kroger {{
            background-color: #003087; /* Kroger navy */
            color: #ffffff;
        }}

        .retailer-target {{
            background-color: #CC0000; /* Target red */
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
        }}

        .card-pricing {{
            display: flex;
            align-items: baseline;
            gap: 6px;
            font-size: 13px;
        }}

        .old-price {{
            color: #ef4444;
            text-decoration: line-through;
        }}

        .new-price {{
            color: var(--green);
            font-weight: 700;
        }}

        .percent-off {{
            color: var(--text-muted);
            font-size: 11px;
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

        .availability {{
            color: var(--text-muted);
        }}

        .streak {{
            color: var(--text-muted);
            font-style: italic;
        }}

        /* Base button style */
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

        /* Brand-true retailer buttons */

        .view-walmart {{
            background-color: #0071CE; /* Walmart blue */
            color: #F9C80E;            /* Walmart spark gold text */
        }}

        .view-kroger {{
            background-color: #003087; /* Kroger navy */
            color: #ffffff;
        }}

        .view-target {{
            background-color: #CC0000; /* Target red */
            color: #ffffff;
        }}

        .view-generic {{
            background-color: #4b5563;
            color: #ffffff;
        }}

        .view-button:hover {{
            filter: brightness(0.95);
        }}

        @media (max-width: 640px) {{
            .page {{
                margin-top: 16px;
            }}
        }}
    </style>
</head>
<body>
    <main class="page">
        <header class="page-header">
            <h1 class="page-title">SnackBuddy Daily Deals</h1>
            <p class="page-subtitle">
                Verified snack deals from your favorite retailers. Tap a card to open the retailer&apos;s page for price checks and comps.
            </p>
            <div class="timestamp">
                Last updated: {last_updated} (local time)
            </div>
        </header>

        <section class="controls-row">
            <div class="pill-label">Sorted by: Retailer → Category → Best savings</div>
            <div class="count-pill">Showing {total} deals</div>
        </section>

        <section class="card-grid">
            {cards_html}
        </section>
    </main>
</body>
</html>
    """


def main():
    deals = load_deals()
    html_content = build_page_html(deals)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(html_content, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} with {len(deals)} deals.")


if __name__ == "__main__":
    main()


