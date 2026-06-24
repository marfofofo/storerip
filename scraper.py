import requests
import csv
import sys
import re
from urllib.parse import urlparse
import warnings
warnings.filterwarnings("ignore")

def detect_platform(base_url):
    try:
        r = requests.get(f"{base_url}/wp-json/wc/store/v1/products?per_page=1", timeout=10)
        if r.status_code == 200:
            return "woocommerce"
    except:
        pass
    try:
        r = requests.get(f"{base_url}/products.json?limit=1", timeout=10)
        if r.status_code == 200 and "products" in r.json():
            return "shopify"
    except:
        pass
    return None

def fetch_woocommerce_products(base_url):
    products = []
    page = 1
    while True:
        url = f"{base_url}/wp-json/wc/store/v1/products?per_page=100&page={page}"
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            break
        data = r.json()
        if not data:
            break
        products.extend(data)
        print(f"  -> Pagina {page}: {len(data)} prodotti scaricati")
        if len(data) < 100:
            break
        page += 1
    return products

def fetch_shopify_products(base_url):
    products = []
    page = 1
    while True:
        url = f"{base_url}/products.json?limit=250&page={page}"
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            break
        data = r.json().get("products", [])
        if not data:
            break
        products.extend(data)
        print(f"  -> Pagina {page}: {len(data)} prodotti scaricati")
        if len(data) < 250:
            break
        page += 1
    return products

def clean_html(value):
    if not value:
        return ""
    if isinstance(value, dict):
        value = value.get("raw", value.get("rendered", ""))
    return re.sub("<[^>]+>", "", str(value)).strip()

