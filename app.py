import csv
import hashlib
import io
import json
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
import streamlit as st

ARQUIVO_JSON_LOCAL = Path(__file__).resolve().parent / "financas_casal.json"

st.set_page_config(page_title="Finanças do casal", page_icon="💶", layout="wide")
st.markdown(
    """
    <style>
    div[class*="st-key-remover_categoria_"] button:hover,
    div[class*="st-key-remover_descricao_"] button:hover {
        background: #c62828;
        border-color: #c62828;
        color: white;
    }
    div[class*="st-key-adicionar_descricao_"] button:hover,
    div[class*="st-key-adicionar_categoria"] button:hover {
        background: #218739;
        border-color: #218739;
        color: white;
    }
    </style>
    """,
    unsafe_allow_html=True,
)
st.title("💶 Finanças do casal")
st.caption("Importa movimentos bancários, classifica-os e acompanha as despesas ao longo do tempo.")

CATEGORIAS = ["Rendimentos", "Casa", "Despesas do dia a dia", "Despesas pessoais", "Poupanças", "Transferências", "Outros"]
DESCRICOES_INICIAIS = [
    ("Rendimentos", "Ordenado Miguel"), ("Rendimentos", "Ordenado Júlia"), ("Rendimentos", "Outros rendimentos"), ("Rendimentos", "Reembolsos"),
    ("Casa", "Crédito habitação"), ("Casa", "Seguro de vida"), ("Casa", "Seguro multirriscos"), ("Casa", "Manutenção da conta"), ("Casa", "Condomínio"), ("Casa", "IMI"), ("Casa", "Mobiliário e equipamento"), ("Casa", "Manutenção e reparações"),
    ("Despesas do dia a dia", "Eletricidade"), ("Despesas do dia a dia", "Água"), ("Despesas do dia a dia", "Gás"), ("Despesas do dia a dia", "Internet"), ("Despesas do dia a dia", "Telecomunicações"), ("Despesas do dia a dia", "Supermercado"), ("Despesas do dia a dia", "Restaurantes e takeaway"), ("Despesas do dia a dia", "Transportes"), ("Despesas do dia a dia", "Saúde"),
    ("Despesas pessoais", "Transferência para Miguel"), ("Despesas pessoais", "Transferência para Júlia"), ("Poupanças", "Transferência para poupança"), ("Transferências", "Transferência entre contas"), ("Outros", "Por classificar"),
]


def estado_novo():
    return {
        "version": 1,
        "movimentos": [],
        "regras": [],
        "categorias": CATEGORIAS.copy(),
        "descricoes": [{"categoria": c, "nome": n} for c, n in DESCRICOES_INICIAIS],
        "proximo_id_movimento": 1,
        "proximo_id_regra": 1,
    }


def carregar_json_local():
    if not ARQUIVO_JSON_LOCAL.exists():
        return estado_novo(), "Sessão nova (sem histórico carregado)"
    try:
        with ARQUIVO_JSON_LOCAL.open("r", encoding="utf-8-sig") as ficheiro:
            dados = json.load(ficheiro)
        if not isinstance(dados, dict) or not all(k in dados for k in ("movimentos", "regras", "descricoes")):
            raise ValueError("JSON sem estrutura válida.")
        base = estado_novo()
        base.update(dados)
        base["movimentos"] = list(base.get("movimentos", []))
        base["regras"] = list(base.get("regras", []))
        categorias_guardadas = dados.get("categorias", CATEGORIAS)
        base["categorias"] = list(dict.fromkeys(c for c in categorias_guardadas if isinstance(c, str) and c.strip())) or CATEGORIAS.copy()
        base["descricoes"] = list(base.get("descricoes", []))
        base["proximo_id_movimento"] = max([int(m.get("id", 0)) for m in base["movimentos"]] + [0]) + 1
        base["proximo_id_regra"] = max([int(r.get("id", 0)) for r in base["regras"]] + [0]) + 1
        return base, ARQUIVO_JSON_LOCAL.name
    except Exception:
        return estado_novo(), "Sessão nova (sem histórico carregado)"


def guardar_json_local():
    if "dados" not in st.session_state:
        return False
    try:
        ARQUIVO_JSON_LOCAL.write_text(json.dumps(st.session_state.dados, ensure_ascii=False, indent=2), encoding="utf-8")
        st.session_state.nome_json = ARQUIVO_JSON_LOCAL.name
        return True
    except Exception:
        return False


