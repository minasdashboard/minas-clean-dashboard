"""
shopee_ads_to_sheets.py — Minas Clean
Puxa performance de Ads (CPC) da Shopee Open API por loja e salva na aba
'historico_ads' do Google Sheets (fonte da Aba 2 / ROAS do dashboard).

⚠️ PRIMEIRA EXECUÇÃO: use sempre o modo diagnóstico primeiro:
    python shopee_ads_to_sheets.py diagnostico
Isso chama a API para uma janela pequena (últimos 3 dias) e SÓ IMPRIME o JSON
bruto da resposta — nada é salvo na planilha. Serve para:
  1) confirmar que a conta tem permissão de Ads liberada pela Shopee
     (senão vem 'error_permission_denied' — precisa abrir chamado com o
     Shopee Partner Support pedindo acesso à Ads API);
  2) conferir se os nomes de campo batem com o que o código espera abaixo
     (ver mapear_linha()) antes de rodar de verdade.

Depois de conferir a amostra, rode para valer:
    python shopee_ads_to_sheets.py coletar

Endpoint usado: /api/v2/ads/get_all_cpc_ads_daily_performance
(performance diária em nível de LOJA — impressões, cliques, gasto, GMV etc.
 agregados, que é exatamente o granularidade que a Aba 2 usa: 1 linha por
 dia por loja).

Autenticação: lê partner_id/partner_key/shop_id/access_token/refresh_token
de config.json (mesmo arquivo usado pelo shopee_auth_setup.py). Sempre que
roda, tenta renovar o access_token via refresh_token antes de chamar a API
(o access_token expira em ~4h; o refresh_token dura mais e é atualizado de
volta no config.json a cada renovação).
"""

import os, sys, json, time, hmac, hashlib, tempfile
from datetime import date, timedelta
import requests
import gspread
from google.oauth2.service_account import Credentials

# ── CONFIGURAÇÃO ──────────────────────────────────────────────
HOST = "https://partner.shopeemobile.com"
CONFIG_PATH = os.environ.get("SHOPEE_CONFIG_FILE", "config.json")
ABA_NOME = "historico_ads"
DIAS_RETENCAO = 400  # ~13 meses de histórico de ads

# quantos dias voltar na primeira coleta "cheia" (catch-up). Depois disso o
# script só busca os últimos N dias a cada execução (evita reprocessar tudo).
JANELA_COLETA_NORMAL = 10

# ── CONFIG / CREDENCIAIS GOOGLE ───────────────────────────────
def carregar_config():
    if not os.path.isfile(CONFIG_PATH):
        sys.exit(f"❌ Não achei {CONFIG_PATH}. Rode este script na pasta 'scripts'.")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def salvar_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def service_account_file(cfg):
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

# ── ASSINATURA / CHAMADAS SHOPEE ──────────────────────────────
def assinar(partner_id, partner_key, path, timestamp, access_token="", shop_id=""):
    base = f"{partner_id}{path}{timestamp}{access_token}{shop_id}"
    return hmac.new(partner_key.encode(), base.encode(), hashlib.sha256).hexdigest()

def renovar_token(shop, partner_id, partner_key):
    """Troca o refresh_token por um access_token novo. Retorna dict atualizado ou None se falhar."""
    path = "/api/v2/auth/access_token/get"
    ts = int(time.time())
    sign = assinar(partner_id, partner_key, path, ts)  # rota pública: sem access_token/shop_id no sign
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
    return {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
    }

def chamar_ads_performance(shop, partner_id, partner_key, data_inicio, data_fim):
    """Chama get_all_cpc_ads_daily_performance para uma loja. Retorna o JSON bruto da resposta."""
    path = "/api/v2/ads/get_all_cpc_ads_daily_performance"
    ts = int(time.time())
    access_token = shop["access_token"]
    shop_id = int(shop["shop_id"])
    sign = assinar(partner_id, partner_key, path, ts, access_token, shop_id)
    params = {
        "partner_id": partner_id,
        "timestamp": ts,
        "access_token": access_token,
        "shop_id": shop_id,
        "sign": sign,
        "start_date": data_inicio.strftime("%d-%m-%Y"),  # Shopee exige DD-MM-AAAA
        "end_date": data_fim.strftime("%d-%m-%Y"),
    }
    r = requests.get(f"{HOST}{path}", params=params, timeout=30)
    try:
        return r.json()
    except Exception:
        print(f"  ⚠️ Resposta não-JSON (status {r.status_code}): {r.text[:500]}")
        return None

def normalizar_data(bruta):
    """Converte a data devolvida pela Shopee (formato incerto até confirmar na
    amostra: pode vir DD-MM-AAAA, AAAAMMDD ou já AAAA-MM-DD) para AAAA-MM-DD,
    que é o formato que a planilha/dashboard esperam para ordenar certo."""
    bruta = bruta.strip()
    if len(bruta) == 8 and bruta.isdigit():  # AAAAMMDD
        return f"{bruta[0:4]}-{bruta[4:6]}-{bruta[6:8]}"
    if "-" in bruta:
        partes = bruta.split("-")
        if len(partes[0]) == 4:  # já é AAAA-MM-DD
            return bruta
        if len(partes[-1]) == 4:  # DD-MM-AAAA
            d, m, a = partes
            return f"{a}-{m.zfill(2)}-{d.zfill(2)}"
    return bruta  # formato desconhecido — mantém como veio pra não perder o dado

