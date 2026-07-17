"""
shopee_wallet_to_sheets.py — Minas Clean
Puxa as TRANSAÇÕES REAIS DA CARTEIRA Shopee (Wallet) — ou seja, o dinheiro
que efetivamente entra e sai da sua conta Shopee, incluindo a liberação do
escrow (quando a Shopee libera o valor de um pedido pra você, depois do
prazo de garantia/devolução).

Isso é DIFERENTE dos dois GMVs do shopee_orders_to_sheets.py:
  - gmv_tradicional / gmv_pago (naquele script) = o que o COMPRADOR fez
    (pediu, pagou), na data em que ele fez.
  - Este script (fluxo_caixa_real) = o que a SHOPEE libera pra VOCÊ,
    na data em que o dinheiro fica disponível na sua carteira/conta.
Para prever fluxo de caixa de verdade (quanto dinheiro você vai ter
disponível em cada data futura), este script é a fonte mais confiável.

✅ CAMPOS VALIDADOS EM 07/07/2026 com dados reais da Loja 1:
create_time, amount, transaction_type, order_sn, status e money_flow vieram
exatamente como esperado. money_flow ("MONEY_IN"/"MONEY_OUT") é usado como
fonte principal pra classificar entrada/saída (mais confiável que o sinal
do valor). Rode diagnostico mesmo assim antes do coletar, como boa prática
— principalmente pra Loja 2 quando a autorização dela estiver pronta,
já que shops diferentes podem ter transaction_type distintos.

Uso:
    python shopee_wallet_to_sheets.py diagnostico
    python shopee_wallet_to_sheets.py coletar

Endpoint usado:
  - /api/v2/payment/get_wallet_transaction_list (paginado)
"""

import os, sys, json, time, hmac, hashlib
from datetime import date, timedelta, datetime, timezone
import requests
import gspread
from google.oauth2.service_account import Credentials

HOST = "https://partner.shopeemobile.com"
CONFIG_PATH = os.environ.get("SHOPEE_CONFIG_FILE", "config.json")
ABA_NOME = "fluxo_caixa_real"
DIAS_RETENCAO = 400
JANELA_COLETA_NORMAL = 30  # transações de escrow podem levar semanas pra aparecer, janela maior que a de pedidos
FUSO_BR = timezone(timedelta(hours=-3))  # BRT, sem horário de verão

# ── CONFIG / CREDENCIAIS GOOGLE (mesmo padrão dos outros scripts) ─────────
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

# ── ASSINATURA / TOKEN (mesmo padrão dos outros scripts) ──────────────────
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

# ── TRANSAÇÕES DA CARTEIRA ─────────────────────────────────────────────────
def listar_transacoes_em_blocos(shop, partner_id, partner_key, data_inicio, data_fim, tamanho_bloco_dias=7):
    """A Shopee rejeita períodos muito longos numa única chamada
    (erro confirmado: 'wallet.time_invalid — time period too large').
    Quebra o intervalo total em blocos de N dias e chama listar_transacoes
    pra cada bloco, juntando os resultados."""
    todas = []
    cursor = data_inicio
    while cursor <= data_fim:
        fim_bloco = min(cursor + timedelta(days=tamanho_bloco_dias - 1), data_fim)
        print(f"  🗓️  Bloco {cursor} → {fim_bloco}")
        transacoes, erro = listar_transacoes(shop, partner_id, partner_key, cursor, fim_bloco)
        if erro:
            return todas, erro
        todas.extend(transacoes)
        cursor = fim_bloco + timedelta(days=1)
    return todas, None