def wc_store_to_rows(products):
    rows = []
    for p in products:
        sku   = p.get("sku", "")
        name  = p.get("name", "")
        desc  = clean_html(p.get("description", ""))
        short = clean_html(p.get("short_description", ""))
        prices = p.get("prices", {})
        price = prices.get("price", "") if isinstance(prices, dict) else ""
        sale  = prices.get("sale_price", "") if isinstance(prices, dict) else ""
        stock = p.get("is_in_stock", False)
        qty   = p.get("stock_quantity", "")
        cats  = ", ".join([c.get("name", "") for c in p.get("categories", [])])
        imgs  = ", ".join([i.get("src", "") for i in p.get("images", [])])
        weight = p.get("weight", "")
        variations = p.get("variations", [])
        attributes = p.get("attributes", [])

        attr_cols = {}
        for i, attr in enumerate(attributes, 1):
            attr_cols[f"Attributo {i} nome"] = attr.get("name", "")
            vals = "|".join([t.get("name", "") for t in attr.get("terms", [])])
            attr_cols[f"Attributo {i} valore(i)"] = vals
            attr_cols[f"Attributo {i} visibile"] = 1
            attr_cols[f"Attributo {i} globale"] = 1
            attr_cols[f"Attributo {i} per variazioni"] = 1 if variations else 0

        parent_row = {
            "ID": "", "Tipo": "variable" if variations else "simple",
            "SKU": sku, "Nome": name, "Pubblicato": 1,
            "In primo piano?": 0, "Visibilita nel catalogo": "visible",
            "Descrizione breve": short, "Tassazione": "taxable",
            "Classe di tassazione": "", "In stock?": 1 if stock else 0,
            "Stock": qty if not variations else "",
            "Quantita backorders": 0, "Venduto singolarmente?": 0,
            "Peso (kg)": weight, "Lunghezza (cm)": "", "Larghezza (cm)": "", "Altezza (cm)": "",
            "Permettere le recensioni?": 1, "Nota per l acquisto": "",
            "Prezzo di listino": price if not variations else "",
            "Prezzo di vendita": sale if not variations else "",
            "Classe di spedizione": "", "Immagini": imgs,
            "Limite download": "", "Giorni scadenza download": "",
            "Genitore": "", "Raggruppamento": "", "Cross-sells": "", "Up-sells": "",
            "Descrizione": desc, "Categorie": cats, "Tag": "", "Posizione": 0,
            **attr_cols
        }
        rows.append(parent_row)

        for v in variations:
            v_attrs = {}
            for i, attr in enumerate(v.get("attributes", []), 1):
                v_attrs[f"Attributo {i} nome"] = attr.get("name", "")
                v_attrs[f"Attributo {i} valore(i)"] = attr.get("value", "")
                v_attrs[f"Attributo {i} visibile"] = 1
                v_attrs[f"Attributo {i} globale"] = 1
                v_attrs[f"Attributo {i} per variazioni"] = 1

            vp = v.get("prices", {})
            v_price = vp.get("price", "") if isinstance(vp, dict) else ""
            v_sale  = vp.get("sale_price", "") if isinstance(vp, dict) else ""
            v_stock = v.get("is_in_stock", False)
            v_qty   = v.get("stock_quantity", "")
            v_imgs  = ", ".join([i.get("src", "") for i in v.get("images", [])]) if v.get("images") else ""

            v_row = {
                "ID": "", "Tipo": "variation",
                "SKU": v.get("sku", f"{sku}-{v.get('id', '')}"),
                "Nome": name, "Pubblicato": 1,
                "In primo piano?": 0, "Visibilita nel catalogo": "visible",
                "Descrizione breve": "", "Tassazione": "taxable",
                "Classe di tassazione": "", "In stock?": 1 if v_stock else 0,
                "Stock": v_qty, "Quantita backorders": 0, "Venduto singolarmente?": 0,
                "Peso (kg)": v.get("weight", ""),
                "Lunghezza (cm)": "", "Larghezza (cm)": "", "Altezza (cm)": "",
                "Permettere le recensioni?": 1, "Nota per l acquisto": "",
                "Prezzo di listino": v_price, "Prezzo di vendita": v_sale,
                "Classe di spedizione": "", "Immagini": v_imgs,
                "Limite download": "", "Giorni scadenza download": "",
                "Genitore": sku,
                "Raggruppamento": "", "Cross-sells": "", "Up-sells": "",
                "Descrizione": "", "Categorie": "", "Tag": "", "Posizione": 0,
                **v_attrs
            }
            rows.append(v_row)
    return rows

