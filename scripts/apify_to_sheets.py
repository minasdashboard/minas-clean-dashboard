"""
shopee_orders_to_sheets.py — Minas Clean
Puxa o TOTAL de pedidos/faturamento da loja (todos os pedidos, com ou sem
anúncio) e salva na aba 'historico_pedidos'. Isso é a base pra calcular:

    Receita orgânica = Faturamento total - Receita atribuída a Ads

⚠️ PRIMEIRA EXECUÇÃO: use sempre o modo diagnóstico primeiro:
    python shopee_orders_to_sheets.py diagnostico
Isso busca só os últimos 2 dias, mostra a lista de pedidos encontrados e o
detalhe de 1 pedido de exemplo — NADA é salvo na planilha. Serve pra:
  1) confirmar que a chamada a get_order_list/get_order_detail funciona com
     os parâmetros que estamos usando (nomes de campo podem mudar);
  2) conferir os nomes de campo antes de rodar de verdade.

Depois de conferir a amostra, rode para valer:
    python shopee_orders_to_sheets.py coletar

Endpoints usados:
  - /api/v2/order/get_order_list   (lista de order_sn no período, paginado)
  - /api/v2/order/get_order_detail (detalhe de até 50 pedidos por chamada —
    aqui pegamos total_amount, order_status, create_time)

Pedidos com status CANCELLED são contados à parte (não entram no GMV).
"""

import os, sys, json, time, hmac, hashlib
from datetime import date, timedelta, datetime, timezone
import requests
import gspread
from google.oauth2.service_account import Credentials

HOST = "https://partner.shopeemobile.com"
CONFIG_PATH = os.environ.get("SHOPEE_CONFIG_FILE", "config.json")
ABA_NOME = "historico_pedidos"
DIAS_RETENCAO = 400
JANELA_COLETA_NORMAL = 10
FUSO_BR = timezone(timedelta(hours=-3))  # BRT, sem horário de verão

# ── CONFIG / CREDENCIAIS GOOGLE (mesmo padrão do shopee_ads_to_sheets.py) ──
def carregar_config():
    if not os.path.isfile(CONFIG_PATH):
        sys.exit(f"❌ Não achei {CONFIG_PATH}. Rode este script na pasta 'scripts'.")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def salvar_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def service_account_file(cfg):
    import tempfile
    _env = os.environ.get("GOOGLE_CREDENTIALS")
    if _env:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        tmp.write(_env)
        tmp.close()
        return tmp.name
    caminho = cfg["google_sheets"]["service_account_json"]
    if not os.path.isabs(caminho):
        caminho = os.path.join(os.path.dirname(os.path.abspath(CONFIG_PATH)), caminho)
    return caminho

def conectar_sheets(cfg):
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(service_account_file(cfg), scopes=scopes)
    return gspread.authorize(creds)

# ── ASSINATURA / TOKEN (mesmo padrão do shopee_ads_to_sheets.py) ──────────
def assinar(partner_id, partner_key, path, timestamp, access_token="", shop_id=""):
    base = f"{partner_id}{path}{timestamp}{access_token}{shop_id}"
    return hmac.new(partner_key.encode(), base.encode(), hashlib.sha256).hexdigest()

def renovar_token(shop, partner_id, partner_key):
    path = "/api/v2/auth/access_token/get"
    ts = int(time.time())
    sign = assinar(partner_id, partner_key, path, ts)
    url = f"{HOST}{path}?partner_id={partner_id}&timestamp={ts}&sign={sign}"
    body = {
        "refresh_token": shop["refresh_token"],
        "partner_id": int(partner_id),
        "shop_id": int(shop["shop_id"]),
    }
    r = requests.post(url, json=body, timeout=30)
    data = r.json()
    if data.get("error"):
        print(f"  ❌ Erro ao renovar token da {shop.get('name','loja')}: {data.get('error')} — {data.get('message')}")
        return None
    return {"access_token": data["access_token"], "refresh_token": data["refresh_token"]}