def listar_transacoes(shop, partner_id, partner_key, data_inicio, data_fim):
    """Pagina get_wallet_transaction_list e devolve a lista completa de
    transações no período.
    ⚠️ Nomes de parâmetro (create_time_from/create_time_to, page_no,
    page_size) são um "melhor palpite" baseado no padrão da API v2 —
    confirme no diagnóstico se a paginação está funcionando como esperado
    (se 'more'/'next' não existir na resposta, ajuste aqui)."""
    path = "/api/v2/payment/get_wallet_transaction_list"
    access_token = shop["access_token"]
    shop_id = int(shop["shop_id"])
    ts_inicio = int(datetime.combine(data_inicio, datetime.min.time(), tzinfo=FUSO_BR).timestamp())
    ts_fim = int(datetime.combine(data_fim, datetime.max.time(), tzinfo=FUSO_BR).timestamp())

    todas = []
    page_no = 1
    page_size = 100
    while True:
        ts = int(time.time())
        sign = assinar(partner_id, partner_key, path, ts, access_token, shop_id)
        params = {
            "partner_id": partner_id, "timestamp": ts, "access_token": access_token,
            "shop_id": shop_id, "sign": sign,
            "create_time_from": ts_inicio, "create_time_to": ts_fim,
            "page_no": page_no, "page_size": page_size,
        }
        r = requests.get(f"{HOST}{path}", params=params, timeout=30)
        data = r.json()
        if data.get("error"):
            print(f"  ❌ Erro no get_wallet_transaction_list (página {page_no}): {data.get('error')} — {data.get('message')}")
            return todas, data
        resp = data.get("response", {})
        lote = resp.get("transaction_list", [])
        todas.extend(lote)
        print(f"  📄 Página {page_no}: {len(lote)} transações (total até agora: {len(todas)})")
        tem_mais = resp.get("more", False) or resp.get("has_next_page", False)
        if not tem_mais or len(lote) < page_size:
            break
        page_no += 1
        if page_no > 50:  # trava de segurança
            print("  ⚠️ Mais de 50 páginas — interrompendo por segurança.")
            break
    return todas, None

def extrair_campos_transacao(t):
    """Campos confirmados via diagnóstico com dados reais (07/07/2026):
    create_time, amount, transaction_type, order_sn e status batem com o
    esperado. money_flow ("MONEY_IN"/"MONEY_OUT") é mais confiável que o
    sinal de amount pra classificar entrada/saída — usamos ele primeiro."""
    ts = t.get("create_time")
    valor = t.get("amount", 0) or 0
    tipo = t.get("transaction_type", "")
    status = t.get("status", "")
    order_sn = t.get("order_sn", "")
    money_flow = t.get("money_flow", "")
    descricao = t.get("description", "")
    return {
        "timestamp": ts,
        "valor": float(valor),
        "tipo": tipo,
        "status": status,
        "order_sn": order_sn,
        "money_flow": money_flow,
        "descricao": descricao,
    }

def eh_entrada(campos):
    """Usa money_flow quando disponível (mais confiável); cai pro sinal do
    valor só se money_flow vier vazio."""
    if campos["money_flow"] == "MONEY_IN":
        return True
    if campos["money_flow"] == "MONEY_OUT":
        return False
    return campos["valor"] >= 0

def agregar_por_dia(transacoes, shop_label):
    """Agrupa transações por dia (fuso BRT).
    - entradas: soma de valores classificados como MONEY_IN (dinheiro que
      entrou na carteira — inclui liberação de escrow)
    - saidas: soma de valores classificados como MONEY_OUT, em módulo
      (taxas, reembolsos, ajustes, saques)
    - saldo_dia: entradas - saidas
    Só considera transações com status COMPLETED — pendentes/em processamento
    ainda podem mudar e entram na próxima coleta quando finalizarem."""
    agregados = {}  # dia -> {entradas, saidas, qtd_transacoes}
    for t in transacoes:
        campos = extrair_campos_transacao(t)
        if not campos["timestamp"]:
            continue
        if campos["status"] and campos["status"] != "COMPLETED":
            continue
        dia = datetime.fromtimestamp(campos["timestamp"], tz=FUSO_BR).date().isoformat()
        if dia not in agregados:
            agregados[dia] = {"entradas": 0.0, "saidas": 0.0, "qtd": 0}
        valor = abs(campos["valor"])
        if eh_entrada(campos):
            agregados[dia]["entradas"] += valor
        else:
            agregados[dia]["saidas"] += valor
        agregados[dia]["qtd"] += 1
    linhas = []
    for dia, vals in sorted(agregados.items()):
        saldo = vals["entradas"] - vals["saidas"]
        linhas.append([
            dia, shop_label, vals["qtd"],
            round(vals["entradas"], 2), round(vals["saidas"], 2), round(saldo, 2),
        ])
    return linhas

