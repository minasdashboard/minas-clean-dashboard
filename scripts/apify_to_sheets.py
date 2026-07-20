"""
apify_to_sheets.py — Minas Clean
Coleta dados de concorrentes via Apify e salva na aba 'concorrentes' do Google Sheets.

⚠️ 01/07/2026 — DESCOBERTO: o actor gio21/shopee-scraper devolvia DADOS MOCK/FAKE
(campo _notice = "THIS IS MOCK / SAMPLE DATA — not real Shopee products").
Trocado para xtracto/shopee-scraper — TESTADO manualmente no Apify Console
com dado real em 2026-07-01/02:
  - mode="keyword" ("pano microfibra 35x35"): 5 produtos reais, nomes/preços/
    URLs batendo com anúncios reais da Shopee Brasil.
  - mode="shop" (loja "minasclean"): 5 produtos reais, shop_id=1781178701
    batendo com o Shop ID oficial já validado via API da Shopee.
Preço vem em CENTAVOS (ex: 2790 = R$ 27,90) — normalizar_item() já converte.
⚠️ PENDENTE: mode="shop" só devolveu 5 produtos mesmo pedindo 40 — pode não
respeitar maxProducts. Conferir no primeiro run de produção se cobre os 24
SKUs ou se falta ajustar (ver nota em rodar_apify_loja()).
Não tem campo de nome da loja em texto — shopName fica como "Loja #{id}".
Também foi adicionada detectar_dados_mock(), que aborta a coleta (sem salvar
nada) se o actor devolver qualquer sinal de dado fake de novo — proteção
permanente, independente de qual actor estiver configurado.

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

# Testado com dado real em 2026-07-01 (ver nota acima). Não precisa de cookie.
ACTOR_ID = "xtracto~shopee-scraper"

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

# URL da sua loja — mantido só como referência/logging
MINHA_LOJA_URL  = "https://shopee.com.br/minasclean"
MINHA_LOJA2_URL = "https://shopee.com.br/maximahome"
MINHA_LOJA_TAG = "minha_loja"

# ⚠️ 02/07/2026 — mode="shop" do actor xtracto/shopee-scraper está CAPADO em
# ~5-6 produtos, não importa o valor de maxProducts (testado manualmente,
# confirmado no log/JSON de 3 runs diferentes). Não dá pra confiar nele pra
# pegar os 24 SKUs. NOVA ESTRATÉGIA: em vez de uma chamada separada por loja,
# os shop_id abaixo (confirmados navegando manualmente até um produto de
# cada loja) são usados pra RECONHECER seus próprios produtos dentro dos
# resultados normais de busca por palavra-chave (que não tem esse limite).
# rodar_apify_loja() fica no código só de referência, não é mais chamada.
MINHA_LOJA_SHOPID  = "1781178701"  # minasclean — confirmado batendo com API oficial da Shopee
MINHA_LOJA2_SHOPID = "1810599865"  # maximahome — confirmado navegando manualmente até um produto

MAX_ITENS = 13  # por termo — ajustado em 02/07/2026 pra caber em ~$29/mês
# rodando 2x/semana no plano Starter da Apify ($30/1.000 resultados do
# actor xtracto/shopee-scraper: 8 termos × 13 × $0,03 × ~8,7 rodadas/mês
# ≈ $27/mês, com margem). Se aumentar a frequência ou os termos de busca,
# recalcular: (orçamento mensal ÷ rodadas/mês) ÷ $0,03 ÷ nº de termos.

# Você tem até 24 SKUs (variações de tamanho x kit) espalhados entre as duas
# lojas. 20 poderia deixar SKU de fora numa loja com catálogo cheio — usamos
# uma margem maior aqui pra garantir que a coleta pega TODOS os seus produtos,
# não só os mais vendidos/melhor rankeados.
MAX_ITENS_LOJA = 40  # não usado mais (ver nota acima) — mantido só de referência
# ─────────────────────────────────────────────────────────────

def detectar_dados_mock(items):
    """Verifica se o actor devolveu dados FAKE em vez de dados reais da Shopee.
    Alguns actors da Apify devolvem 'sample/mock data' silenciosamente quando
    o usuário está em plano gratuito ou quando a raspagem real falha — sem dar
    erro, só preenchendo os campos com dado inventado (foi o que aconteceu
    com o gio21/shopee-scraper em 01/07/2026: campo _notice = 'THIS IS MOCK /
    SAMPLE DATA — not real Shopee products').
    Se detectarmos qualquer sinal disso, a coleta é abortada SEM salvar nada."""
    if not items:
        return False
    sinais_mock = [
        "mock", "sample data", "not real", "fake data",
        "this is a demo", "test data only", "_notice",
    ]
    for item in items[:5]:  # checa uma amostra dos primeiros itens
        texto = json.dumps(item, ensure_ascii=False).lower()
        if any(s in texto for s in sinais_mock):
            return True
    return False

def _extrair_nome_real(valor):
    """⚠️ 20/07/2026 — CONFIRMADO: com fetchDetail=True (ativado em 06/07),
    o campo 'name' às vezes vem como um objeto aninhado tipo
    {"shopid": 372044688, "name": "título real do produto"} em vez de texto
    simples — visto na coleta de 20/07/2026 (189 produtos com esse formato
    quebrado na planilha, coluna 'produto'). Detecta esse formato (tanto
    como dict de verdade quanto como string de JSON) e extrai o nome real
    de dentro, em vez de salvar o objeto bruto."""
    if isinstance(valor, dict):
        return valor.get("name") or ""
    if isinstance(valor, str) and valor.strip().startswith("{") and '"name"' in valor:
        try:
            parsed = json.loads(valor)
            if isinstance(parsed, dict) and parsed.get("name"):
                return parsed["name"]
        except Exception:
            pass
    return valor if isinstance(valor, str) else ""

def normalizar_item(raw):
    """Normaliza um item bruto do actor pra um formato único.
    Suporta 3 formatos:
      (a) xtracto/shopee-scraper — CONFIRMADO com dado real em 2026-07-01,
          e RECONFIRMADO em 2026-07-20 com uma mudança importante: o ator
          passou a devolver o título do produto no campo 'title' (não mais
          'name' como antes). Campos flat: item_id, shop_id, title, price,
          price_before_discount, discount_pct, rating_star, historical_sold,
          sold, url/images. Preço vem em CENTAVOS (ex: 2287 = R$ 22,87).
          Também vimos aqui price/price_min/price_max — quando os três são
          iguais, o item não tem variação de tamanho/kit; ainda não
          confirmamos como vem quando há variação (has_model_with_available_
          shopee_stock=true) — precisa de uma amostra real desse caso.
      (b) formato 'item_basic' aninhado (API interna genérica da Shopee,
          preço em microunidades: R$ real = price / 100000) — fallback.
      (c) formato 'flat' antigo do gio21 (mock, mantido só por segurança) —
          fallback final.
    Se algum campo vier vazio/zero de forma consistente, é sinal de que o
    nome do campo mudou — conferir com 1 item bruto real de novo."""
    if "item_id" in raw or "shop_id" in raw:
        # (a) Formato confirmado do xtracto/shopee-scraper
        preco = raw.get("price")
        preco_orig = raw.get("price_before_discount") or raw.get("original_price")
        preco_max_raw = raw.get("price_max")
        # 20/07/2026 — CONFIRMADO: existem os campos 'models' e 'tier_variations'
        # (a Shopee usa "models" internamente pra representar variações de
        # tamanho/cor/kit dentro de um mesmo anúncio, cada uma com preço
        # próprio). Quando o anúncio tem variações, 'price' costuma ser o
        # MENOR preço entre TODAS as variações — que pode ser de um tamanho
        # bem diferente do que o título principal sugere (ex.: anúncio de
        # "60x80" cujo preço mínimo, R$0,28, na real é de uma variação
        # "35x60" dentro do mesmo anúncio). Isso gera comparação de preço
        # errada por tamanho. Marcamos aqui pra o dashboard tratar esses
        # casos com cautela (não comparar preço mínimo cegamente).
        models = raw.get("models") or raw.get("tier_variations") or []
        tem_variacoes = bool(raw.get("has_model_with_available_shopee_stock")) or bool(models)
        preco_num = (preco / 100) if preco not in (None, "") else 0
        preco_max_num = (preco_max_raw / 100) if preco_max_raw not in (None, "") else preco_num
        # Faixa de preço muito ampla (>40% de diferença) é outro sinal forte
        # de variações de tamanho diferentes dentro do mesmo anúncio, mesmo
        # quando o actor não marcou has_model_with_available_shopee_stock.
        if preco_num > 0 and preco_max_num > 0 and (preco_max_num / preco_num) > 1.4:
            tem_variacoes = True
        return {
            "name": _extrair_nome_real(raw.get("title") or raw.get("name")) or "",
            "price": preco_num,
            "priceMax": preco_max_num,
            "originalPrice": (preco_orig / 100) if preco_orig not in (None, "") else None,
            "discountPercent": raw.get("discount_pct") or 0,
            "isOnSale": bool(raw.get("discount_pct")),
            "historicalSoldEstimated": raw.get("historical_sold") or raw.get("sold") or raw.get("sold_count") or "",
            "rating": raw.get("rating_star") or raw.get("rating") or 0,
            "reviewCount": raw.get("rating_count") or raw.get("total_ratings") or 0,
            "stock": raw.get("stock") or 0,
            "shopName": _extrair_nome_real(raw.get("shop_name") or raw.get("shop")) or (f"Loja #{raw.get('shop_id')}" if raw.get("shop_id") else ""),
            "itemid": raw.get("item_id"),
            "shopid": raw.get("shop_id"),
            "url": raw.get("url") or "",
            "temVariacoes": tem_variacoes,
        }


    base = raw.get("item_basic") if isinstance(raw.get("item_basic"), dict) else raw

    def pega(*chaves, default=None):
        for k in chaves:
            if k in base and base[k] not in (None, ""):
                return base[k]
        return default

    preco_bruto = pega("price", "priceMin")
    preco_original_bruto = pega("price_before_discount", "originalPrice")
    # Formato item_basic da Shopee vem em microunidades (ex: 850000 = R$ 8,50 x 100000)
    eh_microunidade = isinstance(preco_bruto, (int, float)) and preco_bruto and preco_bruto > 100000
    def conv(v):
        if v in (None, ""): return None
        return v / 100000 if eh_microunidade else v

    return {
        "name":  _extrair_nome_real(pega("name", "title", default="")),
        "price": conv(preco_bruto) or 0,
        "priceMax": conv(pega("price_max", "priceMax")),
        "originalPrice": conv(preco_original_bruto),
        "discountPercent": pega("discount", "discountPercent", "raw_discount", default=0),
        "isOnSale": bool(pega("discount", "discountPercent", "isOnSale", default=0)),
        "historicalSoldEstimated": pega("historical_sold", "historicalSoldEstimated", "sold", default=""),
        "rating": pega("item_rating", "rating", default=0) if not isinstance(pega("item_rating"), dict) else (pega("item_rating") or {}).get("rating_star", 0),
        "reviewCount": pega("cmt_count", "reviewCount", default=0),
        "stock": pega("stock", default=0),
        "shopName": _extrair_nome_real(pega("shop_name", "shopName", default="")),
        "itemid": pega("itemid", "item_id"),
        "shopid": pega("shopid", "shop_id"),
        "url": pega("url"),  # se o actor não devolver url pronta, ver construir_url_shopee()
    }

def construir_url_shopee(item):
    """Monta a URL do produto no padrão da Shopee (i.{shopid}.{itemid})
    quando o actor não devolve a URL pronta, mas devolve itemid/shopid."""
    if item.get("url"):
        return item["url"]
    if item.get("itemid") and item.get("shopid"):
        nome_slug = (item.get("name") or "produto").lower()
        nome_slug = "".join(c if c.isalnum() else "-" for c in nome_slug)
        nome_slug = "-".join(filter(None, nome_slug.split("-")))[:100]
        return f"https://shopee.com.br/{nome_slug}-i.{item['shopid']}.{item['itemid']}"
    return ""

def corrigir_offset_nomes(items):
    """⚠️ 06/07/2026 — CONFIRMADO (comparando itens reais de uma run de produção
    do actor xtracto/shopee-scraper, termo 'pano microfibra 35x35', 2026-07-02):
    o campo 'name' vem DESLOCADO 1 POSIÇÃO PRA FRENTE em relação a item_id/
    shop_id/price/url dentro do lote — ou seja, o nome que aparece na posição N
    do lote bruto pertence de verdade ao item da posição N+1.
    Confirmado abrindo a página real da Shopee: o item na posição 10 (shop_id
    860090337, preço R$11,60 — batendo com a página real) tinha, no lote bruto,
    o NOME do item da posição 9 ("Kit com 10 - Pano Microfibra Flanela - 30 x
    30cm..." — que é o título real do produto da posição 10, não da posição 9).
    Esse bug já tinha sido visto e corrigido com o actor anterior (gio21), mas
    a correção não existia para o formato do xtracto/shopee-scraper (campos
    item_id/shop_id/name flat) — por isso passou despercebido até agora.
    Aqui corrigimos deslocando 'name' pra frente: o nome correto do item i é o
    'name' que veio na posição i-1 do lote bruto. O PRIMEIRO item do lote perde
    o nome com confiabilidade (não temos o anterior pra confirmar) — nesse caso
    preferimos marcar como vazio a manter um nome errado, já que um nome errado
    causa classificação errada de tamanho/kit no dashboard, o que é pior do que
    faltar o dado."""
    if not items or len(items) < 2:
        return items
    # só faz sentido corrigir no formato flat (item_id/shop_id), que é o formato
    # onde o bug foi confirmado — não mexe em outros formatos por segurança
    if not all(("item_id" in it or "shop_id" in it) for it in items[:3]):
        return items

    corrigidos = []
    for i, it in enumerate(items):
        novo = dict(it)
        if i - 1 >= 0:
            novo["name"] = _extrair_nome_real(items[i - 1].get("name", ""))
        else:
            novo["name"] = ""  # primeiro item do lote: sem anterior pra confirmar
        corrigidos.append(novo)
    return corrigidos

def achatar(v):
    """Garante que o valor é um tipo simples (texto/número) antes de ir pro
    Sheets. Com fetchDetail=True, alguns campos passaram a vir como lista ou
    dicionário em vez de texto/número simples (ex.: sold_count com metadados
    aninhados) — o Google Sheets rejeita a linha INTEIRA quando isso acontece,
    então precisamos achatar qualquer valor complexo pra string antes."""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)[:200]  # corta pra não estourar limite de célula
    return v

def rodar_apify(termo):
    """Dispara o Actor e aguarda conclusão. Retorna lista de produtos."""
    print(f"\n  🔍 Buscando: '{termo}'")

    # Inicia o run
    url = f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs?token={APIFY_TOKEN}"
    payload = {
        "mode": "keyword",
        "keyword": termo,
        "country": "br",
        "maxProducts": MAX_ITENS,
        "sort": "relevancy",
        "fetchDetail": True,  # 06/07/2026: ativado pra capturar sold_count (vendas
                              # acumuladas) — sem isso o campo vem sempre nulo, pois
                              # a página de busca não inclui esse dado, só a página
                              # individual do produto. Deixa a run mais lenta/cara
                              # (visita cada produto), mas é necessário pro recurso
                              # de estimativa de vendas por período.
    }
    resp = requests.post(url, json=payload, timeout=30)
    if not resp.ok:
        print(f"  ❌ Apify recusou iniciar o run (HTTP {resp.status_code}):")
        print(f"     {resp.text[:500]}")
    resp.raise_for_status()
    run = resp.json()["data"]
    run_id = run["id"]
    print(f"  Run ID: {run_id} — aguardando...")

    # Aguarda conclusão (máx 8 min — com fetchDetail=True a run visita cada
    # produto individualmente e fica bem mais lenta que antes; 3 min não é
    # mais suficiente pra maioria dos casos)
    status = None
    info = None
    falhas_consecutivas = 0
    for _ in range(48):
        time.sleep(10)
        status_url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_TOKEN}"
        try:
            info = requests.get(status_url, timeout=30).json()["data"]
            falhas_consecutivas = 0
        except requests.exceptions.RequestException as e:
            # 20/07/2026: timeout/instabilidade pontual da API da Apify não
            # deve derrubar a coleta inteira (já rodando há minutos) — só
            # tenta de novo na próxima volta do loop. Só desiste se falhar
            # muitas vezes seguidas (rede realmente fora do ar).
            falhas_consecutivas += 1
            print(f"  ⚠️ Falha ao consultar status (tentativa {falhas_consecutivas}/5): {e}")
            if falhas_consecutivas >= 5:
                print(f"  ❌ Muitas falhas seguidas consultando status — desistindo deste termo.")
                return []
            continue
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
    # ✅ 20/07/2026: corrigir_offset_nomes() removida daqui — era pro campo
    # 'name' antigo, que não existe mais (agora é 'title', correto por item).

    if detectar_dados_mock(items):
        print(f"  🚨 ALERTA: o actor '{ACTOR_ID}' devolveu DADOS MOCK/FAKE para '{termo}', não dados reais!")
        raise RuntimeError(
            f"Actor {ACTOR_ID} devolveu dados mock/sample (não reais) para o termo '{termo}'. "
            f"Coleta abortada — NADA foi salvo no Sheets. Verifique o plano/config do actor no Apify Console."
        )

    print(f"  ✅ {len(items)} produtos coletados")
    return items

def rodar_apify_loja(shop_url):

    """Coleta produtos de uma loja específica.
    ✅ CONFIRMADO com dado real em 2026-07-02 — mode="shop" + campo "shop"
    (username) retornou 5 produtos reais da Loja 1 (minasclean), com
    shop_id=1781178701 batendo com o Shop ID real já validado via API
    oficial da Shopee em sessão anterior.
    ⚠️ PENDENTE: só voltaram 5 produtos mesmo pedindo maxProducts=40 — esse
    modo pode ter um limite padrão de amostra que ignora "maxProducts".
    Você tem até 24 SKUs; se a coleta real também trouxer só ~5, boa parte
    do catálogo vai ficar sem preço próprio no dashboard. Verificar depois
    do primeiro run de produção — se persistir, procurar um campo tipo
    "limit"/"maxPages" específico do modo shop, ou trocar de estratégia
    (ex: rodar "keyword" com o nome de cada produto)."""
    shop_username = shop_url.rstrip("/").split("/")[-1]
    print(f"  🏪 Coletando loja: {shop_url} (username: {shop_username})")
    url = f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs?token={APIFY_TOKEN}"
    payload = {
        "mode": "shop",
        "shop": shop_username,
        "country": "br",
        "maxProducts": MAX_ITENS_LOJA,
        "sort": "relevancy",
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
    # ✅ 20/07/2026: corrigir_offset_nomes() removida daqui — era pro campo
    # 'name' antigo, que não existe mais (agora é 'title', correto por item).

    if detectar_dados_mock(items):
        print(f"  🚨 ALERTA: o actor '{ACTOR_ID}' devolveu DADOS MOCK/FAKE para a loja '{shop_url}'!")
        raise RuntimeError(
            f"Actor {ACTOR_ID} devolveu dados mock/sample (não reais) para '{shop_url}'. "
            f"Coleta abortada — NADA foi salvo no Sheets."
        )

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
        "estoque", "is_on_sale", "url", "tem_variacoes"
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
    amostra_impressa = False
    contagem_lojas = {"minha_loja": 0, "minha_loja2": 0}

    for termo in TERMOS_BUSCA:
        try:
            produtos_brutos = rodar_apify(termo)
        except Exception as e:
            # 20/07/2026: isola falhas por termo — um erro de rede ou da
            # Apify num termo específico não deve derrubar a coleta dos
            # outros termos já enfileirados.
            print(f"  ❌ Falha ao coletar '{termo}', pulando pro próximo termo: {e}")
            continue
        if produtos_brutos and not amostra_impressa:
            print("\n  🔎 AMOSTRA DO 1º ITEM BRUTO (confira se os campos batem):")
            print(" ", json.dumps(produtos_brutos[0], ensure_ascii=False)[:4000])
            # Verifica explicitamente se existe algum campo de variações
            # (tamanho/quantidade com preço próprio) — a Shopee chama isso
            # de "models" internamente. Sem isso, só capturamos 1 preço por
            # anúncio, mesmo quando o produto tem várias opções na página.
            chaves_variacao = [k for k in produtos_brutos[0].keys()
                                if any(termo in k.lower() for termo in ("model", "tier", "variat", "variant", "option", "sku"))]
            if chaves_variacao:
                print(f"  🧩 Campos que parecem ser de VARIAÇÕES/MODELS: {chaves_variacao}")
                for k in chaves_variacao:
                    print(f"     {k} = {json.dumps(produtos_brutos[0][k], ensure_ascii=False)[:2000]}")
            else:
                print("  ℹ️  Nenhum campo óbvio de variações/models encontrado neste item"
                      " (chaves disponíveis: " + ", ".join(produtos_brutos[0].keys()) + ")")
            amostra_impressa = True

        # ⚠️ 02/07/2026 — histórico: o actor xtracto/shopee-scraper chegou a
        # devolver o campo 'name' desalinhado 1 posição dentro do lote, o
        # que exigia essa correção manual de deslocamento.
        # ✅ 20/07/2026 — RESOLVIDO NA ORIGEM: o actor passou a devolver o
        # título do produto no campo 'title', vinculado corretamente ao
        # item_id/price/url do mesmo item (sem desalinhamento) — confirmado
        # com amostra real. A correção manual de deslocamento foi REMOVIDA
        # daqui porque, com o title já correto por item, ela só piorava as
        # coisas (sobrescrevia o nome certo com o nome do item vizinho).
        # Se esse tipo de desalinhamento voltar a acontecer no futuro,
        # confirmar de novo com uma amostra bruta antes de reintroduzir
        # qualquer correção de deslocamento.
        for i, raw in enumerate(produtos_brutos):
            p = normalizar_item(raw)

            # Reconhece se esse produto é seu (por shop_id), mesmo vindo de
            # uma busca de "concorrente" — mode="shop" está quebrado nesse
            # actor (capado em ~5 produtos), então usamos os resultados de
            # busca normal, que não tem esse limite, pra também capturar
            # seus próprios produtos.
            shopid = str(p.get("shopid") or "")
            if shopid == MINHA_LOJA_SHOPID:
                termo_busca, loja = "minha_loja", "minasclean"
                contagem_lojas["minha_loja"] += 1
            elif shopid == MINHA_LOJA2_SHOPID:
                termo_busca, loja = "minha_loja2", "maximahome"
                contagem_lojas["minha_loja2"] += 1
            else:
                termo_busca, loja = termo, p.get("shopName", "")

            # Pra produtos seus, prioriza originalPrice (preço sem desconto
            # temporário); pra concorrentes, usa o price normal.
            preco = (p.get("originalPrice") or p.get("price", 0)) if termo_busca in ("minha_loja", "minha_loja2") else p.get("price", 0)

            linha = [achatar(v) for v in [
                hoje,
                termo_busca,
                loja,
                (p.get("name") or "")[:120],
                preco,
                p.get("priceMax") or preco,
                p.get("discountPercent", 0),
                p.get("historicalSoldEstimated", ""),
                p.get("rating", 0),
                p.get("reviewCount", 0),
                p.get("stock", 0),
                "Sim" if p.get("isOnSale") else "Não",
                construir_url_shopee(p),
                "Sim" if p.get("temVariacoes") else "Não",
            ]]
            todas_linhas.append(linha)

    salvar(ws, todas_linhas)
    print(f"\n✅ Concluído — {len(todas_linhas)} produtos de {len(TERMOS_BUSCA)} buscas")
    print(f"   Seus produtos capturados nas buscas: Loja 1 = {contagem_lojas['minha_loja']}, Loja 2 = {contagem_lojas['minha_loja2']}")
    if contagem_lojas["minha_loja"] == 0 or contagem_lojas["minha_loja2"] == 0:
        print("   ⚠️ Uma das lojas não apareceu em NENHUMA busca — pode não estar bem")
        print("      rankeada pros termos de busca atuais, ou precisar de mais termos.")
    print(f"   Planilha: https://docs.google.com/spreadsheets/d/{SHEET_ID}")

if __name__ == "__main__":
    main()
