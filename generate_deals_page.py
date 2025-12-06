import json
from datetime import datetime
from pathlib import Path
import html

# Path setup
ROOT = Path(__file__).parent
DATA_FILE = ROOT / "deals_today.json"
OUTPUT_DIR = ROOT / "docs"
OUTPUT_FILE = OUTPUT_DIR / "index.html"

OUTPUT_DIR.mkdir(exist_ok=True)

def load_deals():
    with DATA_FILE.open("r", encoding="utf-8") as f:
        deals = json.load(f)

    # Sort: retailer -> category -> highest percent_off first
    deals_sorted = sorted(
        deals,
        key=lambda d: (
            d.get("retailer", "").lower(),
            d.get("category", "").lower(),
            -float(d.get("percent_off", 0.0)),
        ),
    )
    return deals_sorted

def format_price(value):
    if value is None:
        return ""
    return f"${value:0.2f}"

def get_badge_label_and_class(percent_off):
    """
    Decide which visual tier the deal is:
    - big-deal (flame-level)
    - flash-deal
    - standard-deal
    - low-deal
    """
    p = float(percent_off or 0)

    if p >= 30:
        return "🔥 Big Deal", "badge big-deal"
    elif p >= 20:
        return "⚡ Flash Deal", "badge flash-deal"
    elif p >= 10:
        return "Deal", "badge standard-deal"
    else:
        return "Small Deal", "badge low-deal"

def build_card_html(deal):
    name = html.escape(deal.get("product_name", ""))
    retailer = html.escape(deal.get("retailer", ""))
    category = html.escape(deal.get("category", ""))
    old_price = deal.get("old_price")
    new_price = deal.get("new_price")
    percent_off = float(deal.get("percent_off", 0.0))
    image_url = html.escape(deal.get("image_url", ""))
    retailer_url = html.escape(deal.get("retailer_url", "#"))
    availability = html.escape(deal.get("availability", ""))

    badge_label, badge_class = get_badge_label_and_class(percent_off)

    old_price_str = format_price(old_price)
    new_price_str = format_price(new_price)

    percent_str = f"{percent_off:.0f}% OFF" if percent_off > 0 else ""

    availability_label = ""
    if availability == "in_store":
        availability_label = "In-store"
    elif availability == "both":
        availability_label = "In-store & online"
    elif availability == "online_only":
        availability_label = "Online only"

    return f"""
    <article class="deal-card">
      <div class="{badge_class}">{badge_label}</div>
      <div class="image-wrapper">
        <img src="{image_url}" alt="{name}">
      </div>
      <div class="deal-body">
        <h2 class="deal-title">{name}</h2>
        <div class="deal-meta">
          <span class="retailer-pill">{retailer}</span>
          <span class="category-pill">{category}</span>
        </div>
        <div class="price-row">
          <div class="old-price">{old_price_str}</div>
          <div class="arrow">→</div>
          <div class="new-price">{new_price_str}</div>
        </div>
        <div class="percent-row">
          <span class="percent-off">{percent_str}</span>
          {"<span class='availability'>" + availability_label + "</span>" if availability_label else ""}
        </div>
        <a class="retailer-link" href="{retailer_url}" target="_blank" rel="noopener noreferrer">
          View on {retailer}.com
        </a>
      </div>
    </article>
    """

