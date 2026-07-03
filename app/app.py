import hmac
import html
import os
import shutil
import tempfile
from functools import wraps

from flask import Flask, Response, request, send_file

from mapa_core import PlanilhaInvalida, gerar

DATA_DIR = os.environ.get("MAPA_DATA_DIR", "/data")
DADOS_PATH = os.path.join(DATA_DIR, "dados_atual")
CACHE_PATH = os.path.join(DATA_DIR, "geocode_cache.json")
INDEX_PATH = os.path.join(DATA_DIR, "index.html")
NOME_ORIGINAL_PATH = os.path.join(DATA_DIR, "nome_original.txt")

EXTENSOES_PERMITIDAS = {".csv", ".xlsx"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

AUTH_USER = os.environ.get("MAPA_AUTH_USER", "")
AUTH_PASS = os.environ.get("MAPA_AUTH_PASS", "")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


def requer_auth(view):
    """Basic auth opcional: só exige credenciais se MAPA_AUTH_USER estiver
    configurado. Sem essa variável, o app fica aberto (ex: atrás de um
    proxy/rede já controlada por outra camada)."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not AUTH_USER:
            return view(*args, **kwargs)
        auth = request.authorization
        usuario_ok = auth and hmac.compare_digest(auth.username or "", AUTH_USER)
        senha_ok = auth and hmac.compare_digest(auth.password or "", AUTH_PASS)
        if not (usuario_ok and senha_ok):
            return Response(
                "Autenticação necessária.", 401,
                {"WWW-Authenticate": 'Basic realm="Mapa de Imoveis"'},
            )
        return view(*args, **kwargs)
    return wrapper

FORM_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Atualizar mapa</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #0f172a;
    --card-bg: #1e293b;
    --borda: #334155;
    --texto: #e2e8f0;
    --texto-sec: #94a3b8;
    --accent: #3b82f6;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    color: var(--texto);
    padding: 24px;
  }
  .card {
    width: 100%;
    max-width: 460px;
    background: var(--card-bg);
    border: 1px solid var(--borda);
    border-radius: 16px;
    padding: 32px;
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.4);
  }
  h1 {
    font-size: 19px;
    margin: 0 0 8px;
    letter-spacing: -0.01em;
    font-weight: 700;
  }
  p.descricao {
    font-size: 13.5px;
    color: var(--texto-sec);
    line-height: 1.55;
    margin: 0 0 22px;
  }
  form { display: flex; flex-direction: column; gap: 14px; }
  input[type="file"] {
    padding: 10px;
    border: 1px dashed var(--borda);
    border-radius: 10px;
    background: var(--bg);
    color: var(--texto-sec);
    font-size: 12.5px;
  }
  input[type="file"]::file-selector-button {
    background: var(--accent);
    color: #fff;
    border: none;
    padding: 7px 14px;
    border-radius: 7px;
    margin-right: 10px;
    cursor: pointer;
    font-size: 12.5px;
    font-weight: 600;
  }
  button[type="submit"] {
    background: var(--accent);
    color: #fff;
    border: none;
    padding: 11px 16px;
    border-radius: 10px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: filter 0.15s ease;
  }
  button[type="submit"]:hover { filter: brightness(1.1); }
  .voltar {
    display: inline-block;
    margin-top: 20px;
    font-size: 13px;
    color: var(--texto-sec);
    text-decoration: none;
  }
  .voltar:hover { color: var(--texto); }
  .botao-download {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    margin-top: 14px;
    padding: 10px 16px;
    border: 1px solid var(--borda);
    border-radius: 10px;
    background: transparent;
    color: var(--texto);
    font-size: 13.5px;
    font-weight: 600;
    text-decoration: none;
    transition: background 0.15s ease;
  }
  .botao-download:hover { background: var(--bg); }
  .banner {
    padding: 12px 14px;
    border-radius: 10px;
    font-size: 13.5px;
    margin-bottom: 18px;
    line-height: 1.5;
  }
  .banner.erro {
    background: rgba(220, 38, 38, 0.15);
    color: #fca5a5;
    border: 1px solid rgba(220, 38, 38, 0.3);
  }
  .banner.sucesso {
    background: rgba(0, 144, 104, 0.15);
    color: #6ee7b7;
    border: 1px solid rgba(0, 144, 104, 0.3);
  }
  .banner.aviso {
    background: rgba(217, 119, 6, 0.15);
    color: #fcd34d;
    border: 1px solid rgba(217, 119, 6, 0.3);
  }
  .banner ul { margin: 8px 0 0; padding-left: 18px; }
  .banner a { color: inherit; font-weight: 600; }
</style>
</head>
<body>
<div class="card">
  <h1>Atualizar planilha do mapa</h1>
  <p class="descricao">Envie um arquivo .csv ou .xlsx (max 5MB) com as colunas da tabela de imóveis.
  Endereços sem lat/long serão geocodificados automaticamente (pode demorar alguns minutos em planilhas grandes).</p>
  __MENSAGEM__
  <form method="post" action="/upload" enctype="multipart/form-data">
    <input type="file" name="planilha" accept=".csv,.xlsx" required>
    <button type="submit">Enviar e atualizar</button>
  </form>
  __DOWNLOAD__
  <a class="voltar" href="/">&larr; Voltar ao mapa</a>
</div>
</body>
</html>
"""


def _extensao_planilha_atual():
    for extensao in EXTENSOES_PERMITIDAS:
        if os.path.exists(DADOS_PATH + extensao):
            return extensao
    return None


def _sanitizar_nome_original(nome: str) -> str:
    nome = nome.replace("/", "_").replace("\\", "_").replace("\x00", "").strip()
    return nome[:255] or "planilha"


def _nome_download_atual(extensao: str) -> str:
    if os.path.exists(NOME_ORIGINAL_PATH):
        with open(NOME_ORIGINAL_PATH, "r", encoding="utf-8") as f:
            nome = f.read().strip()
        if nome:
            return nome
    return "planilha_atual" + extensao


def pagina_upload(mensagem: str = "") -> str:
    extensao = _extensao_planilha_atual()
    download = (
        f'<a class="botao-download" href="/download">'
        f"Baixar planilha atual ({html.escape(_nome_download_atual(extensao))})</a>"
        if extensao
        else ""
    )
    return FORM_HTML.replace("__MENSAGEM__", mensagem).replace("__DOWNLOAD__", download)


@app.route("/")
@requer_auth
def index():
    if not os.path.exists(INDEX_PATH):
        return "Mapa ainda não foi gerado. <a href='/upload'>Enviar planilha</a>.", 404
    return send_file(INDEX_PATH)


@app.route("/upload", methods=["GET"])
@requer_auth
def upload_form():
    return pagina_upload()


@app.route("/download")
@requer_auth
def download():
    extensao = _extensao_planilha_atual()
    if not extensao:
        return "Nenhuma planilha foi enviada ainda.", 404
    return send_file(
        DADOS_PATH + extensao,
        as_attachment=True,
        download_name=_nome_download_atual(extensao),
    )


@app.route("/upload", methods=["POST"])
@requer_auth
def upload_post():
    arquivo = request.files.get("planilha")
    if not arquivo or not arquivo.filename:
        return pagina_upload('<div class="banner erro">Nenhum arquivo selecionado.</div>'), 400

    extensao = os.path.splitext(arquivo.filename)[1].lower()
    if extensao not in EXTENSOES_PERMITIDAS:
        return pagina_upload(
            '<div class="banner erro">Formato não suportado. Envie .csv ou .xlsx.</div>'
        ), 400

    os.makedirs(DATA_DIR, exist_ok=True)
    destino_planilha = DADOS_PATH + extensao

    with tempfile.NamedTemporaryFile(dir=DATA_DIR, suffix=extensao, delete=False) as tmp:
        arquivo.save(tmp.name)
        tmp_path = tmp.name

    try:
        nao_encontrados = gerar(tmp_path, CACHE_PATH, INDEX_PATH)
    except PlanilhaInvalida as exc:
        os.remove(tmp_path)
        return pagina_upload(f'<div class="banner erro">{html.escape(str(exc))}</div>'), 400
    except Exception:
        os.remove(tmp_path)
        app.logger.exception("Falha ao processar planilha enviada")
        return pagina_upload(
            '<div class="banner erro">Erro interno ao processar a planilha.</div>'
        ), 500

    for outra_extensao in EXTENSOES_PERMITIDAS - {extensao}:
        outro_caminho = DADOS_PATH + outra_extensao
        if os.path.exists(outro_caminho):
            os.remove(outro_caminho)
    shutil.move(tmp_path, destino_planilha)

    with open(NOME_ORIGINAL_PATH, "w", encoding="utf-8") as f:
        f.write(_sanitizar_nome_original(arquivo.filename))

    aviso = ""
    if nao_encontrados:
        linhas = "".join(
            f"<li>{html.escape(str(end))}</li>" for _, end in nao_encontrados[:20]
        )
        aviso = (
            f'<div class="banner aviso">{len(nao_encontrados)} endereço(s) não '
            f"foram geocodificados e ficaram de fora do mapa:<ul>{linhas}</ul></div>"
        )

    return pagina_upload(
        '<div class="banner sucesso">Mapa atualizado com sucesso. '
        '<a href="/">Ver mapa atualizado</a></div>' + aviso
    )


@app.errorhandler(413)
def arquivo_grande(_exc):
    return pagina_upload('<div class="banner erro">Arquivo maior que 5MB.</div>'), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