if "dados" not in st.session_state:
    st.session_state.dados, nome_carregado = carregar_json_local()
    st.session_state.nome_json = nome_carregado
if "nome_json" not in st.session_state:
    st.session_state.nome_json = "Sessão nova (sem histórico carregado)"
if "json_upload_hash" not in st.session_state:
    st.session_state.json_upload_hash = None
if "extrato_upload_hash" not in st.session_state:
    st.session_state.extrato_upload_hash = None
if "pagina_atual" not in st.session_state:
    st.session_state.pagina_atual = "Resumo mensal"
if "categoria_descricao_aberta" not in st.session_state:
    st.session_state.categoria_descricao_aberta = None


def abrir_importacao():
    st.session_state.pagina_atual = "Importar histórico e novos movimentos"


def navegar_para(pagina):
    st.session_state.pagina_atual = pagina


def alternar_adicao_descricao(categoria):
    atual = st.session_state.categoria_descricao_aberta
    st.session_state.categoria_descricao_aberta = None if atual == categoria else categoria


def normalizar_nome(nome):
    texto = unicodedata.normalize("NFKD", str(nome).strip().lower())
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return "".join(ch for ch in texto if ch.isalnum())


def valor_monetario(valor):
    if valor is None:
        return None
    texto = str(valor).strip().replace("\u00a0", "").replace("EUR", "").replace("€", "").replace(" ", "")
    if not texto:
        return None
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return float(Decimal(texto))
    except (InvalidOperation, ValueError):
        return None


def data_iso(valor):
    if not valor:
        return None
    texto = str(valor).strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(texto, fmt).date().isoformat()
        except ValueError:
            pass
    return texto


def fingerprint_movimento(data, descricao, montante, saldo, conta):
    base = "|".join([str(data or ""), str(descricao or "").strip().lower(), f"{float(montante):.2f}", "" if saldo is None else f"{float(saldo):.2f}", str(conta or "Conta conjunta").strip().lower()])
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def mapear_colunas(cabecalhos):
    aliases = {
        "data": ["datamov", "datamovimento", "data", "date", "bookingdate"],
        "data_valor": ["datavalor", "valuedate", "dataval"],
        "descricao": ["descricao", "descricaomovimento", "description", "detalhe", "details", "memo"],
        "montante": ["montante", "valor", "amount", "value", "creditdebitamount"],
        "saldo": ["saldocontabilisticoaposmovimento", "saldocontabilistico", "saldo", "balance", "runningbalance"],
        "conta": ["conta", "account", "accountname"],
    }
    normalizados = {normalizar_nome(c): c for c in cabecalhos}
    resultado = {}
    for campo, opcoes in aliases.items():
        for opcao in opcoes:
            if opcao in normalizados:
                resultado[campo] = normalizados[opcao]
                break
    return resultado


def ler_csv(conteudo_bytes):
    texto = None
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            texto = conteudo_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if texto is None:
        raise ValueError("Não foi possível descodificar o ficheiro.")
    linhas = texto.splitlines()
    indice_cabecalho = None
    delimitador = ";"
    for i, linha in enumerate(linhas):
        low = linha.lower()
        if "data mov." in low and "montante" in low and ("descrição" in low or "descricao" in low):
            indice_cabecalho = i
            delimitador = ";" if linha.count(";") >= linha.count(",") else ","
            break
    if indice_cabecalho is not None:
        return list(csv.DictReader(linhas[indice_cabecalho:], delimiter=delimitador))
    amostra = "\n".join(linhas[:10])
    try:
        delimitador = csv.Sniffer().sniff(amostra, delimiters=";,\t|").delimiter
    except csv.Error:
        delimitador = ";"
    return list(csv.DictReader(io.StringIO(texto), delimiter=delimitador))