def shopify_to_rows(products):
    rows = []
    for p in products:
        name     = p.get("title", "")
        desc     = clean_html(p.get("body_html", ""))
        cats     = p.get("product_type", "")
        tags     = p.get("tags", "")
        imgs     = ", ".join([i.get("src", "") for i in p.get("images", [])])
        variants = p.get("variants", [])
        options  = p.get("options", [])
        has_vars = len(variants) > 1
        parent_sku = variants[0].get("sku", "") if variants else ""

        attr_cols = {}
        for i, opt in enumerate(options, 1):
            vals = "|".join(set(v.get(f"option{i}", "") for v in variants))
            attr_cols[f"Attributo {i} nome"] = opt.get("name", "")
            attr_cols[f"Attributo {i} valore(i)"] = vals
            attr_cols[f"Attributo {i} visibile"] = 1
            attr_cols[f"Attributo {i} globale"] = 1
            attr_cols[f"Attributo {i} per variazioni"] = 1 if has_vars else 0

        parent_row = {
            "ID": "", "Tipo": "variable" if has_vars else "simple",
            "SKU": parent_sku, "Nome": name, "Pubblicato": 1,
            "In primo piano?": 0, "Visibilita nel catalogo": "visible",
            "Descrizione breve": "", "Tassazione": "taxable",
            "Classe di tassazione": "",
            "In stock?": 1 if variants and variants[0].get("inventory_quantity", 0) > 0 else 0,
            "Stock": variants[0].get("inventory_quantity", "") if not has_vars else "",
            "Quantita backorders": 0, "Venduto singolarmente?": 0,
            "Peso (kg)": variants[0].get("weight", "") if not has_vars else "",
            "Lunghezza (cm)": "", "Larghezza (cm)": "", "Altezza (cm)": "",
            "Permettere le recensioni?": 1, "Nota per l acquisto": "",
            "Prezzo di listino": variants[0].get("price", "") if not has_vars else "",
            "Prezzo di vendita": variants[0].get("compare_at_price", "") if not has_vars else "",
            "Classe di spedizione": "", "Immagini": imgs,
            "Limite download": "", "Giorni scadenza download": "",
            "Genitore": "", "Raggruppamento": "", "Cross-sells": "", "Up-sells": "",
            "Descrizione": desc, "Categorie": cats, "Tag": tags, "Posizione": 0,
            **attr_cols
        }
        rows.append(parent_row)

        if has_vars:
            for v in variants:
                v_attrs = {}
                for i, opt in enumerate(options, 1):
                    v_attrs[f"Attributo {i} nome"] = opt.get("name", "")
                    v_attrs[f"Attributo {i} valore(i)"] = v.get(f"option{i}", "")
                    v_attrs[f"Attributo {i} visibile"] = 1
                    v_attrs[f"Attributo {i} globale"] = 1
                    v_attrs[f"Attributo {i} per variazioni"] = 1

                v_row = {
                    "ID": "", "Tipo": "variation",
                    "SKU": v.get("sku", f"{parent_sku}-{v.get('id', '')}"),
                    "Nome": name, "Pubblicato": 1,
                    "In primo piano?": 0, "Visibilita nel catalogo": "visible",
                    "Descrizione breve": "", "Tassazione": "taxable",
                    "Classe di tassazione": "",
                    "In stock?": 1 if v.get("inventory_quantity", 0) > 0 else 0,
                    "Stock": v.get("inventory_quantity", ""),
                    "Quantita backorders": 0, "Venduto singolarmente?": 0,
                    "Peso (kg)": v.get("weight", ""),
                    "Lunghezza (cm)": "", "Larghezza (cm)": "", "Altezza (cm)": "",
                    "Permettere le recensioni?": 1, "Nota per l acquisto": "",
                    "Prezzo di listino": v.get("price", ""),
                    "Prezzo di vendita": v.get("compare_at_price", ""),
                    "Classe di spedizione": "", "Immagini": "",
                    "Limite download": "", "Giorni scadenza download": "",
                    "Genitore": parent_sku,
                    "Raggruppamento": "", "Cross-sells": "", "Up-sells": "",
                    "Descrizione": "", "Categorie": "", "Tag": "", "Posizione": 0,
                    **v_attrs
                }
                rows.append(v_row)
    return rows

def write_csv(rows, filename):
    if not rows:
        print("Nessun prodotto trovato.")
        return
    all_keys = []
    for row in rows:
        for k in row.keys():
            if k not in all_keys:
                all_keys.append(k)
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=all_keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in all_keys})
    print(f"Salvato: {filename} ({len(rows)} righe)")

if __name__ == "__main__":
    base_url = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else input("URL sito: ").strip().rstrip("/")
    print(f"Rilevamento piattaforma per {base_url}...")
    platform = detect_platform(base_url)
    if platform == "woocommerce":
        print("WooCommerce rilevato")
        raw = fetch_woocommerce_products(base_url)
        print(f"Totale prodotti: {len(raw)}")
        rows = wc_store_to_rows(raw)
    elif platform == "shopify":
        print("Shopify rilevato")
        raw = fetch_shopify_products(base_url)
        print(f"Totale prodotti: {len(raw)}")
        rows = shopify_to_rows(raw)
    else:
        print("Piattaforma non rilevata.")
        sys.exit(1)
    domain = urlparse(base_url).netloc.replace(".", "_")
    write_csv(rows, f"{domain}_products.csv")
