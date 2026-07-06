"""
atualizar_secret_config.py — Minas Clean

Depois que shopee_ads_to_sheets.py renova o access_token/refresh_token e
salva no config.json local, este script pega esse config.json ATUALIZADO
e sobrescreve o Secret do GitHub (SHOPEE_CONFIG_JSON) com o conteúdo novo —
assim, na PRÓXIMA execução agendada, o workflow já baixa o token mais
recente em vez do antigo (que a Shopee pode ter invalidado ao gerar um
novo refresh_token).

Só roda dentro do GitHub Actions (precisa das env vars abaixo, vindas de
Secrets). Não precisa rodar isso localmente no seu PC.

Requer:
  - GH_PAT: Personal Access Token com escopo 'repo' (para poder escrever
    Secrets do repositório via API — o GITHUB_TOKEN automático do Actions
    não tem essa permissão).
  - GITHUB_REPOSITORY: já vem pronto do próprio GitHub Actions
    (formato "dono/repo").
"""

import os
import sys
import json
import base64
import requests
from nacl import encoding, public

CONFIG_PATH = os.environ.get("SHOPEE_CONFIG_FILE", "config.json")
SECRET_NAME = "SHOPEE_CONFIG_JSON"

def encriptar_secret(chave_publica_b64: str, valor_texto: str) -> str:
    """Criptografa um valor usando a chave pública do repositório (libsodium
    sealed box), no formato exigido pela API de Secrets do GitHub."""
    chave_publica = public.PublicKey(chave_publica_b64.encode("utf-8"), encoding.Base64Encoder())
    caixa_selada = public.SealedBox(chave_publica)
    criptografado = caixa_selada.encrypt(valor_texto.encode("utf-8"))
    return base64.b64encode(criptografado).decode("utf-8")

def main():
    gh_pat = os.environ.get("GH_PAT")
    repo = os.environ.get("GITHUB_REPOSITORY")  # ex: "minasdashboard/minas-clean-dashboard"

    if not gh_pat or not repo:
        sys.exit("❌ GH_PAT ou GITHUB_REPOSITORY não configurados — abortando (isso só roda no GitHub Actions).")

    if not os.path.isfile(CONFIG_PATH):
        sys.exit(f"❌ Não achei {CONFIG_PATH} para salvar de volta no Secret.")

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        conteudo_config = f.read()

    # valida que o conteúdo é JSON válido antes de sobrescrever o Secret
    # (proteção: nunca salva um config.json quebrado por cima do que já funciona)
    try:
        json.loads(conteudo_config)
    except json.JSONDecodeError as e:
        sys.exit(f"❌ config.json local está com JSON inválido, NÃO vou atualizar o Secret: {e}")

    headers = {
        "Authorization": f"Bearer {gh_pat}",
        "Accept": "application/vnd.github+json",
    }

    # 1) pega a chave pública do repositório (necessária pra criptografar o secret)
    r = requests.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key", headers=headers, timeout=30)
    r.raise_for_status()
    info_chave = r.json()

    # 2) criptografa o config.json atualizado com essa chave
    valor_criptografado = encriptar_secret(info_chave["key"], conteudo_config)

    # 3) grava/atualiza o Secret no repositório
    payload = {
        "encrypted_value": valor_criptografado,
        "key_id": info_chave["key_id"],
    }
    r = requests.put(
        f"https://api.github.com/repos/{repo}/actions/secrets/{SECRET_NAME}",
        headers=headers, json=payload, timeout=30,
    )
    if r.status_code in (201, 204):
        print(f"✅ Secret '{SECRET_NAME}' atualizado com o config.json mais recente.")
    else:
        print(f"❌ Falha ao atualizar o Secret (status {r.status_code}): {r.text[:500]}")
        sys.exit(1)

if __name__ == "__main__":
    main()
