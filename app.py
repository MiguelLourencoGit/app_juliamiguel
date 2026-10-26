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
    st.session_state.dados, _ = carregar_json_local()
if "nome_json" not in st.session_state:
    st.session_state.nome_json = ARQUIVO_JSON_LOCAL.name if ARQUIVO_JSON_LOCAL.exists() else "Sessão nova (sem histórico carregado)"


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
    texto = str(descricao or "").lower()
    regras = sorted([r for r in st.session_state.dados["regras"] if r.get("ativa", True)], key=lambda r: len(r.get("texto_contem", "")), reverse=True)
    for regra in regras:
        if regra["texto_contem"].lower() in texto:
            return regra["categoria"], regra["descricao_personalizada"], "Automática"
    return "Outros", "Por classificar", "Por classificar"


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
        movimento = {"id": dados["proximo_id_movimento"], "data_movimento": data, "data_valor": data_valor, "descricao_original": str(descricao).strip(), "montante": montante, "saldo": saldo, "conta": conta_registo, "categoria": categoria, "descricao_personalizada": desc_personalizada, "estado": estado, "origem_ficheiro": origem, "fingerprint": fp}
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
            break
    if criar_regra and texto_regra.strip():
        existente = next((r for r in st.session_state.dados["regras"] if r["texto_contem"].casefold() == texto_regra.strip().casefold()), None)
        if existente:
            existente.update({"categoria": categoria, "descricao_personalizada": descricao_personalizada, "ativa": True})
        else:
            st.session_state.dados["regras"].append({"id": st.session_state.dados["proximo_id_regra"], "texto_contem": texto_regra.strip(), "categoria": categoria, "descricao_personalizada": descricao_personalizada, "ativa": True})
            st.session_state.dados["proximo_id_regra"] += 1
    guardar_json_local()

# Gestão do JSON: é opcional. Sem carregar JSON, a aplicação começa vazia e aceita CSV/XML normalmente.
st.sidebar.header("Guardar e recuperar dados")
st.sidebar.caption("Se houver um ficheiro JSON local na pasta da app, ele é carregado automaticamente na próxima vez que abrires a aplicação.")
json_upload = st.sidebar.file_uploader("Carregar histórico (.json, opcional)", type=["json"], key="json_upload")
if st.sidebar.button("Carregar JSON", use_container_width=True):
    if json_upload is None:
        st.sidebar.warning("Seleciona primeiro um ficheiro JSON.")
    else:
        try:
            candidato = json.loads(json_upload.getvalue().decode("utf-8-sig"))
            if not isinstance(candidato, dict) or not all(k in candidato for k in ("movimentos", "regras", "descricoes")):
                raise ValueError("O JSON não tem a estrutura esperada da aplicação.")
            base = estado_novo()
            base.update(candidato)
            base["movimentos"] = list(base.get("movimentos", []))
            base["regras"] = list(base.get("regras", []))
            base["descricoes"] = list(base.get("descricoes", []))
            base["proximo_id_movimento"] = max([int(m.get("id", 0)) for m in base["movimentos"]] + [0]) + 1
            base["proximo_id_regra"] = max([int(r.get("id", 0)) for r in base["regras"]] + [0]) + 1
            st.session_state.dados = base
            st.session_state.nome_json = json_upload.name
            guardar_json_local()
            st.sidebar.success(f"Histórico carregado: {json_upload.name}")
        except Exception as exc:
            st.sidebar.error(f"Não foi possível carregar o JSON: {exc}")
if st.sidebar.button("Guardar no JSON local", use_container_width=True):
    if guardar_json_local():
        st.sidebar.success(f"Histórico guardado em: {ARQUIVO_JSON_LOCAL.name}")
    else:
        st.sidebar.error("Não foi possível guardar o JSON local.")