def build_page_html(deals):
    now = datetime.now()
    updated_str = now.strftime("%B %d, %Y at %I:%M %p")

    cards_html = "\n".join(build_card_html(d) for d in deals)

    # Basic HTML + inline CSS
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>SnackBuddy Daily Deals</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {{
      --bg: #f7f7fa;
      --card-bg: #ffffff;
      --border: #e1e1ea;
      --text-main: #222222;
      --text-muted: #666666;
      --accent: #36a852;
      --danger: #c62828;
      --yellow: #f4c542;
      --orange: #f28c28;
      --lowgray: #bbbbbb;
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      padding: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: var(--bg);
      color: var(--text-main);
    }}
    header {{
      background: #ffffff;
      border-bottom: 1px solid var(--border);
      padding: 16px 20px;
      position: sticky;
      top: 0;
      z-index: 10;
    }}
    .header-inner {{
      max-width: 1100px;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .title-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .title-row h1 {{
      font-size: 1.25rem;
      margin: 0;
    }}
    .subtitle {{
      font-size: 0.85rem;
      color: var(--text-muted);
    }}
    main {{
      max-width: 1100px;
      margin: 0 auto;
      padding: 16px 12px 40px;
    }}
    .updated {{
      font-size: 0.8rem;
      color: var(--text-muted);
      margin-bottom: 12px;
    }}
    .filters {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 16px;
      font-size: 0.85rem;
    }}
    .filter-pill {{
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: #ffffff;
      cursor: default;
      color: var(--text-muted);
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 16px;
    }}
    .deal-card {{
      position: relative;
      background: var(--card-bg);
      border-radius: 12px;
      border: 1px solid var(--border);
      padding: 10px 10px 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      box-shadow: 0 2px 6px rgba(0,0,0,0.03);
    }}
    .badge {{
      position: absolute;
      top: 10px;
      right: 10px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 0.7rem;
      font-weight: 600;
      border: 2px solid transparent;
      background: #ffffffee;
    }}
    .big-deal {{
      border-color: var(--orange);
      color: var(--orange);
    }}
    .flash-deal {{
      border-color: var(--orange);
      color: var(--orange);
    }}
    .standard-deal {{
      border-color: var(--yellow);
      color: var(--yellow);
    }}
    .low-deal {{
      border-color: var(--lowgray);
      color: var(--lowgray);
    }}
    .image-wrapper {{
      width: 100%;
      display: flex;
      justify-content: center;
      align-items: center;
      padding-top: 6px;
    }}
    .image-wrapper img {{
      max-width: 100%;
      max-height: 160px;
      object-fit: contain;
    }}
    .deal-body {{
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .deal-title {{
      font-size: 0.9rem;
      margin: 0;
      line-height: 1.3;
      min-height: 2.6em;
    }}
    .deal-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      font-size: 0.75rem;
    }}
    .retailer-pill, .category-pill {{
      padding: 2px 8px;
      border-radius: 999px;
      background: #f0f1f7;
    }}
    .price-row {{
      display: flex;
      align-items: baseline;
      gap: 6px;
      font-size: 0.9rem;
    }}
    .old-price {{
      color: var(--danger);
      text-decoration: line-through;
      font-size: 0.8rem;
    }}
    .arrow {{
      font-size: 0.8rem;
      color: var(--text-muted);
    }}
    .new-price {{
      color: var(--accent);
      font-weight: 700;
    }}
    .percent-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      font-size: 0.75rem;
      color: var(--text-muted);
    }}
    .percent-off {{
      font-weight: 600;
    }}
    .availability {{
      padding-left: 6px;
      border-left: 1px solid var(--border);
    }}
    .retailer-link {{
      margin-top: 4px;
      display: inline-block;
      font-size: 0.8rem;
      text-decoration: none;
      padding: 6px 10px;
      border-radius: 6px;
      background: #1a73e8;
      color: #ffffff;
      text-align: center;
      font-weight: 500;
    }}
    .retailer-link:hover {{
      background: #1558ad;
    }}

    @media (max-width: 600px) {{
      header {{
        padding: 10px 12px;
      }}
      main {{
        padding: 12px 8px 28px;
      }}
      .deal-card {{
        padding: 8px 8px 10px;
      }}
      .image-wrapper img {{
        max-height: 140px;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <div class="title-row">
        <h1>SnackBuddy Daily Deals</h1>
      </div>
      <div class="subtitle">
        Verified snack deals from your favorite retailers. Tap a card to open the retailer's page for price checks and comps.
      </div>
    </div>
  </header>
  <main>
    <div class="updated">Last updated: {updated_str} (local time)</div>
    <div class="filters">
      <div class="filter-pill">Sorted by: Retailer → Category → Best savings</div>
      <div class="filter-pill">Showing {len(deals)} deals</div>
    </div>
    <section class="grid">
      {cards_html}
    </section>
  </main>
</body>
</html>
"""

def main():
    deals = load_deals()
    html_content = build_page_html(deals)
    OUTPUT_FILE.write_text(html_content, encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE} with {len(deals)} deals.")

if __name__ == "__main__":
    main()
