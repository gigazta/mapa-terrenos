# Mapa de Imóveis

Aplicação web para visualizar uma carteira de imóveis num mapa interativo, com
filtros por tipo, cidade, área de risco, valor e ano de aquisição. Os dados
vêm de uma planilha (CSV ou Excel) que pode ser atualizada a qualquer momento
direto pelo navegador, sem precisar mexer em código ou reiniciar o servidor.

## Funcionalidades

- Mapa interativo (Leaflet/OpenStreetMap) com um marcador por imóvel, colorido
  por área de risco (azul = área comum, vermelho = área de risco).
- Painel de filtros em cards colapsáveis: tipo, cidade, área de risco, valor
  de avaliação (slider) e ano de aquisição (slider), cada um mostrando quantos
  imóveis atendem ao filtro (ou a soma dos valores, à escolha).
- Modo escuro/claro.
- Upload de planilha (`.csv` ou `.xlsx`) direto pelo navegador — geocodifica
  automaticamente endereços sem latitude/longitude preenchida.
- Download da planilha atualmente em uso, com o nome original preservado.
- Autenticação HTTP Basic opcional (usuário/senha via variável de ambiente).

## Como rodar

Requer [Docker](https://docs.docker.com/get-docker/) instalado no servidor.

```bash
git clone https://github.com/gigazta/mapa-terrenos.git
cd mapa-terrenos
docker compose up -d --build
```

O mapa fica disponível em `http://<ip-do-servidor>:8080`. Qualquer estação de
trabalho na mesma rede consegue acessar pelo navegador usando esse endereço.

Na primeira vez, acesse `http://<ip-do-servidor>:8080/upload` para enviar a
planilha de imóveis. O mapa é gerado automaticamente a partir dela.

## Configuração

Variáveis de ambiente (definidas no `docker-compose.yml`):

| Variável | Padrão | Descrição |
|---|---|---|
| `MAPA_AUTH_USER` | *(vazio)* | Usuário para autenticação HTTP Basic. Se vazio, o app fica sem autenticação. |
| `MAPA_AUTH_PASS` | *(vazio)* | Senha correspondente ao usuário acima. |
| `MAPA_DATA_DIR` | `/data` | Diretório onde a planilha, o cache de geocodificação e o mapa gerado ficam salvos. Já vem mapeado para um volume Docker persistente. |

Para trocar a porta exposta, edite a linha `"8080:5000"` no
`docker-compose.yml` (o primeiro número é a porta no host).

**Importante:** troque `MAPA_AUTH_PASS` no `docker-compose.yml` antes de subir
em produção — o valor padrão do repositório é só um placeholder.

## Formato da planilha

A planilha deve ter uma linha de cabeçalho com (pelo menos) estas colunas:

| Coluna | Obrigatória | Descrição |
|---|---|---|
| `tipo` | não | Categoria do imóvel (usada nos filtros) |
| `descricao` | não | Descrição curta, aparece no popup e na lista de filtros |
| `endereco` | **sim** | Endereço completo — usado para geocodificação quando falta lat/long |
| `cidade` | não | Se ausente, é extraída automaticamente do campo `endereco` (heurística) |
| `lat`, `long` | não | Coordenadas. Se vazias, são geocodificadas automaticamente a partir do endereço |
| `situacao-atu` | não | Situação atual/jurídica, aparece no popup |
| `valor-cont` | não | Valor contábil |
| `valor-aval` | não | Valor de avaliação |
| `aquisicao` | não | Ano de aquisição (usado no filtro de ano) |
| `violencia` | não | `TRUE`/`FALSE` — indica área de risco (marcador fica vermelho) |

Outras colunas (`cartorio`, `rgi`, `insc-municipal`, `ci`, `observacoes`) são
lidas e exibidas no popup do imóvel, mas não afetam os filtros.

Limite: 5000 linhas e 5 MB por upload.

## Persistência de dados

A planilha enviada, o cache de geocodificação e o `index.html` gerado ficam
no volume Docker `mapa_data`, então sobrevivem a reinicializações do
container. Pra fazer backup, basta copiar o conteúdo desse volume.

## Desenvolvimento local (sem Docker)

```bash
cd app
pip install -r requirements.txt
python app.py
```

O servidor de desenvolvimento sobe em `http://127.0.0.1:5000`. Não use esse
modo em produção — o `docker-compose.yml` já roda via Gunicorn.
