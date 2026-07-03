"""Logica compartilhada de geracao do mapa: usada pelo script CLI e pelo app web."""
import html
import json
import os
import re
import time
import unicodedata

import folium
import pandas as pd
from geopy.exc import GeocoderServiceError, GeocoderTimedOut
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim

COLOR_AREA_COMUM = "blue"
COLOR_AREA_RISCO = "red"

MAX_LINHAS = 5000


class PlanilhaInvalida(Exception):
    pass


def normalizar_coluna(nome: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", sem_acento.strip().lower()).strip("_")


_FAIXA_LAT = (-35.0, 6.0)
_FAIXA_LONG = (-75.0, -33.0)


def _corrigir_coordenada(valor, faixa):
    """Corrige lat/long sem ponto decimal (Excel BR interpreta '.' como
    separador de milhar ao exportar, ex: -22.9363794 vira -229363794).
    O numero de casas decimais perdidas varia por linha (6, 7...), entao
    testamos varias posicoes de ponto decimal e ficamos com a que cai
    dentro da faixa geografica plausivel do Brasil."""
    if pd.isna(valor):
        return None
    texto = str(valor).strip()
    if not texto or texto.lower() == "nan":
        return None

    if "." in texto or "," in texto:
        try:
            numero = float(texto.replace(",", "."))
        except ValueError:
            return None
        return numero if faixa[0] <= numero <= faixa[1] else None

    try:
        inteiro = int(texto)
    except ValueError:
        return None

    if abs(inteiro) <= 1000:
        numero = float(inteiro)
        return numero if faixa[0] <= numero <= faixa[1] else None

    for casas in range(4, 10):
        candidato = inteiro / (10**casas)
        if faixa[0] <= candidato <= faixa[1]:
            return candidato
    return None


def carregar_dados(caminho: str) -> pd.DataFrame:
    extensao = os.path.splitext(caminho)[1].lower()
    try:
        if extensao == ".xlsx":
            df = pd.read_excel(caminho, dtype=str)
        else:
            df = pd.read_csv(caminho, dtype=str)
    except Exception as exc:
        raise PlanilhaInvalida(f"Nao foi possivel ler o arquivo como planilha: {exc}") from exc

    if len(df) > MAX_LINHAS:
        raise PlanilhaInvalida(f"Planilha tem {len(df)} linhas, limite e {MAX_LINHAS}.")

    df.columns = [normalizar_coluna(c) for c in df.columns]
    if "endereco" not in df.columns:
        raise PlanilhaInvalida("Coluna 'endereco' nao encontrada na planilha.")

    if "lat" in df.columns:
        df["lat"] = df["lat"].apply(lambda v: _corrigir_coordenada(v, _FAIXA_LAT))
    else:
        df["lat"] = None
    if "long" in df.columns:
        df["long"] = df["long"].apply(lambda v: _corrigir_coordenada(v, _FAIXA_LONG))
    else:
        df["long"] = None

    df["violencia"] = (
        df.get("violencia", "FALSE").astype(str).str.strip().str.upper().eq("TRUE")
    )
    return df


def carregar_cache(caminho: str) -> dict:
    if os.path.exists(caminho):
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def salvar_cache(caminho: str, cache: dict) -> None:
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def geocodificar_faltantes(df: pd.DataFrame, cache_path: str) -> tuple[pd.DataFrame, list]:
    faltantes = df["lat"].isna() | df["long"].isna()
    nao_encontrados = []
    if not faltantes.any():
        return df, nao_encontrados

    cache = carregar_cache(cache_path)
    geolocator = Nominatim(user_agent="mapa-imoveis-heazul")
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1)

    for idx in df[faltantes].index:
        endereco = str(df.at[idx, "endereco"]).strip()
        if not endereco or endereco.lower() == "nan":
            nao_encontrados.append((idx, endereco))
            continue

        if endereco in cache:
            cached = cache[endereco]
            lat, lon = (cached[0], cached[1]) if cached else (None, None)
        else:
            try:
                resultado = geocode(f"{endereco}, Brasil")
            except (GeocoderTimedOut, GeocoderServiceError):
                nao_encontrados.append((idx, endereco))
                continue

            if resultado is None:
                nao_encontrados.append((idx, endereco))
                cache[endereco] = None
                salvar_cache(cache_path, cache)
                continue

            lat, lon = resultado.latitude, resultado.longitude
            cache[endereco] = [lat, lon]
            salvar_cache(cache_path, cache)

        if lat is None or lon is None:
            nao_encontrados.append((idx, endereco))
            continue

        df.at[idx, "lat"] = lat
        df.at[idx, "long"] = lon

    return df, nao_encontrados


def _parse_valor(valor) -> float | None:
    """Converte texto numerico para float, aceitando tanto formato brasileiro
    (1.234,56) quanto o formato simples vindo de celulas numericas do Excel
    via pandas (1234.56, sem separador de milhar). Tambem tolera o valor vir
    como texto com simbolo de moeda (ex: 'R$ 1.234,56'), caso a celula nao
    seja realmente numerica no Excel."""
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto or texto.lower() == "nan":
        return None

    texto = re.sub(r"r\$", "", texto, flags=re.IGNORECASE)
    texto = texto.replace("\xa0", " ").replace(" ", "").strip()
    if not texto:
        return None

    try:
        if "." in texto and "," in texto:
            return float(texto.replace(".", "").replace(",", "."))
        if "," in texto:
            return float(texto.replace(",", "."))
        return float(texto)
    except ValueError:
        return None


