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


def get_badge(deal):
    """
    Decide badge label & class from deal_strength.
    deal_strength examples: '🔥 strong', '🟡 mild', '', etc.
    """
    strength = (deal.get("deal_strength") or "").strip().lower()

    if "strong" in strength or "🔥" in strength:
        # Strong deals everywhere: 🔥 Strong deal
        return "🔥 Strong Deal", "badge badge-strong"
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

    # Normalized values for filters (used in data-* attributes)
    section_raw = (deal.get("section") or "").strip().lower()
    category_raw = (deal.get("category") or "").strip().lower()
    retailer_raw = (deal.get("retailer") or "").strip().lower()

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
    <div class="card" data-section="{section_attr}" data-category="{category_attr}" data-retailer="{retailer_attr}">
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

        .sb-logo {{
            width: 32px;
            height: 32px;
            border-radius: 8px;
            background-color: #e5f3ff; /* placeholder if no logo image yet */
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
            opacity: 0.9;
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
        /* SECTION 1C: SORT + COUNT UTIL ROW   */
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
    min-width: 120px;
}}

        /* ============================ */
        /* SECTION 2: DEAL CARD GRID    */
        /* (unchanged from before)     */
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
    color: var(--text-main);  /* black/dark text */
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

        @media (max-width: 640px) {{
            .page {{
                padding-top: 8px;
            }}
        }}
    </style>
</head>
<body>
    <!-- Backdrop + Nav Drawer for mobile menu -->
    <div class="sb-nav-backdrop" id="sb-nav-backdrop"></div>
    <nav class="sb-nav-drawer" id="sb-nav-drawer">
        <div class="sb-nav-inner">
            <div class="sb-nav-header">
                <div class="sb-nav-title">SnackBuddy</div>
                <button class="sb-nav-close" aria-label="Close menu">&times;</button>
            </div>
            <a href="#deals-list" class="sb-nav-link">All deals</a>
            <a href="#about" class="sb-nav-link">About SnackBuddy</a>
            <a href="#how-it-works" class="sb-nav-link">How SnackBuddy works</a>
            <a href="#subscribe" class="sb-nav-link">Get daily deal emails</a>
            <div class="sb-nav-footer">
                SnackBuddy helps you find the best healthy snack prices at your favorite retailers.
            </div>
        </div>
    </nav>

    <!-- ============================ -->
    <!-- SECTION 1A: NAVBAR           -->
    <!-- ============================ -->
    <header class="sb-header">
        <div class="sb-header-inner">
            <div class="sb-header-left">
                <div class="sb-logo"></div>
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
                    <h1 class="sb-hero-title">Snack, Save, Repeat.</h1>
                    <p class="sb-hero-subtitle">
                        Today&apos;s best healthy snack deals — {total} found.
                    </p>
                    <a href="#deals-list" class="sb-hero-cta">
                        Get notified about new snack deals →
                    </a>
                </div>
            </div>
        </section>

        <!-- =================================== -->
        <!-- SECTION 1C: SORT + DEAL COUNT ROW   -->
        <!-- =================================== -->
<section class="sb-utility-row">
    <div class="pill-label">Filters</div>

    <div class="filter-controls">
        
        <!-- Retailer Filter -->
        <select id="filter-retailer" class="sb-filter">
            <option value="">All retailers</option>
            <option value="walmart">Walmart</option>
            <option value="kroger">Kroger</option>
            <option value="target">Target</option>
        </select>

        <!-- Section Filter -->
        <select id="filter-section" class="sb-filter">
            <option value="">All sections</option>
            <option value="food">Food</option>
            <option value="drinks">Drinks</option>
        </select>

        <!-- Category Filter -->
        <select id="filter-category" class="sb-filter">
            <option value="">All categories</option>
            <!-- JS will populate -->
        </select>

    </div>

    <div id="deal-count" class="count-pill">
        👀 Showing {total} deals
    </div>
</section>

        <!-- ============================ -->
        <!-- SECTION 2: DEAL CARD GRID    -->
        <!-- ============================ -->
        <section class="card-grid" id="deals-list">
            {cards_html}
        </section>
    </main>

    <!-- Tiny JS for slide-out menu -->
    <script>
      document.addEventListener("DOMContentLoaded", function() {{
        const menuButton = document.querySelector(".sb-menu-button");
        const drawer = document.getElementById("sb-nav-drawer");
        const backdrop = document.getElementById("sb-nav-backdrop");
        const closeButton = document.querySelector(".sb-nav-close");

        if (!menuButton || !drawer || !backdrop) return;

        function openNav() {{
          drawer.classList.add("open");
          backdrop.classList.add("open");
          menuButton.setAttribute("aria-expanded", "true");
        }}

        function closeNav() {{
          drawer.classList.remove("open");
          backdrop.classList.remove("open");
          menuButton.setAttribute("aria-expanded", "false");
        }}

        menuButton.addEventListener("click", function() {{
          if (drawer.classList.contains("open")) {{
            closeNav();
          }} else {{
            openNav();
          }}
        }});

        backdrop.addEventListener("click", closeNav);
        if (closeButton) {{
          closeButton.addEventListener("click", closeNav);
        }}
      }});
    </script>

    <!-- JS for filters -->
    <script>
      document.addEventListener("DOMContentLoaded", function () {{
        const cards = Array.from(document.querySelectorAll(".card"));
        const retailerFilter = document.getElementById("filter-retailer");
        const sectionFilter = document.getElementById("filter-section");
        const categoryFilter = document.getElementById("filter-category");
        const countEl = document.getElementById("deal-count");

        if (!cards.length || !retailerFilter || !sectionFilter || !categoryFilter || !countEl) {{
          return;
        }}

        // Build dynamic category list from data-category attributes
        const categories = Array.from(
          new Set(
            cards
              .map(function (c) {{ return c.dataset.category || ""; }})
              .filter(function (val) {{ return val; }})
          )
        ).sort();

        categories.forEach(function (cat) {{
          const opt = document.createElement("option");
          opt.value = cat;
          opt.textContent = cat.charAt(0).toUpperCase() + cat.slice(1);
          categoryFilter.appendChild(opt);
        }});

        function applyFilters() {{
          const r = retailerFilter.value;
          const s = sectionFilter.value;
          const c = categoryFilter.value;

          let shown = 0;

          cards.forEach(function (card) {{
            const matchRetailer = !r || card.dataset.retailer === r;
            const matchSection = !s || card.dataset.section === s;
            const matchCategory = !c || card.dataset.category === c;

            if (matchRetailer && matchSection && matchCategory) {{
              card.style.display = "";
              shown++;
            }} else {{
              card.style.display = "none";
            }}
          }});

          countEl.textContent = "👀 Showing " + shown + " deals";
        }}

        retailerFilter.addEventListener("change", applyFilters);
        sectionFilter.addEventListener("change", applyFilters);
        categoryFilter.addEventListener("change", applyFilters);
      }});
    </script>
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