CABECALHO = ["date", "shop", "qtd_transacoes", "entradas", "saidas", "saldo_dia", "atualizado_em"]

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

def upsert_linhas(ws, linhas):
    """Atualiza a linha se (date, shop) já existir na planilha; senão, cria nova."""
    dados = ws.get_all_values()
    idx_map = {}
    for i, linha in enumerate(dados[1:], start=2):
        if len(linha) >= 2:
            idx_map[(linha[0], linha[1])] = i

    agora = datetime.now(FUSO_BR).strftime("%Y-%m-%d %H:%M:%S")
    atualizacoes = []
    novas = []
    for linha in linhas:
        linha_completa = linha + [agora]
        chave = (linha[0], linha[1])
        if chave in idx_map:
            atualizacoes.append((idx_map[chave], linha_completa))
        else:
            novas.append(linha_completa)

    for row_num, linha in atualizacoes:
        ws.update(f"A{row_num}", [linha], value_input_option="USER_ENTERED")
    if novas:
        ws.append_rows(novas, value_input_option="USER_ENTERED")

    return len(atualizacoes), len(novas)

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
        data_inicio, data_fim = hoje - timedelta(days=7), hoje
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

        print(f"💰 Listando transações da carteira — {shop['name']} ({data_inicio} → {data_fim})")
        transacoes, erro = listar_transacoes_em_blocos(shop, partner_id, partner_key, data_inicio, data_fim)
        if erro:
            continue
        print(f"  ✅ {len(transacoes)} transações encontradas no período")

        if modo == "diagnostico":
            print("\n  🔎 AMOSTRA DE TRANSAÇÕES BRUTAS (confira os campos antes de rodar 'coletar'):")
            if transacoes:
                for t in transacoes[:3]:
                    print(json.dumps(t, ensure_ascii=False, indent=2)[:1500])
                    print("  ---")
                print("\n  🔎 Campos que o script conseguiu identificar na 1ª transação:")
                print(" ", json.dumps(extrair_campos_transacao(transacoes[0]), ensure_ascii=False, indent=2, default=str))
            else:
                print("  (nenhuma transação retornada — pode ser normal se não houve movimentação no período,"
                      " ou pode ser erro de parâmetro. Confira a mensagem de erro acima, se houver.)")
            continue

        if not transacoes:
            continue

        linhas = agregar_por_dia(transacoes, shop["name"])
        todas_linhas.extend(linhas)
        print(f"  ✅ {len(linhas)} dias agregados para {shop['name']}")

    if modo == "diagnostico":
        print("\n✅ Diagnóstico concluído — nada foi salvo na planilha.")
        print("   Copie e cole esta saída de volta pro Claude conferir os nomes de campo antes de rodar:")
        print("   python shopee_wallet_to_sheets.py coletar")
        return

    if not todas_linhas:
        print("\nℹ️  Nenhuma linha nova para salvar.")
        return

    print(f"\n💾 Conectando ao Google Sheets...")
    client = conectar_sheets(cfg)
    sh = client.open_by_key(cfg["google_sheets"]["spreadsheet_id"])
    ws = garantir_aba(sh)
    limpar_dados_antigos(ws)

    atualizadas, novas = upsert_linhas(ws, todas_linhas)
    print(f"  📊 {atualizadas} linhas atualizadas, {novas} linhas novas salvas na aba '{ABA_NOME}'")

if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else "diagnostico"
    if modo not in ("diagnostico", "coletar"):
        sys.exit("Uso: python shopee_wallet_to_sheets.py [diagnostico|coletar]")
    rodar(modo)