def _parse_ano(valor) -> int | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto or texto.lower() == "nan":
        return None
    match = re.search(r"(\d{4})", texto)
    if not match:
        return None
    ano = int(match.group(1))
    return ano if 1900 <= ano <= 2100 else None


def formatar_moeda(valor) -> str:
    valor_float = _parse_valor(valor)
    if valor_float is None:
        return "N/A"
    texto = f"{valor_float:,.2f}"
    texto = texto.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {texto}"


def cor_do_marcador(violencia: bool) -> str:
    return COLOR_AREA_RISCO if violencia else COLOR_AREA_COMUM


def montar_popup(row: pd.Series) -> str:
    aviso = (
        '<p style="color:red;font-weight:bold;">'
        "⚠ Area com indicador de criminalidade/risco</p>"
        if row.get("violencia")
        else ""
    )
    descricao = html.escape(str(row.get("descricao", "")))
    tipo = html.escape(str(row.get("tipo", "")))
    endereco = html.escape(str(row.get("endereco", "")))
    situacao = html.escape(str(row.get("situacao_atu", "")))
    return f"""
    <div style="font-family: Arial, sans-serif; font-size: 13px; max-width: 260px;">
        <b>{descricao}</b><br>
        <b>Tipo:</b> {tipo}<br>
        <b>Endereco:</b> {endereco}<br>
        <b>Situacao:</b> {situacao}<br>
        <b>Valor contabil:</b> {formatar_moeda(row.get('valor_cont'))}<br>
        <b>Valor de avaliacao:</b> {formatar_moeda(row.get('valor_aval'))}<br>
        {aviso}
    </div>
    """


def extrair_cidade(endereco: str) -> str:
    """Heuristica: extrai a cidade do texto livre de endereco.
    Espera formatos como '..., Cidade/UF' ou '..., Cidade, UF'."""
    if not endereco or not isinstance(endereco, str):
        return ""
    partes = [p.strip() for p in endereco.split(",") if p.strip()]
    if not partes:
        return ""

    ultima = partes[-1]
    if "/" in ultima:
        return ultima.split("/")[0].strip()
    if len(ultima) <= 3 and ultima.isupper():
        return partes[-2] if len(partes) >= 2 else ""
    return ultima


def _preparar_pontos(df: pd.DataFrame) -> list:
    pontos = []
    validos = df.dropna(subset=["lat", "long"])
    for _, row in validos.iterrows():
        tipo = str(row.get("tipo") or "Sem categoria")
        violencia = bool(row.get("violencia", False))
        cidade = row.get("cidade") or ""
        if not cidade:
            cidade = extrair_cidade(str(row.get("endereco", "")))
        valor_aval = _parse_valor(row.get("valor_aval"))
        valor_cont = _parse_valor(row.get("valor_cont"))
        if valor_aval is not None and valor_cont is not None:
            valor_filtro = max(valor_aval, valor_cont)
        else:
            valor_filtro = valor_aval if valor_aval is not None else (valor_cont or 0)
        ano = _parse_ano(row.get("aquisicao"))

        pontos.append({
            "lat": float(row["lat"]),
            "lng": float(row["long"]),
            "tipo": tipo,
            "cidade": cidade,
            "violencia": violencia,
            "valor": round(valor_filtro, 2),
            "ano": ano,
            "cor": cor_do_marcador(violencia),
            "popup": montar_popup(row),
            "tooltip": html.escape(str(row.get("descricao", ""))),
        })
    return pontos


def montar_mapa(df: pd.DataFrame) -> folium.Map:
    mapa = folium.Map(location=[-22.95, -43.3], zoom_start=9, tiles=None)

    pontos = _preparar_pontos(df)
    dados_json = json.dumps(pontos, ensure_ascii=False)
    script = (
        _FILTROS_JS
        .replace("__DADOS_JSON__", dados_json)
        .replace("__MAP_NAME__", mapa.get_name())
    )
    mapa.get_root().header.add_child(folium.Element(_FILTROS_CSS))
    mapa.get_root().html.add_child(folium.Element(_FILTROS_HTML))
    mapa.get_root().script.add_child(folium.Element(script))
    return mapa


def gerar(csv_path: str, cache_path: str, output_path: str) -> list:
    """Executa o pipeline completo. Retorna lista de (idx, endereco) nao geocodificados."""
    df = carregar_dados(csv_path)
    df, nao_encontrados = geocodificar_faltantes(df, cache_path)
    mapa = montar_mapa(df)
    mapa.save(output_path)
    return nao_encontrados