def ler_xml(conteudo_bytes):
    raiz = ET.fromstring(conteudo_bytes)
    movimentos = []
    nomes_data = {"date", "bookingdate", "datamov", "datamovimento", "data"}
    nomes_valor = {"amount", "value", "montante", "valor", "creditdebitamount"}
    nomes_desc = {"description", "descricao", "remittanceinformation", "details", "memo"}
    nomes_saldo = {"balance", "saldo", "saldocontabilistico", "runningbalance"}
    nomes_data_valor = {"valuedate", "datavalor", "dataval"}
    for no in raiz.iter():
        filhos = list(no.iter())
        if len(filhos) < 3:
            continue
        campos = {}
        for filho in filhos:
            chave = normalizar_nome(filho.tag.split("}")[-1])
            valor = (filho.text or "").strip()
            if not valor:
                continue
            if chave in nomes_data and "data" not in campos: campos["data"] = valor
            elif chave in nomes_data_valor and "data_valor" not in campos: campos["data_valor"] = valor
            elif chave in nomes_valor and "montante" not in campos: campos["montante"] = valor
            elif chave in nomes_desc and "descricao" not in campos: campos["descricao"] = valor
            elif chave in nomes_saldo and "saldo" not in campos: campos["saldo"] = valor
        if "data" in campos and "montante" in campos and "descricao" in campos and valor_monetario(campos["montante"]) is not None:
            movimentos.append({"Data mov.": campos["data"], "Data-valor": campos.get("data_valor", ""), "Descrição": campos["descricao"], "Montante": campos["montante"], "Saldo contabilístico após movimento": campos.get("saldo", "")})
    unicos = {}
    for m in movimentos:
        unicos[(m.get("Data mov."), m.get("Descrição"), m.get("Montante"), m.get("Saldo contabilístico após movimento"))] = m
    if not unicos:
        raise ValueError("Não consegui identificar movimentos neste XML. O banco pode usar um formato que exige um leitor específico.")
    return list(unicos.values())


def classificar(descricao):
    regra = regra_correspondente(descricao)
    if regra:
        return regra["categoria"], regra["descricao_personalizada"], "Automática"
    categoria_padrao = "Outros" if "Outros" in categorias_atuais() else categorias_atuais()[0]
    return categoria_padrao, "Por classificar", "Por classificar"


def regra_correspondente(descricao):
    texto = str(descricao or "").casefold()
    regras = sorted(
        [r for r in st.session_state.dados["regras"] if r.get("ativa", True)],
        key=lambda r: len(r.get("texto_contem", "")),
        reverse=True,
    )
    return next((r for r in regras if r.get("texto_contem", "").casefold() in texto), None)


def categorias_atuais():
    return st.session_state.dados.get("categorias", CATEGORIAS)


def reclassificar_movimentos_automaticos(textos_relevantes=None):
    textos_relevantes = [texto.casefold() for texto in (textos_relevantes or []) if texto]
    for movimento in st.session_state.dados["movimentos"]:
        descricao_original = str(movimento.get("descricao_original", ""))
        associado_a_regra = movimento.get("regra_id") is not None
        corresponde_texto = any(texto in descricao_original.casefold() for texto in textos_relevantes)
        if movimento.get("estado") not in ("Automática", "Por classificar") and not associado_a_regra and not corresponde_texto:
            continue
        categoria, descricao, estado = classificar(movimento.get("descricao_original"))
        regra = regra_correspondente(movimento.get("descricao_original"))
        movimento.update({
            "categoria": categoria,
            "descricao_personalizada": descricao,
            "estado": estado,
            "regra_id": regra.get("id") if regra else None,
        })
        st.session_state.pop(f"categoria_movimento_edicao_{movimento['id']}", None)
        st.session_state.pop(f"descricao_movimento_edicao_{movimento['id']}", None)


def adicionar_categoria(nome):
    nome = str(nome).strip()
    if not nome or any(c.casefold() == nome.casefold() for c in categorias_atuais()):
        return False
    st.session_state.dados["categorias"].append(nome)
    guardar_json_local()
    return True


def remover_categoria(categoria):
    if categoria not in categorias_atuais() or len(categorias_atuais()) <= 1:
        return
    st.session_state.dados["categorias"].remove(categoria)
    st.session_state.dados["descricoes"] = [d for d in st.session_state.dados["descricoes"] if d["categoria"] != categoria]
    st.session_state.dados["regras"] = [r for r in st.session_state.dados["regras"] if r.get("categoria") != categoria]
    for movimento in st.session_state.dados["movimentos"]:
        if movimento.get("categoria") == categoria:
            movimento.update({"categoria": "Outros" if "Outros" in categorias_atuais() else categorias_atuais()[0], "descricao_personalizada": "Por classificar", "estado": "Por classificar"})
    reclassificar_movimentos_automaticos()
    guardar_json_local()