# ── PEDIDOS ────────────────────────────────────────────────────────────────
def listar_order_sns(shop, partner_id, partner_key, data_inicio, data_fim):
    """Pagina get_order_list e devolve a lista completa de order_sn no período."""
    path = "/api/v2/order/get_order_list"
    access_token = shop["access_token"]
    shop_id = int(shop["shop_id"])
    ts_inicio = int(datetime.combine(data_inicio, datetime.min.time(), tzinfo=FUSO_BR).timestamp())
    ts_fim = int(datetime.combine(data_fim, datetime.max.time(), tzinfo=FUSO_BR).timestamp())

    todos_sn = []
    cursor = ""
    pagina = 1
    while True:
        ts = int(time.time())
        sign = assinar(partner_id, partner_key, path, ts, access_token, shop_id)
        params = {
            "partner_id": partner_id, "timestamp": ts, "access_token": access_token,
            "shop_id": shop_id, "sign": sign,
            "time_range_field": "create_time",
            "time_from": ts_inicio, "time_to": ts_fim,
            "page_size": 100, "cursor": cursor,
        }
        r = requests.get(f"{HOST}{path}", params=params, timeout=30)
        data = r.json()
        if data.get("error"):
            print(f"  ❌ Erro no get_order_list (página {pagina}): {data.get('error')} — {data.get('message')}")
            return todos_sn, data
        resp = data.get("response", {})
        lote = [o["order_sn"] for o in resp.get("order_list", [])]
        todos_sn.extend(lote)
        print(f"  📄 Página {pagina}: {len(lote)} pedidos (total até agora: {len(todos_sn)})")
        if not resp.get("more", False):
            break
        cursor = resp.get("next_cursor", "")
        pagina += 1
        if pagina > 50:  # trava de segurança
            print("  ⚠️ Mais de 50 páginas — interrompendo por segurança.")
            break
    return todos_sn, None

def buscar_detalhes_pedidos(shop, partner_id, partner_key, order_sn_list):
    """Busca detalhe de pedidos em lotes de até 50 por chamada."""
    path = "/api/v2/order/get_order_detail"
    access_token = shop["access_token"]
    shop_id = int(shop["shop_id"])
    todos_pedidos = []
    for i in range(0, len(order_sn_list), 50):
        lote = order_sn_list[i:i+50]
        ts = int(time.time())
        sign = assinar(partner_id, partner_key, path, ts, access_token, shop_id)
        params = {
            "partner_id": partner_id, "timestamp": ts, "access_token": access_token,
            "shop_id": shop_id, "sign": sign,
            "order_sn_list": ",".join(lote),
        }
        r = requests.get(f"{HOST}{path}", params=params, timeout=30)
        data = r.json()
        if data.get("error"):
            print(f"  ❌ Erro no get_order_detail: {data.get('error')} — {data.get('message')}")
            continue
        todos_pedidos.extend(data.get("response", {}).get("order_list", []))
    return todos_pedidos

def agregar_por_dia(pedidos, shop_label):
    """Agrupa pedidos por dia (create_time, fuso BRT), soma GMV e conta pedidos.
    Pedidos CANCELLED entram só na contagem separada, não no GMV."""
    agregados = {}  # data_str -> {gmv, pedidos, cancelados}
    for p in pedidos:
        ts = p.get("create_time")
        if not ts:
            continue
        dia = datetime.fromtimestamp(ts, tz=FUSO_BR).date().isoformat()
        if dia not in agregados:
            agregados[dia] = {"gmv": 0.0, "pedidos": 0, "cancelados": 0}
        status = p.get("order_status", "")
        valor = float(p.get("total_amount", 0) or 0)
        if status == "CANCELLED":
            agregados[dia]["cancelados"] += 1
        else:
            agregados[dia]["gmv"] += valor
            agregados[dia]["pedidos"] += 1
    linhas = []
    for dia, vals in sorted(agregados.items()):
        linhas.append([dia, shop_label, vals["pedidos"], round(vals["gmv"], 2), vals["cancelados"]])
    return linhas

CABECALHO = ["date", "shop", "total_orders", "total_gmv", "cancelled_orders"]

# ── SHEETS ─────────────────────────────────────────────────────────────────
def garantir_aba(sh):
    try:
        ws = sh.worksheet(ABA_NOME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=ABA_NOME, rows=3000, cols=10)
    dados = ws.get_all_values()
    if not dados or dados[0] != CABECALHO:
        ws.clear()
        ws.append_row(CABECALHO)
    return ws

def datas_ja_salvas(ws, shop_label):
    dados = ws.get_all_values()
    if len(dados) <= 1:
        return set()
    return {linha[0] for linha in dados[1:] if linha[1] == shop_label}