_FILTROS_CSS = """
<style>
:root {
  --painel-bg: #1e293b;
  --painel-bg-alt: #0f172a;
  --painel-bg-hover: #27374b;
  --painel-borda: #334155;
  --texto-principal: #e2e8f0;
  --texto-secundario: #94a3b8;
  --accent: #3b82f6;
  --accent-suave: #1e3a5f;
  --sombra: 0 10px 30px rgba(0, 0, 0, 0.55);
  --switch-bg: #0f172a;
}

body.tema-claro {
  --painel-bg: #ffffff;
  --painel-bg-alt: #f3f4f6;
  --painel-bg-hover: #e5e7eb;
  --painel-borda: #e5e7eb;
  --texto-principal: #1f2937;
  --texto-secundario: #6b7280;
  --accent: #2563eb;
  --accent-suave: #dbeafe;
  --sombra: 0 10px 30px rgba(15, 23, 42, 0.12);
  --switch-bg: #ffffff;
}

.leaflet-container { background: #1e293b; }
body.tema-claro .leaflet-container { background: #f8f9fa; }
.tiles-escuro { filter: brightness(1.35) contrast(0.9); }

body.tema-escuro .leaflet-popup-content-wrapper,
body.tema-escuro .leaflet-popup-tip {
  background: #1e293b;
  color: #e2e8f0;
}

#cards-filtros {
  position: fixed;
  top: 16px;
  right: 16px;
  z-index: 1000;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 12px;
  max-height: calc(100vh - 32px);
  overflow-y: auto;
  overflow-x: hidden;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
  scrollbar-width: thin;
  scrollbar-color: var(--painel-borda) transparent;
}
#cards-filtros::-webkit-scrollbar { width: 6px; }
#cards-filtros::-webkit-scrollbar-track { background: transparent; }
#cards-filtros::-webkit-scrollbar-thumb {
  background: var(--painel-borda);
  border-radius: 999px;
}
#cards-filtros::-webkit-scrollbar-thumb:hover { background: var(--texto-secundario); }

.card-filtro {
  box-sizing: border-box;
  width: 380px;
  flex-shrink: 0;
  background: var(--painel-bg);
  border-radius: 14px;
  box-shadow: var(--sombra);
  color: var(--texto-principal);
  overflow: hidden;
  transition: width 0.2s ease, height 0.2s ease;
}
.card-filtro * { box-sizing: border-box; }

.card-filtro.recolhido {
  width: 46px;
  height: 46px;
  aspect-ratio: 1 / 1;
  border-radius: 50%;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
}
.card-filtro.recolhido .card-titulo,
.card-filtro.recolhido .card-chevron,
.card-filtro.recolhido .card-corpo {
  display: none;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 12px 14px;
}
.card-filtro.recolhido .card-header { padding: 0; height: 100%; }

.card-icone {
  appearance: none;
  -webkit-appearance: none;
  width: 22px;
  height: 22px;
  border: none;
  outline: none;
  background: transparent;
  box-shadow: none;
  color: var(--texto-principal);
  padding: 0;
  cursor: pointer;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}
.card-icone:focus, .card-icone:focus-visible { outline: none; box-shadow: none; }
.card-icone svg { width: 100%; height: 100%; display: block; }

.card-titulo {
  flex: 1;
  font-size: 14px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--texto-secundario);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.card-chevron {
  border: none;
  background: transparent;
  color: var(--texto-secundario);
  cursor: pointer;
  font-size: 13px;
  padding: 4px;
  transition: transform 0.15s ease;
}
.card-filtro.recolhido .card-chevron { transform: rotate(-90deg); }

.card-corpo { padding: 0 14px 14px; }

.secao-corpo {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.item-linha {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  font-size: 15px;
}
.item-linha .item-label {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.swatch {
  width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
}

#select-unidade {
  width: 100%;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--painel-borda);
  background: var(--painel-bg-alt);
  font-size: 15px;
  color: var(--texto-principal);
  cursor: pointer;
}
#select-unidade:focus { outline: 2px solid var(--accent-suave); border-color: var(--accent); }

.item-direita {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.contagem {
  min-width: 1.4em;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--painel-bg-alt);
  color: var(--texto-secundario);
  font-size: 12.5px;
  font-weight: 600;
  text-align: center;
}

/* Toggle: https://uiverse.io/nikk7007/smooth-fox-6 (adaptado ao painel) */
.switch {
  --false: #E81B1B;
  --true: #009068;
  display: inline-flex;
  flex-shrink: 0;
}

.switch input[type="checkbox"] {
  appearance: none;
  height: 1.6rem;
  width: 3.6rem;
  background-color: var(--switch-bg);
  border: 1px solid var(--painel-borda);
  position: relative;
  border-radius: 0.3em;
  cursor: pointer;
  margin: 0;
}

.switch input[type="checkbox"]::before {
  content: '';
  display: block;
  height: 1.2em;
  width: 1.2em;
  transform: translate(-50%, -50%);
  position: absolute;
  top: 50%;
  left: calc(1.2em/2 + 0.25em);
  background-color: var(--false);
  border-radius: 0.25em;
  transition: 0.3s ease;
}

.switch input[type="checkbox"]:checked::before {
  background-color: var(--true);
  left: calc(100% - (1.2em/2 + 0.25em));
}

.slider-valor { padding-top: 4px; }
.slider-track-wrap { position: relative; height: 22px; }
.slider-track-wrap input[type="range"] {
  position: absolute;
  width: 100%;
  top: 6px;
  -webkit-appearance: none;
  appearance: none;
  background: transparent;
  pointer-events: none;
  margin: 0;
}
.slider-track-wrap input[type="range"]::-webkit-slider-runnable-track {
  height: 4px; background: var(--painel-borda); border-radius: 2px;
}
.slider-track-wrap input[type="range"]::-webkit-slider-thumb {
  -webkit-appearance: none;
  pointer-events: auto;
  width: 16px; height: 16px;
  border-radius: 50%;
  background: var(--accent);
  border: 2px solid var(--painel-bg);
  box-shadow: 0 1px 4px rgba(0,0,0,0.3);
  cursor: pointer;
  margin-top: -6px;
}
.slider-track-wrap input[type="range"]::-moz-range-track {
  height: 4px; background: var(--painel-borda); border-radius: 2px;
}
.slider-track-wrap input[type="range"]::-moz-range-thumb {
  pointer-events: auto;
  width: 14px; height: 14px;
  border-radius: 50%;
  background: var(--accent);
  border: 2px solid var(--painel-bg);
  box-shadow: 0 1px 4px rgba(0,0,0,0.3);
  cursor: pointer;
}
.slider-labels {
  display: flex;
  justify-content: space-between;
  font-size: 13.5px;
  color: var(--texto-secundario);
  margin-top: 6px;
}

#limpar-filtros {
  width: 100%;
  margin-top: 12px;
  padding: 9px 10px;
  border: 1px solid var(--painel-borda);
  background: var(--painel-bg-alt);
  color: var(--texto-principal);
  border-radius: 8px;
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
}
#limpar-filtros:hover { background: var(--painel-bg-hover); }

#contador-resultados {
  margin-top: 10px;
  font-size: 13.5px;
  color: var(--texto-secundario);
  text-align: center;
}

.botoes-flutuantes {
  position: fixed;
  bottom: 20px;
  left: 20px;
  z-index: 1000;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.botao-flutuante {
  position: relative;
  width: 50px;
  height: 50px;
  border: none;
  border-radius: 50%;
  background: var(--accent);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: var(--sombra);
  text-decoration: none;
  cursor: pointer;
  transition: transform 0.15s ease, filter 0.15s ease;
}
.botao-flutuante:hover { transform: scale(1.07); filter: brightness(1.1); }

#toggle-tema { font-size: 20px; line-height: 1; }

.botao-tooltip {
  position: absolute;
  bottom: 62px;
  left: 50%;
  transform: translateX(-50%) translateY(4px);
  background: var(--painel-bg);
  color: var(--texto-principal);
  padding: 6px 12px;
  border-radius: 8px;
  font-size: 12.5px;
  font-weight: 500;
  white-space: nowrap;
  box-shadow: var(--sombra);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s ease, transform 0.15s ease;
}
.botao-flutuante:hover .botao-tooltip { opacity: 1; transform: translateX(-50%) translateY(0); }

@media (max-width: 480px) {
  #cards-filtros { right: 12px; top: 12px; }
  .card-filtro { width: calc(100vw - 24px); }
}
</style>
"""

