"""
apify_to_sheets.py — Minas Clean
Coleta dados de concorrentes via Apify (gio21/shopee-scraper)
e salva na aba 'concorrentes' do Google Sheets.

Uso: python apify_to_sheets.py
Agendar: todo dia às 06:00 via Agendador de Tarefas do Windows
"""

import os, json, tempfile
import requests
import time
from datetime import date
import gspread
from google.oauth2.service_account import Credentials

# ── CONFIGURAÇÃO ──────────────────────────────────────────────
# Funciona local (hardcoded) e no GitHub Actions (variável de ambiente)
APIFY_TOKEN  = os.environ.get("APIFY_TOKEN", "")  # configure via GitHub Secrets
ACTOR_ID     = "gio21~shopee-scraper"

SHEET_ID     = "1IXN9PtJnqJfXDevC7Iy1FTBbrMF-rquXFZdlaaIRTDI"
ABA_NOME     = "concorrentes"

# Credenciais Google: arquivo local ou variável de ambiente (GitHub Actions)
_CREDS_ENV = os.environ.get("GOOGLE_CREDENTIALS")
if _CREDS_ENV:
    _tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    _tmp.write(_CREDS_ENV)
    _tmp.close()
    SERVICE_ACCOUNT_FILE = _tmp.name
else:
    SERVICE_ACCOUNT_FILE = r"C:\Users\joaom\OneDrive\Documentos\shopee_ads_dashboard\mnt\user-data\outputs\shopee_ads_dashboard\scripts\credentials.json"

# Buscas de concorrentes — todos os tamanhos do catálogo Minas Clean
TERMOS_BUSCA = [
    "pano microfibra 35x35",
    "pano microfibra 40x40",
    "pano microfibra 40x60",
    "pano microfibra 35x55",
    "pano microfibra 50x70",
    "pano microfibra 60x80",
    "pano chao microfibra gigante 70x100",
    "pano microfibra 30x30",
]

# URL da sua loja — coletada separadamente com tag "minha_loja"
MINHA_LOJA_URL = "https://shopee.com.br/minasclean"
MINHA_LOJA_TAG = "minha_loja"

MAX_ITENS = 20  # por termo — ajuste conforme créditos disponíveis
# ─────────────────────────────────────────────────────────────

def rodar_apify(termo):
    """Dispara o Actor e aguarda conclusão. Retorna lista de produtos."""
    print(f"\n  🔍 Buscando: '{termo}'")

    # Inicia o run
    url = f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs?token={APIFY_TOKEN}"
    payload = {
        "location": termo,
        "maxItems": MAX_ITENS,
        "countryCode": "BR",
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    run = resp.json()["data"]
    run_id = run["id"]
    print(f"  Run ID: {run_id} — aguardando...")

    # Aguarda conclusão (máx 3 min)
    for _ in range(18):
        time.sleep(10)
        status_url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_TOKEN}"
        info = requests.get(status_url, timeout=15).json()["data"]
        status = info["status"]
        print(f"  Status: {status}")
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break

    if status != "SUCCEEDED":
        print(f"  ⚠️ Run não concluído: {status}")
        return []

    # Busca resultados do dataset
    dataset_id = info["defaultDatasetId"]
    items_url = (
        f"https://api.apify.com/v2/datasets/{dataset_id}/items"
        f"?token={APIFY_TOKEN}&format=json&clean=true"
    )
    items = requests.get(items_url, timeout=30).json()
    print(f"  ✅ {len(items)} produtos coletados")
    return items

def rodar_apify_loja(shop_url):
    """Coleta produtos de uma loja específica via shopUrls."""
    print(f"  🏪 Coletando loja: {shop_url}")
    url = f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs?token={APIFY_TOKEN}"
    payload = {
        "shopUrls": [shop_url],
        "maxItems": MAX_ITENS,
        "countryCode": "BR",
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    run = resp.json()["data"]
    run_id = run["id"]
    print(f"  Run ID: {run_id} — aguardando...")

    for _ in range(18):
        time.sleep(10)
        status_url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_TOKEN}"
        info = requests.get(status_url, timeout=15).json()["data"]
        status = info["status"]
        print(f"  Status: {status}")
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break

    if status != "SUCCEEDED":
        print(f"  ⚠️ Run falhou: {status}")
        return []

    dataset_id = info["defaultDatasetId"]
    items_url = (
        f"https://api.apify.com/v2/datasets/{dataset_id}/items"
        f"?token={APIFY_TOKEN}&format=json&clean=true"
    )
    items = requests.get(items_url, timeout=30).json()
    print(f"  ✅ {len(items)} produtos coletados da loja")
    return items