# ── NORMALIZAÇÃO ──────────────────────────────────────────────
def mapear_linha(shop_label, dia_str, item):
    """
    Converte 1 item do JSON de resposta da Shopee em 1 linha para a planilha.
    ⚠️ Os nomes de campo abaixo são a MELHOR HIPÓTESE baseada na documentação
    pública da Shopee Ads (impression, clicks, expense/cost, gmv, orders/
    conversion). CONFIRME contra a amostra impressa no modo diagnóstico antes
    de rodar 'coletar' — se os nomes não baterem, ajuste as chaves em
    pega(...) abaixo em vez de mudar a estrutura da planilha.
    """
    def pega(*chaves, default=0):
        for k in chaves:
            if k in item and item[k] is not None:
                return item[k]
        return default

    impressions = float(pega("impression", "impressions"))
    clicks      = float(pega("clicks", "click"))
    cost        = float(pega("expense", "cost")) 
    gmv         = float(pega("broad_gmv", "direct_gmv", "gmv"))
    orders      = float(pega("broad_order", "direct_order", "order", "orders"))

    ctr_pct = (clicks / impressions * 100) if impressions > 0 else 0
    cpc = (cost / clicks) if clicks > 0 else 0
    roas = (gmv / cost) if cost > 0 else 0
    conversion_rate = (orders / clicks * 100) if clicks > 0 else 0

    return [
        dia_str, shop_label,
        round(impressions), round(clicks), round(ctr_pct, 2),
        round(cost, 2), round(gmv, 2), round(roas, 2),
        round(cpc, 2), round(orders), round(conversion_rate, 2),
    ]

CABECALHO = [
    "date", "shop", "impressions", "clicks", "ctr_pct",
    "cost", "gmv", "roas", "cpc", "orders", "conversion_rate",
]

# ── SHEETS ─────────────────────────────────────────────────────
def garantir_aba(sh):
    try:
        ws = sh.worksheet(ABA_NOME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=ABA_NOME, rows=3000, cols=15)
    dados = ws.get_all_values()
    if not dados or dados[0] != CABECALHO:
        ws.clear()
        ws.append_row(CABECALHO)
    return ws

def datas_ja_salvas(ws, shop_label):
    dados = ws.get_all_values()
    if len(dados) <= 1:
        return set()
    idx_date = CABECALHO.index("date")
    idx_shop = CABECALHO.index("shop")
    return {linha[idx_date] for linha in dados[1:] if linha[idx_shop] == shop_label}

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

# ── PRINCIPAL ──────────────────────────────────────────────────
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
        data_inicio = hoje - timedelta(days=3)
        data_fim = hoje
    else:
        data_inicio = hoje - timedelta(days=JANELA_COLETA_NORMAL)
        data_fim = hoje

    linhas_para_salvar = []

    for shop in lojas:
        partner_id = str(shop["partner_id"])
        partner_key = shop["partner_key"]
        print(f"\n🔑 Renovando token — {shop['name']}...")
        novo = renovar_token(shop, partner_id, partner_key)
        if not novo:
            print(f"  ⚠️  Usando access_token antigo (pode estar expirado) para {shop['name']}")
        else:
            shop["access_token"] = novo["access_token"]
            shop["refresh_token"] = novo["refresh_token"]
            salvar_config(cfg)
            print(f"  ✅ Token renovado e salvo em {CONFIG_PATH}")

        print(f"📈 Buscando performance de Ads — {shop['name']} ({data_inicio} → {data_fim})")
        resp = chamar_ads_performance(shop, partner_id, partner_key, data_inicio, data_fim)

        if resp is None:
            continue

        if modo == "diagnostico":
            print("\n  🔎 RESPOSTA BRUTA DA SHOPEE (confira os campos antes de rodar 'coletar'):")
            print(" ", json.dumps(resp, ensure_ascii=False, indent=2)[:3000])
            continue

        if resp.get("error"):
            print(f"  ❌ Erro da API: {resp.get('error')} — {resp.get('message')}")
            if resp.get("error") == "error_permission_denied":
                print("     → Sua conta ainda não tem a Ads API liberada. Abra um chamado")
                print("       no Shopee Partner Support pedindo acesso ao módulo de Ads.")
            continue

        resposta_bruta = resp.get("response")
        if isinstance(resposta_bruta, list):
            itens = resposta_bruta
        elif isinstance(resposta_bruta, dict):
            itens = resposta_bruta.get("daily_performance") or resposta_bruta.get("list") or []
        else:
            itens = []
        if not isinstance(itens, list):
            print(f"  ⚠️  Formato de resposta inesperado, veja acima e ajuste mapear_linha(). Bruto: "
                  f"{json.dumps(resp, ensure_ascii=False)[:500]}")
            continue

        for item in itens:
            dia_bruto = str(item.get("date") or item.get("day") or "")
            dia_str = normalizar_data(dia_bruto)
            linha = mapear_linha(shop["name"], dia_str, item)
            linhas_para_salvar.append(linha)

        print(f"  ✅ {len(itens)} dias de performance coletados para {shop['name']}")

    if modo == "diagnostico":
        print("\n✅ Diagnóstico concluído — nada foi salvo na planilha.")
        print("   Se os campos acima baterem com CABECALHO/mapear_linha(), rode:")
        print("   python shopee_ads_to_sheets.py coletar")
        return

    if not linhas_para_salvar:
        print("\nℹ️  Nenhuma linha nova para salvar.")
        return

    print(f"\n💾 Conectando ao Google Sheets...")
    client = conectar_sheets(cfg)
    sh = client.open_by_key(cfg["google_sheets"]["spreadsheet_id"])
    ws = garantir_aba(sh)
    limpar_dados_antigos(ws)

    # evita duplicar linhas (mesma data+loja já salva)
    linhas_filtradas = []
    cache_datas = {}
    for linha in linhas_para_salvar:
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
        sys.exit("Uso: python shopee_ads_to_sheets.py [diagnostico|coletar]")
    rodar(modo)