st.sidebar.caption(f"Dados atuais: {st.session_state.nome_json}")
json_bytes = json.dumps(st.session_state.dados, ensure_ascii=False, indent=2).encode("utf-8")
st.sidebar.download_button("Descarregar cópia JSON", data=json_bytes, file_name="financas_casal.json", mime="application/json", use_container_width=True)

pagina = st.sidebar.radio("Navegação", ["Resumo mensal", "Importar movimentos", "Movimentos", "Regras automáticas", "Categorias e descrições"])

if pagina == "Importar movimentos":
    st.header("Importar movimentos bancários")
    st.write("Carrega um CSV ou XML exportado pelo banco. Funciona mesmo sem teres carregado um JSON.")
    conta = st.text_input("Nome da conta", value="Conta conjunta")
    ficheiro = st.file_uploader("Escolhe o ficheiro", type=["csv", "xml", "txt"])
    if ficheiro is not None:
        st.info(f"Ficheiro selecionado: {ficheiro.name}")
        if st.button("Importar ficheiro", type="primary"):
            try:
                conteudo = ficheiro.getvalue()
                registos = ler_xml(conteudo) if ficheiro.name.lower().endswith(".xml") else ler_csv(conteudo)
                if not registos:
                    st.warning("Não encontrei linhas de movimentos no ficheiro.")
                else:
                    inseridos, ignorados = importar_movimentos(registos, ficheiro.name, conta)
                    st.success(f"Importação concluída: {inseridos} movimentos novos; {ignorados} possíveis duplicados ignorados.")
                    st.caption("Os duplicados são comparados apenas com os movimentos presentes nesta sessão/histórico carregado.")
            except Exception as exc:
                st.error(f"Não foi possível importar o ficheiro: {exc}")
                st.caption("O formato exportado pelo banco pode precisar de um ajuste específico no leitor.")

elif pagina == "Movimentos":
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
        st.dataframe(filtrado[["id", "data_movimento", "descricao_original", "montante", "categoria", "descricao_personalizada", "estado"]], use_container_width=True, hide_index=True)
        st.subheader("Editar classificação de um movimento")
        ids = filtrado["id"].tolist()
        if ids:
            escolha_id = st.selectbox("Escolhe o movimento", ids, format_func=lambda x: f"#{x} | {filtrado.loc[filtrado['id'] == x, 'data_movimento'].iloc[0]} | {filtrado.loc[filtrado['id'] == x, 'descricao_original'].iloc[0]}")
            movimento = df.loc[df["id"] == escolha_id].iloc[0]
            with st.form("editar_movimento"):
                categoria_nova = st.selectbox("Categoria principal", CATEGORIAS, index=CATEGORIAS.index(movimento["categoria"]) if movimento["categoria"] in CATEGORIAS else CATEGORIAS.index("Outros"))
                existentes = descricoes_por_categoria(categoria_nova)
                atual = movimento["descricao_personalizada"]
                opcoes = existentes.copy()
                if atual and atual not in opcoes: opcoes.insert(0, atual)
                descricao_nova = st.selectbox("Descrição personalizada", opcoes if opcoes else ["Por classificar"])
                descricao_manual = st.text_input("Ou escreve uma nova descrição", value="")
                criar_regra = st.checkbox("Criar/atualizar regra automática para movimentos futuros")
                texto_regra = st.text_input("Texto que a descrição bancária deve conter", value=movimento["descricao_original"])
                guardar = st.form_submit_button("Guardar classificação")
                if guardar:
                    aplicar_classificacao(escolha_id, categoria_nova, descricao_manual.strip() or descricao_nova, criar_regra, texto_regra)
                    st.success("Classificação guardada e salva no JSON local.")
                    st.rerun()