def conectar_sheets():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    return gspread.authorize(creds)

DIAS_RETENCAO = 60  # apaga linhas com mais de 60 dias

def garantir_aba(sh):
    """Cria a aba 'concorrentes' se não existir e define cabeçalho."""
    try:
        ws = sh.worksheet(ABA_NOME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=ABA_NOME, rows=5000, cols=15)

    cabecalho = [
        "data", "termo_busca", "loja", "produto",
        "preco_min", "preco_max", "desconto_pct",
        "vendas_estimadas", "rating", "qtd_avaliacoes",
        "estoque", "is_on_sale", "url"
    ]
    dados = ws.get_all_values()
    if not dados or dados[0] != cabecalho:
        ws.clear()
        ws.append_row(cabecalho)
    return ws

def limpar_dados_antigos(ws):
    """Remove linhas com data anterior a DIAS_RETENCAO dias atrás."""
    from datetime import datetime, timedelta
    limite = date.today() - timedelta(days=DIAS_RETENCAO)
    dados = ws.get_all_values()
    if len(dados) <= 1:
        return  # só cabeçalho, nada a fazer

    # identifica linhas a manter (cabeçalho + dados dentro do período)
    manter = [dados[0]]  # sempre mantém cabeçalho
    removidas = 0
    for linha in dados[1:]:
        try:
            data_linha = date.fromisoformat(linha[0])
            if data_linha >= limite:
                manter.append(linha)
            else:
                removidas += 1
        except:
            manter.append(linha)  # linha com data inválida: mantém

    if removidas > 0:
        print(f"  🗑️  Removendo {removidas} linhas com mais de {DIAS_RETENCAO} dias...")
        ws.clear()
        ws.append_rows(manter, value_input_option="USER_ENTERED")
        print(f"  ✅ Limpeza concluída — {len(manter)-1} linhas mantidas")
    else:
        print(f"  ℹ️  Nenhuma linha para remover (todas dentro de {DIAS_RETENCAO} dias)")

def salvar(ws, linhas):
    if not linhas:
        return
    ws.append_rows(linhas, value_input_option="USER_ENTERED")
    print(f"  📊 {len(linhas)} linhas salvas no Sheets")

def main():
    hoje = date.today().isoformat()
    print(f"\n🚀 Minas Clean — Scraping de Concorrentes — {hoje}")

    client = conectar_sheets()
    sh = client.open_by_key(SHEET_ID)
    ws = garantir_aba(sh)
    limpar_dados_antigos(ws)

    todas_linhas = []

    for termo in TERMOS_BUSCA:
        produtos = rodar_apify(termo)
        for p in produtos:
            linha = [
                hoje,
                termo,
                p.get("shopName", ""),
                p.get("name", "")[:120],
                p.get("price", 0),
                p.get("priceMax") or p.get("price", 0),
                p.get("discountPercent", 0),
                p.get("historicalSoldEstimated", ""),
                p.get("rating", 0),
                p.get("reviewCount", 0),
                p.get("stock", 0),
                "Sim" if p.get("isOnSale") else "Não",
                p.get("url", ""),
            ]
            todas_linhas.append(linha)

    # Coleta da própria loja (Minas Clean) — usa shopUrl para buscar por loja
    print(f"\n  🏪 Coletando minha loja: {MINHA_LOJA_URL}")
    meus_produtos = rodar_apify_loja(MINHA_LOJA_URL)
    for p in meus_produtos:
        linha = [
            hoje,
            MINHA_LOJA_TAG,
            p.get("shopName", "minasclean"),
            p.get("name", "")[:120],
            p.get("price", 0),
            p.get("priceMax") or p.get("price", 0),
            p.get("discountPercent", 0),
            p.get("historicalSoldEstimated", ""),
            p.get("rating", 0),
            p.get("reviewCount", 0),
            p.get("stock", 0),
            "Sim" if p.get("isOnSale") else "Não",
            p.get("url", ""),
        ]
        todas_linhas.append(linha)

    salvar(ws, todas_linhas)
    print(f"\n✅ Concluído — {len(todas_linhas)} produtos de {len(TERMOS_BUSCA)} buscas")
    print(f"   Planilha: https://docs.google.com/spreadsheets/d/{SHEET_ID}")

if __name__ == "__main__":
    main()