def remover_descricao(categoria, nome):
    st.session_state.dados["descricoes"] = [
        d for d in st.session_state.dados["descricoes"]
        if not (d["categoria"] == categoria and d["nome"].casefold() == nome.casefold())
    ]
    for regra in st.session_state.dados["regras"]:
        if regra.get("categoria") == categoria and regra.get("descricao_personalizada", "").casefold() == nome.casefold():
            regra["descricao_personalizada"] = "Por classificar"
    for movimento in st.session_state.dados["movimentos"]:
        if movimento.get("categoria") == categoria and movimento.get("descricao_personalizada", "").casefold() == nome.casefold():
            movimento["descricao_personalizada"] = "Por classificar"
            if movimento.get("estado") == "Automática":
                movimento["estado"] = "Por classificar"
    reclassificar_movimentos_automaticos()
    guardar_json_local()


def guardar_descricao(categoria, nome):
    nome = str(nome).strip()
    if nome and not any(d["categoria"] == categoria and d["nome"].casefold() == nome.casefold() for d in st.session_state.dados["descricoes"]):
        st.session_state.dados["descricoes"].append({"categoria": categoria, "nome": nome})
        guardar_json_local()


def descricoes_por_categoria(categoria):
    return sorted([d["nome"] for d in st.session_state.dados["descricoes"] if d["categoria"] == categoria], key=str.casefold)


def importar_movimentos(registos, origem, conta):
    dados = st.session_state.dados
    fingerprints = {m.get("fingerprint") for m in dados["movimentos"]}
    inseridos = ignorados = 0
    for registo in registos:
        mapa = mapear_colunas(list(registo.keys()))
        def valor(campo):
            coluna = mapa.get(campo)
            return registo.get(coluna) if coluna else None
        data = data_iso(valor("data"))
        data_valor = data_iso(valor("data_valor"))
        descricao = valor("descricao")
        montante = valor_monetario(valor("montante"))
        saldo = valor_monetario(valor("saldo"))
        conta_registo = str(valor("conta") or conta or "Conta conjunta").strip()
        if not descricao or montante is None:
            continue
        fp = fingerprint_movimento(data, descricao, montante, saldo, conta_registo)
        if fp in fingerprints:
            ignorados += 1
            continue
        categoria, desc_personalizada, estado = classificar(descricao)
        regra = regra_correspondente(descricao)
        movimento = {"id": dados["proximo_id_movimento"], "data_movimento": data, "data_valor": data_valor, "descricao_original": str(descricao).strip(), "montante": montante, "saldo": saldo, "conta": conta_registo, "categoria": categoria, "descricao_personalizada": desc_personalizada, "estado": estado, "regra_id": regra.get("id") if regra else None, "origem_ficheiro": origem, "fingerprint": fp}
        dados["movimentos"].append(movimento)
        dados["proximo_id_movimento"] += 1
        fingerprints.add(fp)
        inseridos += 1
    guardar_json_local()
    return inseridos, ignorados


def carregar_movimentos():
    colunas = ["id", "data_movimento", "data_valor", "descricao_original", "montante", "saldo", "conta", "categoria", "descricao_personalizada", "estado", "origem_ficheiro", "fingerprint"]
    return pd.DataFrame(st.session_state.dados["movimentos"], columns=colunas).sort_values(["data_movimento", "id"], ascending=[False, False], na_position="last") if st.session_state.dados["movimentos"] else pd.DataFrame(columns=colunas)


def aplicar_classificacao(movimento_id, categoria, descricao_personalizada, criar_regra, texto_regra):
    guardar_descricao(categoria, descricao_personalizada)
    for movimento in st.session_state.dados["movimentos"]:
        if movimento["id"] == movimento_id:
            movimento["categoria"] = categoria
            movimento["descricao_personalizada"] = descricao_personalizada
            movimento["estado"] = "Manual"
            movimento["regra_id"] = None
            break
    if criar_regra and texto_regra.strip():
        existente = next((r for r in st.session_state.dados["regras"] if r["texto_contem"].casefold() == texto_regra.strip().casefold()), None)
        if existente:
            existente.update({"categoria": categoria, "descricao_personalizada": descricao_personalizada, "ativa": True})
        else:
            st.session_state.dados["regras"].append({"id": st.session_state.dados["proximo_id_regra"], "texto_contem": texto_regra.strip(), "categoria": categoria, "descricao_personalizada": descricao_personalizada, "ativa": True})
            st.session_state.dados["proximo_id_regra"] += 1
            existente = st.session_state.dados["regras"][-1]
        for movimento in st.session_state.dados["movimentos"]:
            if movimento["id"] == movimento_id:
                movimento["regra_id"] = existente["id"]
                break
        reclassificar_movimentos_automaticos([texto_regra.strip()])
    guardar_json_local()