elif pagina == "Regras automáticas":
    st.header("Regras automáticas")
    st.write("As regras procuram um texto dentro da descrição original. As mais específicas são aplicadas primeiro.")
    with st.form("nova_regra"):
        texto = st.text_input("Se a descrição bancária contiver")
        categoria = st.selectbox("Categoria principal", CATEGORIAS)
        descricoes = descricoes_por_categoria(categoria)
        descricao = st.selectbox("Descrição personalizada", descricoes if descricoes else ["Por classificar"])
        descricao_nova = st.text_input("Ou cria uma nova descrição")
        ativa = st.checkbox("Regra ativa", value=True)
        submeter = st.form_submit_button("Guardar regra")
        if submeter:
            nome_desc = descricao_nova.strip() or descricao
            if not texto.strip():
                st.error("Indica o texto a procurar na descrição bancária.")
            else:
                guardar_descricao(categoria, nome_desc)
                existente = next((r for r in st.session_state.dados["regras"] if r["texto_contem"].casefold() == texto.strip().casefold()), None)
                if existente: existente.update({"categoria": categoria, "descricao_personalizada": nome_desc, "ativa": ativa})
                else:
                    st.session_state.dados["regras"].append({"id": st.session_state.dados["proximo_id_regra"], "texto_contem": texto.strip(), "categoria": categoria, "descricao_personalizada": nome_desc, "ativa": ativa})
                    st.session_state.dados["proximo_id_regra"] += 1
                guardar_json_local()
                st.success("Regra guardada. O JSON local foi atualizado.")
                st.rerun()
    regras = sorted(st.session_state.dados["regras"], key=lambda r: (len(r.get("texto_contem", "")), r.get("texto_contem", "").lower()), reverse=True)
    if regras:
        st.subheader("Regras existentes")
        st.dataframe(pd.DataFrame(regras)[["id", "texto_contem", "categoria", "descricao_personalizada", "ativa"]], use_container_width=True, hide_index=True)
        apagar_id = st.selectbox("ID da regra a eliminar", [None] + [r["id"] for r in regras], format_func=lambda x: "Selecionar..." if x is None else str(x))
        if apagar_id is not None and st.button("Eliminar regra selecionada"):
            st.session_state.dados["regras"] = [r for r in st.session_state.dados["regras"] if r["id"] != apagar_id]
            guardar_json_local()
            st.success("Regra eliminada.")
            st.rerun()
    else:
        st.info("Ainda não criaste regras.")

elif pagina == "Categorias e descrições":
    st.header("Categorias e descrições")
    st.write("As categorias principais são predefinidas. Podes acrescentar descrições reutilizáveis.")
    categoria = st.selectbox("Categoria", CATEGORIAS)
    with st.form("nova_descricao"):
        nome = st.text_input("Nova descrição personalizada", placeholder="Ex.: Farmácia")
        submeter = st.form_submit_button("Adicionar descrição")
        if submeter:
            if not nome.strip(): st.error("Escreve uma descrição.")
            else:
                guardar_descricao(categoria, nome)
                st.success("Descrição adicionada e guardada no JSON local.")
                st.rerun()
    st.subheader("Descrições existentes")
    for cat in CATEGORIAS:
        descs = descricoes_por_categoria(cat)
        if descs:
            with st.expander(cat): st.write(", ".join(descs))

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
            resumo_cat["total de saídas (positivo)"] = resumo_cat["montante"].apply(lambda x: -x if x < 0 else 0)
            st.dataframe(resumo_cat[["categoria", "montante", "total de saídas (positivo)"]], use_container_width=True, hide_index=True)
            st.subheader("Totais por descrição")
            st.dataframe(mensal.groupby(["categoria", "descricao_personalizada"], dropna=False)["montante"].sum().reset_index(), use_container_width=True, hide_index=True)
            st.subheader("Movimentos do mês")
            st.dataframe(mensal[["data_movimento", "descricao_original", "montante", "categoria", "descricao_personalizada", "estado"]].sort_values("data_movimento", ascending=False), use_container_width=True, hide_index=True)

st.sidebar.divider()
st.sidebar.caption("Os dados are guardados localmente em JSON na pasta da app, para poderes continuar em sessões futuras sem carregares o ficheiro manualmente.")
st.sidebar.caption("Podes também descarregar uma cópia em JSON a qualquer momento.")