def limpar_dados_antigos(ws):
    limite = date.today() - timedelta(days=DIAS_RETENCAO)
    dados = ws.get_all_values()
    if len(dados) <= 1:
        return
    manter = [dados[0]]
    removidas = 0
    for linha in dados[1:]:
        try:
            if date.fromisoformat(linha[0]) >= limite:
                manter.append(linha)
            else:
                removidas += 1
        except Exception:
            manter.append(linha)
    if removidas:
        ws.clear()
        ws.append_rows(manter, value_input_option="USER_ENTERED")
        print(f"  🗑️  {removidas} linhas com mais de {DIAS_RETENCAO} dias removidas")

# ── PRINCIPAL ──────────────────────────────────────────────────────────────
def lojas_validas(cfg):
    validas = []
    for shop in cfg["shops"]:
        tok = shop.get("access_token", "")
        if not tok or tok.startswith("SEU_"):
            print(f"  ⏭️  Pulando {shop.get('name')} — sem token configurado ainda.")
            continue
        validas.append(shop)
    return validas

def rodar(modo):
    cfg = carregar_config()
    lojas = lojas_validas(cfg)
    if not lojas:
        sys.exit("❌ Nenhuma loja com token configurado em config.json.")

    hoje = date.today()
    if modo == "diagnostico":
        data_inicio, data_fim = hoje - timedelta(days=2), hoje
    else:
        data_inicio, data_fim = hoje - timedelta(days=JANELA_COLETA_NORMAL), hoje

    todas_linhas = []

    for shop in lojas:
        partner_id = str(shop["partner_id"])
        partner_key = shop["partner_key"]
        print(f"\n🔑 Renovando token — {shop['name']}...")
        novo = renovar_token(shop, partner_id, partner_key)
        if novo:
            shop["access_token"], shop["refresh_token"] = novo["access_token"], novo["refresh_token"]
            salvar_config(cfg)
            print(f"  ✅ Token renovado e salvo em {CONFIG_PATH}")
        else:
            print(f"  ⚠️  Usando access_token antigo (pode estar expirado) para {shop['name']}")

        print(f"📦 Listando pedidos — {shop['name']} ({data_inicio} → {data_fim})")
        order_sns, erro = listar_order_sns(shop, partner_id, partner_key, data_inicio, data_fim)
        if erro:
            continue
        print(f"  ✅ {len(order_sns)} pedidos encontrados no período")

        if not order_sns:
            continue

        print(f"🔎 Buscando detalhe de {len(order_sns)} pedidos...")
        pedidos = buscar_detalhes_pedidos(shop, partner_id, partner_key, order_sns)

        if modo == "diagnostico":
            print("\n  🔎 AMOSTRA DE 1 PEDIDO BRUTO (confira os campos antes de rodar 'coletar'):")
            if pedidos:
                print(" ", json.dumps(pedidos[0], ensure_ascii=False, indent=2)[:2500])
            else:
                print("  (nenhum pedido retornado pelo get_order_detail)")
            continue

        linhas = agregar_por_dia(pedidos, shop["name"])
        todas_linhas.extend(linhas)
        print(f"  ✅ {len(linhas)} dias agregados para {shop['name']}")

    if modo == "diagnostico":
        print("\n✅ Diagnóstico concluído — nada foi salvo na planilha.")
        print("   Se os campos acima baterem (order_status, total_amount, create_time), rode:")
        print("   python shopee_orders_to_sheets.py coletar")
        return

    if not todas_linhas:
        print("\nℹ️  Nenhuma linha nova para salvar.")
        return

    print(f"\n💾 Conectando ao Google Sheets...")
    client = conectar_sheets(cfg)
    sh = client.open_by_key(cfg["google_sheets"]["spreadsheet_id"])
    ws = garantir_aba(sh)
    limpar_dados_antigos(ws)

    linhas_filtradas = []
    cache_datas = {}
    for linha in todas_linhas:
        shop_label = linha[1]
        if shop_label not in cache_datas:
            cache_datas[shop_label] = datas_ja_salvas(ws, shop_label)
        if linha[0] not in cache_datas[shop_label]:
            linhas_filtradas.append(linha)

    if linhas_filtradas:
        ws.append_rows(linhas_filtradas, value_input_option="USER_ENTERED")
        print(f"  📊 {len(linhas_filtradas)} linhas novas salvas na aba '{ABA_NOME}'")
    else:
        print("  ℹ️  Todas as linhas já existiam na planilha (nada duplicado).")

if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else "diagnostico"
    if modo not in ("diagnostico", "coletar"):
        sys.exit("Uso: python shopee_orders_to_sheets.py [diagnostico|coletar]")
    rodar(modo)