st.sidebar.subheader("Importação de histórico e novos movimentos")
if ARQUIVO_JSON_LOCAL.exists() and st.session_state.nome_json == ARQUIVO_JSON_LOCAL.name:
    st.sidebar.success(f"Histórico ativo: {st.session_state.nome_json}")
else:
    st.sidebar.info("Sem histórico local carregado")
pagina_importacao = "Importar histórico e novos movimentos"
st.sidebar.button(
    "Abrir importação",
    type="primary" if st.session_state.pagina_atual == pagina_importacao else "secondary",
    use_container_width=True,
    on_click=abrir_importacao,
)
st.sidebar.divider()
st.sidebar.subheader("Navegação")
for pagina_navegacao in ("Resumo mensal", "Movimentos e regras", "Categorias e descrições"):
    st.sidebar.button(
        pagina_navegacao,
        type="primary" if st.session_state.pagina_atual == pagina_navegacao else "secondary",
        use_container_width=True,
        key=f"navegar_{normalizar_nome(pagina_navegacao)}",
        on_click=navegar_para,
        args=(pagina_navegacao,),
    )
pagina = st.session_state.pagina_atual

if pagina == "Importar histórico e novos movimentos":
    st.header("Importar histórico e novos movimentos")
    if ARQUIVO_JSON_LOCAL.exists() and st.session_state.nome_json == ARQUIVO_JSON_LOCAL.name:
        st.success(f"Histórico pronto a usar: {ARQUIVO_JSON_LOCAL.name}. Os novos movimentos serão acrescentados a este histórico.")
    elif ARQUIVO_JSON_LOCAL.exists():
        st.warning(f"Foi encontrado {ARQUIVO_JSON_LOCAL.name}, mas não foi possível carregá-lo. Podes selecionar um JSON válido abaixo.")
    else:
        st.info("Ainda não existe histórico local. Importa um JSON ou começa por importar movimentos bancários.")

    st.subheader("Importar histórico JSON")
    json_upload = st.file_uploader("Carregar ou substituir histórico JSON", type=["json"], key="json_upload")
    if json_upload is not None:
        conteudo_json = json_upload.getvalue()
        hash_json = hashlib.sha256(conteudo_json).hexdigest()
        if hash_json != st.session_state.json_upload_hash:
            try:
                candidato = json.loads(conteudo_json.decode("utf-8-sig"))
                if not isinstance(candidato, dict) or not all(k in candidato for k in ("movimentos", "regras", "descricoes")):
                    raise ValueError("O JSON não tem a estrutura esperada da aplicação.")
                base = estado_novo()
                base.update(candidato)
                base["movimentos"] = list(base.get("movimentos", []))
                base["regras"] = list(base.get("regras", []))
                categorias_guardadas = candidato.get("categorias", CATEGORIAS)
                base["categorias"] = list(dict.fromkeys(c for c in categorias_guardadas if isinstance(c, str) and c.strip())) or CATEGORIAS.copy()
                base["descricoes"] = list(base.get("descricoes", []))
                base["proximo_id_movimento"] = max([int(m.get("id", 0)) for m in base["movimentos"]] + [0]) + 1
                base["proximo_id_regra"] = max([int(r.get("id", 0)) for r in base["regras"]] + [0]) + 1
                st.session_state.dados = base
                st.session_state.nome_json = json_upload.name
                if guardar_json_local():
                    st.session_state.json_upload_hash = hash_json
                    st.session_state.json_upload_status = f"Histórico carregado e guardado: {json_upload.name}"
                else:
                    st.session_state.json_upload_status = "O histórico foi carregado nesta sessão, mas não foi possível guardá-lo localmente."
            except Exception as exc:
                st.session_state.json_upload_status = f"Não foi possível carregar o JSON: {exc}"
        if st.session_state.get("json_upload_status", "").startswith("Histórico carregado"):
            st.success(st.session_state.json_upload_status)
        elif st.session_state.get("json_upload_status"):
            st.error(st.session_state.json_upload_status)

    st.download_button("Descarregar histórico JSON", data=json.dumps(st.session_state.dados, ensure_ascii=False, indent=2).encode("utf-8"), file_name="financas_casal.json", mime="application/json")
    st.divider()
    st.subheader("Importar Novos Movimentos")
    ficheiro = st.file_uploader("Seleciona um extrato CSV ou XML", type=["csv", "xml", "txt"], key="extrato_upload")
    if ficheiro is not None:
        conteudo_extrato = ficheiro.getvalue()
        hash_extrato = hashlib.sha256(conteudo_extrato).hexdigest()
        if hash_extrato != st.session_state.extrato_upload_hash:
            try:
                registos = ler_xml(conteudo_extrato) if ficheiro.name.lower().endswith(".xml") else ler_csv(conteudo_extrato)
                if not registos:
                    st.session_state.extrato_upload_status = "Não encontrei linhas de movimentos no ficheiro."
                else:
                    inseridos, ignorados = importar_movimentos(registos, ficheiro.name, "Conta conjunta")
                    st.session_state.extrato_upload_status = f"{ficheiro.name}: {inseridos} movimentos novos; {ignorados} duplicados ignorados. Histórico guardado localmente."
                st.session_state.extrato_upload_hash = hash_extrato
            except Exception as exc:
                st.session_state.extrato_upload_status = f"Não foi possível importar o ficheiro: {exc}"
                st.session_state.extrato_upload_hash = hash_extrato
        if st.session_state.get("extrato_upload_status", "").startswith("Não foi possível") or st.session_state.get("extrato_upload_status", "").startswith("Não encontrei"):
            st.warning(st.session_state.extrato_upload_status)
        elif st.session_state.get("extrato_upload_status"):
            st.success(st.session_state.extrato_upload_status)