_ICONE_RULER = (
    '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg"><path fill="currentColor" '
    'd="M24.63,1.62V98.38H40.55v-82H75.37V1.62Z"/></svg>'
)
_ICONE_HOUSE = (
    '<svg viewBox="0 0 192 192" xmlns="http://www.w3.org/2000/svg"><path '
    'd="M41.733 160.134v-59.2H21.999L96 31.865l74 69.067h-19.733v59.201H110.8v-44.4H81.2v44.4z" '
    'fill="none" stroke="currentColor" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
_ICONE_PIN = (
    '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">'
    '<path d="M12 21C15.5 17.4 19 14.1764 19 10.2C19 6.22355 15.866 3 12 3C8.13401 3 5 6.22355 5 10.2C5 14.1764 8.5 17.4 12 21Z" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
    '<path d="M12 13C13.6569 13 15 11.6569 15 10C15 8.34315 13.6569 7 12 7C10.3431 7 9 8.34315 9 10C9 11.6569 10.3431 13 12 13Z" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
_ICONE_BULLET = (
    '<svg viewBox="0 0 503.607 503.607" xmlns="http://www.w3.org/2000/svg">'
    '<g transform="translate(-1 -1)" fill="currentColor"><path d="M293.946,456.778c0.107-0.386,0.213-0.773,0.293-1.175l6.715-26.02c0-0.63,0-1.261-0.026-1.891'
    'c0.012-0.207,0.026-0.413,0.026-0.627V227.368c0.194-1.279,0.193-2.59-0.209-3.795c-0.304-1.485-0.945-2.761-1.863-3.772'
    'l-14.715-29.431v-48.682c0-33.574-5.036-67.987-15.108-100.721L258.987,4.875C257.308,1.518,253.951-1,250.593-1'
    's-6.715,2.518-8.393,5.875l-10.072,36.092c-10.072,32.734-15.108,67.148-15.108,100.721v47.843l-15.948,31.895'
    'c-1.315,1.972-1.585,4.455-0.839,6.651v198.988c0,0.214,0.014,0.42,0.026,0.627c-0.026,0.63-0.026,1.261-0.026,1.891l6.715,25.18'
    'c0.165,0.823,0.401,1.613,0.674,2.385c-4.539,3.823-7.389,9.56-7.389,16.081v9.233c0,10.911,9.233,20.144,20.984,20.144h58.754'
    'c11.751,0,20.984-9.233,21.823-20.144v-9.233C301.793,466.493,298.75,460.595,293.946,456.778z M223.734,451.407l-4.197-15.948'
    'h62.111l-4.197,15.948c0,0-0.839,0.839-1.679,0.839h-51.2C223.734,452.246,223.734,452.246,223.734,451.407z M267.38,183.656'
    'h-33.574v-33.574h33.574V183.656z M230.449,200.443h40.289l8.393,16.787h-57.075L230.449,200.443z M284.167,234.016v184.656'
    'H217.02V234.016H284.167z M248.075,46.003l2.518-9.233l2.518,9.233c8.394,28.538,13.43,57.915,14.269,87.292h-33.574'
    'C234.646,103.918,239.682,74.541,248.075,46.003z M284.167,482.462c0,1.679-1.679,3.357-3.357,3.357h-59.593'
    'c-2.518,0-4.197-1.679-4.197-3.357v-9.233c0-2.518,1.679-4.197,4.197-4.197h3.357h52.039h3.357c2.518,0,4.197,1.679,4.197,4.197'
    'V482.462z"/></g></svg>'
)
_ICONE_MONEY = (
    '<svg viewBox="-0.5 0 25 25" fill="none" xmlns="http://www.w3.org/2000/svg">'
    '<path d="M12.7003 17.1099V18.22C12.7003 18.308 12.6829 18.395 12.6492 18.4763C12.6156 18.5576 12.5662 18.6316 12.504 18.6938C12.4418 18.7561 12.3679 18.8052 12.2867 18.8389C12.2054 18.8725 12.1182 18.8899 12.0302 18.8899C11.9423 18.8899 11.8551 18.8725 11.7738 18.8389C11.6925 18.8052 11.6187 18.7561 11.5565 18.6938C11.4943 18.6316 11.4449 18.5576 11.4113 18.4763C11.3776 18.395 11.3602 18.308 11.3602 18.22V17.0801C10.9165 17.0072 10.4917 16.8468 10.1106 16.6082C9.72943 16.3695 9.39958 16.0573 9.14023 15.6899C9.04577 15.57 8.99311 15.4226 8.99023 15.27C8.99148 15.1842 9.00997 15.0995 9.04459 15.021C9.0792 14.9425 9.12927 14.8718 9.19177 14.813C9.25428 14.7542 9.32794 14.7087 9.40842 14.679C9.4889 14.6492 9.57455 14.6359 9.66025 14.6399C9.74504 14.6401 9.82883 14.6582 9.90631 14.6926C9.98379 14.7271 10.0532 14.7773 10.1102 14.8401C10.4326 15.2576 10.8657 15.5763 11.3602 15.76V13.21C10.0302 12.69 9.36023 11.9099 9.36023 10.8999C9.38027 10.3592 9.5928 9.84343 9.9595 9.44556C10.3262 9.04769 10.8229 8.79397 11.3602 8.72998V7.62988C11.3602 7.5419 11.3776 7.45482 11.4113 7.37354C11.4449 7.29225 11.4943 7.21847 11.5565 7.15625C11.6187 7.09403 11.6925 7.04466 11.7738 7.01099C11.8551 6.97732 11.9423 6.95996 12.0302 6.95996C12.1182 6.95996 12.2054 6.97732 12.2867 7.01099C12.3679 7.04466 12.4418 7.09403 12.504 7.15625C12.5662 7.21847 12.6156 7.29225 12.6492 7.37354C12.6829 7.45482 12.7003 7.5419 12.7003 7.62988V8.71997C13.0724 8.77828 13.4289 8.91103 13.7485 9.11035C14.0681 9.30967 14.3442 9.57137 14.5602 9.87988C14.6555 9.99235 14.7117 10.1329 14.7202 10.28C14.7229 10.3662 14.7084 10.4519 14.6776 10.5325C14.6467 10.613 14.6002 10.6867 14.5406 10.749C14.481 10.8114 14.4096 10.8613 14.3306 10.8958C14.2516 10.9303 14.1665 10.9487 14.0802 10.95C13.99 10.9475 13.9013 10.9257 13.8202 10.886C13.7391 10.8463 13.6675 10.7897 13.6102 10.72C13.3718 10.4221 13.0575 10.1942 12.7003 10.0601V12.3101L12.9503 12.4099C14.2203 12.9099 15.0103 13.63 15.0103 14.77C14.9954 15.3808 14.7481 15.9629 14.3189 16.3977C13.8897 16.8325 13.3108 17.0871 12.7003 17.1099ZM11.3602 11.73V10.0999C11.1988 10.1584 11.0599 10.2662 10.963 10.408C10.8662 10.5497 10.8162 10.7183 10.8203 10.8899C10.8173 11.0676 10.8669 11.2424 10.963 11.3918C11.0591 11.5413 11.1973 11.6589 11.3602 11.73ZM13.5502 14.8C13.5502 14.32 13.2203 14.03 12.7003 13.8V15.8C12.9387 15.7639 13.1561 15.6427 13.3123 15.459C13.4685 15.2752 13.553 15.0412 13.5502 14.8Z" fill="currentColor"/>'
    '<path d="M18 3.96997H6C4.93913 3.96997 3.92172 4.39146 3.17157 5.1416C2.42142 5.89175 2 6.9091 2 7.96997V17.97C2 19.0308 2.42142 20.0482 3.17157 20.7983C3.92172 21.5485 4.93913 21.97 6 21.97H18C19.0609 21.97 20.0783 21.5485 20.8284 20.7983C21.5786 20.0482 22 19.0308 22 17.97V7.96997C22 6.9091 21.5786 5.89175 20.8284 5.1416C20.0783 4.39146 19.0609 3.96997 18 3.96997Z" '
    'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
_ICONE_DATE = (
    '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">'
    '<path d="M20 10V7C20 5.89543 19.1046 5 18 5H6C4.89543 5 4 5.89543 4 7V10M20 10V19C20 20.1046 19.1046 21 18 21H6C4.89543 21 4 20.1046 4 19V10M20 10H4M8 3V7M16 3V7" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
    '<rect x="6" y="12" width="3" height="3" rx="0.5" fill="currentColor"/>'
    '<rect x="10.5" y="12" width="3" height="3" rx="0.5" fill="currentColor"/>'
    '<rect x="15" y="12" width="3" height="3" rx="0.5" fill="currentColor"/></svg>'
)
_ICONE_FILTER = (
    '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">'
    '<path d="M21 6H19M21 12H16M21 18H16M7 20V13.5612C7 13.3532 7 13.2492 6.97958 13.1497C6.96147 13.0615 6.93151 12.9761 6.89052 12.8958C6.84431 12.8054 6.77934 12.7242 6.64939 12.5617L3.35061 8.43826C3.22066 8.27583 3.15569 8.19461 3.10948 8.10417C3.06849 8.02393 3.03853 7.93852 3.02042 7.85026C3 7.75078 3 7.64677 3 7.43875V5.6C3 5.03995 3 4.75992 3.10899 4.54601C3.20487 4.35785 3.35785 4.20487 3.54601 4.10899C3.75992 4 4.03995 4 4.6 4H13.4C13.9601 4 14.2401 4 14.454 4.10899C14.6422 4.20487 14.7951 4.35785 14.891 4.54601C15 4.75992 15 5.03995 15 5.6V7.43875C15 7.64677 15 7.75078 14.9796 7.85026C14.9615 7.93852 14.9315 8.02393 14.8905 8.10417C14.8443 8.19461 14.7793 8.27583 14.6494 8.43826L11.3506 12.5617C11.2207 12.7242 11.1557 12.8054 11.1095 12.8958C11.0685 12.9761 11.0385 13.0615 11.0204 13.1497C11 13.2492 11 13.3532 11 13.5612V17L7 20Z" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

_FILTROS_HTML = """
<div id="cards-filtros">

  <div class="card-filtro recolhido" data-card="unidade">
    <div class="card-header">
      <button class="card-icone" type="button" title="Unidade de medida">__ICONE_RULER__</button>
      <span class="card-titulo">Unidade de medida</span>
      <button class="card-chevron" type="button">&#9662;</button>
    </div>
    <div class="card-corpo">
      <select id="select-unidade">
        <option value="quantidade">Quantidade</option>
        <option value="valor">Valor</option>
      </select>
    </div>
  </div>

  <div class="card-filtro recolhido" data-card="tipo">
    <div class="card-header">
      <button class="card-icone" type="button" title="Tipo">__ICONE_HOUSE__</button>
      <span class="card-titulo">Tipo</span>
      <button class="card-chevron" type="button">&#9662;</button>
    </div>
    <div class="card-corpo">
      <div class="secao-corpo" id="secao-tipo"></div>
    </div>
  </div>

  <div class="card-filtro recolhido" data-card="cidade">
    <div class="card-header">
      <button class="card-icone" type="button" title="Cidade">__ICONE_PIN__</button>
      <span class="card-titulo">Cidade</span>
      <button class="card-chevron" type="button">&#9662;</button>
    </div>
    <div class="card-corpo">
      <div class="secao-corpo" id="secao-cidade"></div>
    </div>
  </div>

  <div class="card-filtro recolhido" data-card="risco">
    <div class="card-header">
      <button class="card-icone" type="button" title="Area de risco">__ICONE_BULLET__</button>
      <span class="card-titulo">Area de risco</span>
      <button class="card-chevron" type="button">&#9662;</button>
    </div>
    <div class="card-corpo">
      <div class="secao-corpo" id="secao-risco">
        <div class="item-linha" data-campo="risco" data-valor="comum">
          <span class="item-label">Imoveis em area comum</span>
          <div class="item-direita">
            <span class="contagem"></span>
            <label class="switch"><input type="checkbox" checked data-chave="comum"></label>
          </div>
        </div>
        <div class="item-linha" data-campo="risco" data-valor="risco">
          <span class="item-label">Imoveis em area de risco</span>
          <div class="item-direita">
            <span class="contagem"></span>
            <label class="switch"><input type="checkbox" checked data-chave="risco"></label>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="card-filtro recolhido" data-card="valor">
    <div class="card-header">
      <button class="card-icone" type="button" title="Valor de avaliacao">__ICONE_MONEY__</button>
      <span class="card-titulo">Valor de avaliacao</span>
      <button class="card-chevron" type="button">&#9662;</button>
    </div>
    <div class="card-corpo">
      <div class="slider-valor">
        <div class="slider-track-wrap">
          <input type="range" id="slider-min">
          <input type="range" id="slider-max">
        </div>
        <div class="slider-labels">
          <span id="label-min"></span>
          <span id="label-max"></span>
        </div>
      </div>
    </div>
  </div>

  <div class="card-filtro recolhido" data-card="ano">
    <div class="card-header">
      <button class="card-icone" type="button" title="Ano de aquisicao">__ICONE_DATE__</button>
      <span class="card-titulo">Ano de aquisicao</span>
      <button class="card-chevron" type="button">&#9662;</button>
    </div>
    <div class="card-corpo">
      <div class="slider-valor">
        <div class="slider-track-wrap">
          <input type="range" id="slider-ano-min">
          <input type="range" id="slider-ano-max">
        </div>
        <div class="slider-labels">
          <span id="label-ano-min"></span>
          <span id="label-ano-max"></span>
        </div>
      </div>
    </div>
  </div>

  <div class="card-filtro recolhido" data-card="resumo">
    <div class="card-header">
      <button class="card-icone" type="button" title="Limpar filtros">__ICONE_FILTER__</button>
      <span class="card-titulo">Resultados</span>
      <button class="card-chevron" type="button">&#9662;</button>
    </div>
    <div class="card-corpo">
      <div id="contador-resultados"></div>
      <button id="limpar-filtros" type="button">Limpar filtros</button>
    </div>
  </div>

</div>

<div class="botoes-flutuantes">
  <a href="/upload" id="botao-upload" class="botao-flutuante">
    <span class="botao-tooltip">Atualizar planilha</span>
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
      <line x1="12" y1="19" x2="12" y2="5"></line>
      <polyline points="5 12 12 5 19 12"></polyline>
    </svg>
  </a>
  <button id="toggle-tema" type="button" class="botao-flutuante" title="Alternar tema claro/escuro">&#9788;</button>
</div>
"""

_FILTROS_HTML = (
    _FILTROS_HTML
    .replace("__ICONE_RULER__", _ICONE_RULER)
    .replace("__ICONE_HOUSE__", _ICONE_HOUSE)
    .replace("__ICONE_PIN__", _ICONE_PIN)
    .replace("__ICONE_BULLET__", _ICONE_BULLET)
    .replace("__ICONE_MONEY__", _ICONE_MONEY)
    .replace("__ICONE_DATE__", _ICONE_DATE)
    .replace("__ICONE_FILTER__", _ICONE_FILTER)
)

_FILTROS_JS = """
document.addEventListener('DOMContentLoaded', function() {
  var pontos = __DADOS_JSON__;
  var leafletMap = __MAP_NAME__;
  var marcadores = [];
  var estadoTipo = {};
  var estadoCidade = {};
  var estadoRisco = {comum: true, risco: true};
  var unidadeMedida = 'quantidade';

  var tileEscuro = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: 'abcd', maxZoom: 20, className: 'tiles-escuro'
  });
  var tileClaro = L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: 'abcd', maxZoom: 20
  });
  var tileAtual = null;

  function aplicarTema(escuro) {
    document.body.classList.toggle('tema-escuro', escuro);
    document.body.classList.toggle('tema-claro', !escuro);
    document.getElementById('toggle-tema').innerHTML = escuro ? '&#9788;' : '&#9789;';
    var novoTile = escuro ? tileEscuro : tileClaro;
    if (tileAtual !== novoTile) {
      if (tileAtual) leafletMap.removeLayer(tileAtual);
      novoTile.addTo(leafletMap);
      tileAtual = novoTile;
    }
    try { localStorage.setItem('mapaTema', escuro ? 'escuro' : 'claro'); } catch (e) {}
  }

  var temaSalvo = null;
  try { temaSalvo = localStorage.getItem('mapaTema'); } catch (e) {}
  aplicarTema(temaSalvo !== 'claro');

  document.getElementById('toggle-tema').addEventListener('click', function(ev) {
    ev.stopPropagation();
    aplicarTema(!document.body.classList.contains('tema-escuro'));
  });

  function formatarMoeda(v) {
    return v.toLocaleString('pt-BR', {style: 'currency', currency: 'BRL', maximumFractionDigits: 0});
  }

  function unicos(campo) {
    var vistos = {};
    var lista = [];
    pontos.forEach(function(p) {
      var v = p[campo];
      if (v && !vistos[v]) { vistos[v] = true; lista.push(v); }
    });
    lista.sort(function(a, b) { return a.localeCompare(b, 'pt-BR'); });
    return lista;
  }

  pontos.forEach(function(p) {
    var m = L.circleMarker([p.lat, p.lng], {
      radius: 8, color: p.cor, fillColor: p.cor, fillOpacity: 0.85, weight: 1
    }).bindPopup(p.popup, {maxWidth: 300}).bindTooltip(p.tooltip);
    m._dados = p;
    m.addTo(leafletMap);
    marcadores.push(m);
  });

  if (marcadores.length) {
    leafletMap.fitBounds(L.featureGroup(marcadores).getBounds(), {padding: [40, 40], maxZoom: 12});
  }

  function criarLinha(rotulo, cor, chave, estadoObj, campo) {
    var linha = document.createElement('div');
    linha.className = 'item-linha';
    linha.setAttribute('data-campo', campo);
    linha.setAttribute('data-valor', chave);
    var label = document.createElement('span');
    label.className = 'item-label';
    if (cor) {
      var sw = document.createElement('span');
      sw.className = 'swatch';
      sw.style.background = cor;
      label.appendChild(sw);
    }
    var texto = document.createElement('span');
    texto.textContent = rotulo;
    label.appendChild(texto);
    linha.appendChild(label);

    var direita = document.createElement('div');
    direita.className = 'item-direita';
    var badge = document.createElement('span');
    badge.className = 'contagem';
    direita.appendChild(badge);

    var switchLabel = document.createElement('label');
    switchLabel.className = 'switch';
    var input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = true;
    input.addEventListener('change', function() {
      estadoObj[chave] = input.checked;
      aplicarFiltros();
    });
    switchLabel.appendChild(input);
    direita.appendChild(switchLabel);
    linha.appendChild(direita);
    return linha;
  }

  var elSecaoTipo = document.getElementById('secao-tipo');
  unicos('tipo').forEach(function(tipo) {
    estadoTipo[tipo] = true;
    elSecaoTipo.appendChild(criarLinha(tipo, null, tipo, estadoTipo, 'tipo'));
  });

  var elSecaoCidade = document.getElementById('secao-cidade');
  unicos('cidade').forEach(function(cidade) {
    estadoCidade[cidade] = true;
    elSecaoCidade.appendChild(criarLinha(cidade, null, cidade, estadoCidade, 'cidade'));
  });

  document.querySelectorAll('#secao-risco input[type=checkbox]').forEach(function(input) {
    var chave = input.getAttribute('data-chave');
    input.addEventListener('change', function() {
      estadoRisco[chave] = input.checked;
      aplicarFiltros();
    });
  });

  document.querySelectorAll('.card-filtro[data-card]').forEach(function(card) {
    var botaoIcone = card.querySelector('.card-icone');
    var titulo = card.querySelector('.card-titulo');
    var chevron = card.querySelector('.card-chevron');

    function alternarColapso() { card.classList.toggle('recolhido'); }
    if (botaoIcone) botaoIcone.addEventListener('click', alternarColapso);
    if (titulo) titulo.addEventListener('click', alternarColapso);
    if (chevron) chevron.addEventListener('click', alternarColapso);
  });

  var valores = pontos.map(function(p) { return p.valor; });
  var valorMin = valores.length ? Math.min.apply(null, valores) : 0;
  var valorMax = valores.length ? Math.max.apply(null, valores) : 0;
  var sliderMin = document.getElementById('slider-min');
  var sliderMax = document.getElementById('slider-max');
  [sliderMin, sliderMax].forEach(function(s) {
    s.min = valorMin; s.max = valorMax;
    s.step = 'any';
  });
  sliderMin.value = valorMin;
  sliderMax.value = valorMax;

  function atualizarLabelsSlider() {
    document.getElementById('label-min').textContent = formatarMoeda(parseFloat(sliderMin.value));
    document.getElementById('label-max').textContent = formatarMoeda(parseFloat(sliderMax.value));
  }
  atualizarLabelsSlider();

  [sliderMin, sliderMax].forEach(function(s) {
    s.addEventListener('input', function() {
      if (parseFloat(sliderMin.value) > parseFloat(sliderMax.value)) {
        var tmp = sliderMin.value; sliderMin.value = sliderMax.value; sliderMax.value = tmp;
      }
      atualizarLabelsSlider();
      aplicarFiltros();
    });
  });

  var anos = pontos.map(function(p) { return p.ano; }).filter(function(a) { return a != null; });
  var anoMin = anos.length ? Math.min.apply(null, anos) : 0;
  var anoMax = anos.length ? Math.max.apply(null, anos) : 0;
  var sliderAnoMin = document.getElementById('slider-ano-min');
  var sliderAnoMax = document.getElementById('slider-ano-max');
  [sliderAnoMin, sliderAnoMax].forEach(function(s) {
    s.min = anoMin; s.max = anoMax;
    s.step = 'any';
  });
  sliderAnoMin.value = anoMin;
  sliderAnoMax.value = anoMax;

  function atualizarLabelsAno() {
    document.getElementById('label-ano-min').textContent = Math.round(parseFloat(sliderAnoMin.value));
    document.getElementById('label-ano-max').textContent = Math.round(parseFloat(sliderAnoMax.value));
  }
  atualizarLabelsAno();

  [sliderAnoMin, sliderAnoMax].forEach(function(s) {
    s.addEventListener('input', function() {
      if (parseFloat(sliderAnoMin.value) > parseFloat(sliderAnoMax.value)) {
        var tmp = sliderAnoMin.value; sliderAnoMin.value = sliderAnoMax.value; sliderAnoMax.value = tmp;
      }
      atualizarLabelsAno();
      aplicarFiltros();
    });
  });

  function passaFiltros(p, ignorar) {
    if (ignorar !== 'tipo' && estadoTipo[p.tipo] === false) return false;
    if (ignorar !== 'cidade' && estadoCidade[p.cidade] === false) return false;
    if (ignorar !== 'risco') {
      if (p.violencia && !estadoRisco.risco) return false;
      if (!p.violencia && !estadoRisco.comum) return false;
    }
    var vMin = parseFloat(sliderMin.value);
    var vMax = parseFloat(sliderMax.value);
    if (p.valor < vMin || p.valor > vMax) return false;
    if (p.ano != null) {
      var aMin = parseFloat(sliderAnoMin.value);
      var aMax = parseFloat(sliderAnoMax.value);
      if (p.ano < aMin || p.ano > aMax) return false;
    }
    return true;
  }

  function agregar(lista) {
    if (unidadeMedida === 'valor') {
      var soma = lista.reduce(function(acc, p) { return acc + p.valor; }, 0);
      return formatarMoeda(soma);
    }
    return String(lista.length);
  }

  function atualizarContagens() {
    document.querySelectorAll('.item-linha[data-campo]').forEach(function(linha) {
      var campo = linha.getAttribute('data-campo');
      var valor = linha.getAttribute('data-valor');
      var badge = linha.querySelector('.contagem');
      var lista;
      if (campo === 'risco') {
        var eRisco = valor === 'risco';
        lista = pontos.filter(function(p) {
          return (p.violencia === eRisco) && passaFiltros(p, 'risco');
        });
      } else {
        lista = pontos.filter(function(p) {
          return p[campo] === valor && passaFiltros(p, campo);
        });
      }
      badge.textContent = agregar(lista);
    });
  }

  function aplicarFiltros() {
    var contador = 0;
    marcadores.forEach(function(m) {
      var p = m._dados;
      var ok = passaFiltros(p, null);

      var presente = leafletMap.hasLayer(m);
      if (ok) { if (!presente) m.addTo(leafletMap); contador++; }
      else if (presente) { leafletMap.removeLayer(m); }
    });
    document.getElementById('contador-resultados').textContent =
      contador + ' de ' + marcadores.length + ' imoveis';
    atualizarContagens();
  }

  document.getElementById('select-unidade').addEventListener('change', function(ev) {
    unidadeMedida = ev.target.value;
    atualizarContagens();
  });

  document.getElementById('limpar-filtros').addEventListener('click', function() {
    document.querySelectorAll('#secao-tipo input[type=checkbox], #secao-cidade input[type=checkbox]')
      .forEach(function(input) { input.checked = true; });
    Object.keys(estadoTipo).forEach(function(k) { estadoTipo[k] = true; });
    Object.keys(estadoCidade).forEach(function(k) { estadoCidade[k] = true; });

    estadoRisco.comum = true;
    estadoRisco.risco = true;
    document.querySelectorAll('#secao-risco input[type=checkbox]')
      .forEach(function(input) { input.checked = true; });

    sliderMin.value = valorMin;
    sliderMax.value = valorMax;
    atualizarLabelsSlider();

    sliderAnoMin.value = anoMin;
    sliderAnoMax.value = anoMax;
    atualizarLabelsAno();

    aplicarFiltros();
  });

  aplicarFiltros();
});
"""