elif pagina == "Movimentos e regras":
    st.header("Movimentos bancários")
    df = carregar_movimentos()
    if df.empty:
        st.info("Ainda não existem movimentos. Começa por importar um ficheiro.")
    else:
        col1, col2, col3 = st.columns(3)
        categoria_filtro = col1.selectbox("Filtrar por categoria", ["Todas"] + sorted(df["categoria"].dropna().unique().tolist()))
        pesquisa = col2.text_input("Pesquisar descrição")
        estado_filtro = col3.selectbox("Estado da classificação", ["Todos"] + sorted(df["estado"].dropna().unique().tolist()))
        filtrado = df.copy()
        if categoria_filtro != "Todas": filtrado = filtrado[filtrado["categoria"] == categoria_filtro]
        if pesquisa: filtrado = filtrado[filtrado["descricao_original"].str.contains(pesquisa, case=False, na=False)]
        if estado_filtro != "Todos": filtrado = filtrado[filtrado["estado"] == estado_filtro]
        movimentos_tabela = filtrado[["id", "data_movimento", "descricao_original", "montante", "categoria", "descricao_personalizada", "estado"]]
        movimentos_tabela = movimentos_tabela.rename(columns={"id": "ID", "data_movimento": "Data", "descricao_original": "Nome", "montante": "Montante", "categoria": "Categoria", "descricao_personalizada": "Descrição", "estado": "Estado"})
        st.dataframe(movimentos_tabela, use_container_width=True, hide_index=True)
        st.subheader("Editar classificação de um movimento")
        ids = filtrado["id"].tolist()
        if ids:
            escolha_id = st.selectbox("Escolhe o movimento", ids, format_func=lambda x: f"#{x} | {filtrado.loc[filtrado['id'] == x, 'data_movimento'].iloc[0]} | {filtrado.loc[filtrado['id'] == x, 'descricao_original'].iloc[0]}")
            movimento = df.loc[df["id"] == escolha_id].iloc[0]
            categorias = categorias_atuais()
            categoria_nova = st.selectbox("Categoria principal", categorias, index=categorias.index(movimento["categoria"]) if movimento["categoria"] in categorias else 0, key=f"categoria_movimento_edicao_{escolha_id}")
            opcoes = descricoes_por_categoria(categoria_nova) or ["Por classificar"]
            descricao_nova = st.selectbox("Descrição personalizada", opcoes, key=f"descricao_movimento_edicao_{escolha_id}")
            criar_regra = st.checkbox("Criar/atualizar regra automática para movimentos futuros", key=f"criar_regra_movimento_{escolha_id}")
            if st.button("Guardar classificação", key=f"guardar_classificacao_{escolha_id}"):
                descricao_original = filtrado.loc[filtrado["id"] == escolha_id, "descricao_original"].iloc[0]
                aplicar_classificacao(escolha_id, categoria_nova, descricao_nova, criar_regra, descricao_original)
                st.success("Classificação guardada no JSON local.")
                st.rerun()

    st.divider()
    st.subheader("Regras automáticas")
    st.caption("Alterar ou eliminar uma regra atualiza também os movimentos classificados automaticamente.")
    regras = sorted(st.session_state.dados["regras"], key=lambda r: (len(r.get("texto_contem", "")), r.get("texto_contem", "").lower()), reverse=True)
    if regras:
        for regra in regras:
            with st.expander(f"{regra['texto_contem']}  ·  {regra['categoria']} / {regra['descricao_personalizada']}"):
                texto_regra = st.text_input("Descrição bancária contém", value=regra.get("texto_contem", ""), key=f"texto_regra_{regra['id']}")
                categorias = categorias_atuais()
                categoria_atual = regra.get("categoria")
                categoria_regra = st.selectbox("Categoria", categorias, index=categorias.index(categoria_atual) if categoria_atual in categorias else 0, key=f"categoria_regra_{regra['id']}")
                descricoes_regra = descricoes_por_categoria(categoria_regra) or ["Por classificar"]
                descricao_atual = regra.get("descricao_personalizada", "Por classificar")
                descricao_padrao = descricoes_regra.index(descricao_atual) if descricao_atual in descricoes_regra else 0
                descricao_regra = st.selectbox("Descrição", descricoes_regra, index=descricao_padrao, key=f"descricao_regra_{regra['id']}")
                ativa_regra = st.checkbox("Regra ativa", value=regra.get("ativa", True), key=f"ativa_regra_{regra['id']}")
                if st.button("Guardar regra", key=f"guardar_regra_{regra['id']}"):
                    if not texto_regra.strip():
                        st.error("O texto da regra não pode ficar vazio.")
                    else:
                        texto_anterior = regra.get("texto_contem", "")
                        regra.update({"texto_contem": texto_regra.strip(), "categoria": categoria_regra, "descricao_personalizada": descricao_regra, "ativa": ativa_regra})
                        reclassificar_movimentos_automaticos([texto_anterior, texto_regra.strip()])
                        guardar_json_local()
                        st.success("Regra atualizada e movimentos automáticos revistos.")
                        st.rerun()
                if st.button("Eliminar regra", key=f"eliminar_regra_{regra['id']}"):
                    texto_removido = regra.get("texto_contem", "")
                    st.session_state.dados["regras"] = [r for r in st.session_state.dados["regras"] if r["id"] != regra["id"]]
                    reclassificar_movimentos_automaticos([texto_removido])
                    guardar_json_local()
                    st.success("Regra eliminada e movimentos automáticos atualizados.")
                    st.rerun()
    else:
        st.info("Ainda não existem regras automáticas. Cria uma ao classificar um movimento e marcar a opção correspondente.")

elif pagina == "Categorias e descrições":
    st.header("Categorias e descrições")
    st.subheader("Categorias")
    with st.form("nova_categoria"):
        nova_categoria = st.text_input("Nova categoria", placeholder="Ex.: Educação")
        adicionar_cat = st.form_submit_button("＋ Adicionar categoria", key="adicionar_categoria")
        if adicionar_cat:
            if adicionar_categoria(nova_categoria):
                st.success("Categoria adicionada e guardada.")
                st.rerun()
            st.error("Escreve uma categoria nova, ainda não existente.")

    for categoria in categorias_atuais():
        cabecalho, apagar, _ = st.columns([0.26, 0.06, 0.68])
        cabecalho.subheader(categoria)
        if apagar.button("×", key=f"remover_categoria_{categoria}", help=f"Remover categoria {categoria}"):
            remover_categoria(categoria)
            st.rerun()
        descricoes = descricoes_por_categoria(categoria)
        botoes_descricoes = [(descricao, indice) for indice, descricao in enumerate(descricoes)] + [(None, None)]
        for inicio in range(0, len(botoes_descricoes), 4):
            elementos_linha = botoes_descricoes[inicio:inicio + 4]
            colunas_descricoes = st.columns(len(elementos_linha))
            for coluna, (descricao, _) in zip(colunas_descricoes, elementos_linha):
                if descricao is None:
                    coluna.button(
                        "＋",
                        key=f"adicionar_descricao_{normalizar_nome(categoria)}",
                        help=f"Adicionar descrição a {categoria}",
                        use_container_width=True,
                        on_click=alternar_adicao_descricao,
                        args=(categoria,),
                    )
                elif coluna.button(
                    f"{descricao}  ×",
                    key=f"remover_descricao_{normalizar_nome(categoria)}_{normalizar_nome(descricao)}",
                    help=f"Remover {descricao}",
                    use_container_width=True,
                ):
                    remover_descricao(categoria, descricao)
                    st.rerun()
        if st.session_state.categoria_descricao_aberta == categoria:
            with st.form(f"nova_descricao_{normalizar_nome(categoria)}"):
                col_nome, col_adicionar, col_cancelar = st.columns([6, 1, 1])
                nome = col_nome.text_input("Nome da descrição", placeholder="Ex.: Farmácia")
                adicionar_desc = col_adicionar.form_submit_button("＋", help="Adicionar descrição")
                cancelar_desc = col_cancelar.form_submit_button("×", help="Cancelar")
                if adicionar_desc:
                    if not nome.strip():
                        st.error("Escreve uma descrição.")
                    elif any(item.casefold() == nome.strip().casefold() for item in descricoes_por_categoria(categoria)):
                        st.error("Essa descrição já existe nesta categoria.")
                    else:
                        guardar_descricao(categoria, nome)
                        st.session_state.categoria_descricao_aberta = None
                        guardar_json_local()
                        st.rerun()
                if cancelar_desc:
                    st.session_state.categoria_descricao_aberta = None
                    st.rerun()

else:
    st.header("Resumo mensal")
    df = carregar_movimentos()
    if df.empty:
        st.info("Ainda não existem movimentos. Vai a 'Importar movimentos' para carregar o primeiro extrato.")
    else:
        df["data_movimento"] = pd.to_datetime(df["data_movimento"], errors="coerce")
        df = df[df["data_movimento"].notna()].copy()
        if df.empty:
            st.warning("Existem movimentos, mas as datas não estão num formato reconhecido para o resumo mensal.")
        else:
            df["mes"] = df["data_movimento"].dt.strftime("%Y-%m")
            meses = sorted(df["mes"].unique().tolist(), reverse=True)
            mes = st.selectbox("Mês", meses)
            mensal = df[df["mes"] == mes].copy()
            rendimentos = mensal.loc[(mensal["categoria"] == "Rendimentos") & (mensal["montante"] > 0), "montante"].sum()
            despesas = mensal.loc[(~mensal["categoria"].isin(["Rendimentos", "Transferências", "Poupanças"])) & (mensal["montante"] < 0), "montante"].sum() * -1
            transferencias = mensal.loc[(mensal["categoria"].isin(["Despesas pessoais", "Transferências", "Poupanças"])) & (mensal["montante"] < 0), "montante"].sum() * -1
            variacao_saldo = mensal["montante"].sum()
            fmt = lambda x: f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Rendimentos", fmt(rendimentos)); c2.metric("Despesas classificadas", fmt(despesas)); c3.metric("Transferências / poupanças", fmt(transferencias)); c4.metric("Variação líquida dos movimentos", fmt(variacao_saldo))
            st.subheader("Totais por categoria")
            resumo_cat = mensal.groupby("categoria", dropna=False)["montante"].sum().reset_index()
            resumo_cat["Total de saídas (positivo)"] = resumo_cat["montante"].apply(lambda x: -x if x < 0 else 0)
            resumo_cat = resumo_cat.rename(columns={"categoria": "Categoria", "montante": "Montante"})
            st.dataframe(resumo_cat[["Categoria", "Montante", "Total de saídas (positivo)"]], use_container_width=True, hide_index=True)
            st.subheader("Totais por descrição")
            resumo_desc = mensal.groupby(["categoria", "descricao_personalizada"], dropna=False)["montante"].sum().reset_index()
            resumo_desc = resumo_desc.rename(columns={"categoria": "Categoria", "descricao_personalizada": "Descrição", "montante": "Montante"})
            st.dataframe(resumo_desc, use_container_width=True, hide_index=True)
            st.subheader("Movimentos do mês")
            movimentos_mes = mensal[["data_movimento", "descricao_original", "montante", "categoria", "descricao_personalizada", "estado"]].sort_values("data_movimento", ascending=False)
            movimentos_mes = movimentos_mes.rename(columns={"data_movimento": "Data", "descricao_original": "Nome", "montante": "Montante", "categoria": "Categoria", "descricao_personalizada": "Descrição", "estado": "Estado"})
            st.dataframe(movimentos_mes, use_container_width=True, hide_index=True)

