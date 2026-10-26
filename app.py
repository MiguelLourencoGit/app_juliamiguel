import csv
import hashlib
import io
import sqlite3
import tempfile
import os
import xml.etree.ElementTree as ET
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
import streamlit as st

# ----------------------------
# Configuração
# ----------------------------
st.set_page_config(page_title="Finanças do casal", page_icon="💶", layout="wide")
st.title("💶 Finanças do casal")
st.caption("Importa movimentos bancários, classifica-os e acompanha as despesas ao longo do tempo.")

# A base de dados de trabalho fica numa pasta temporária da sessão, nunca na pasta Git.
# Para guardar os dados entre sessões, descarrega o .db e volta a carregá-lo na próxima utilização.
if "db_work_path" not in st.session_state:
    fd, temp_path = tempfile.mkstemp(prefix="financas_casal_", suffix=".db")
    os.close(fd)
    st.session_state["db_work_path"] = temp_path
    st.session_state["db_loaded_name"] = "Nova base de dados (sessão atual)"

DB_PATH = Path(st.session_state["db_work_path"])

CATEGORIAS = [
    "Rendimentos",
    "Casa",
    "Despesas do dia a dia",
    "Despesas pessoais",
    "Poupanças",
    "Transferências",
    "Outros",
]

DESCRICOES_INICIAIS = [
    ("Rendimentos", "Ordenado Miguel"),
    ("Rendimentos", "Ordenado Júlia"),
    ("Rendimentos", "Outros rendimentos"),
    ("Rendimentos", "Reembolsos"),
    ("Casa", "Crédito habitação"),
    ("Casa", "Seguro de vida"),
    ("Casa", "Seguro multirriscos"),
    ("Casa", "Manutenção da conta"),
    ("Casa", "Condomínio"),
    ("Casa", "IMI"),
    ("Casa", "Mobiliário e equipamento"),
    ("Casa", "Manutenção e reparações"),
    ("Despesas do dia a dia", "Eletricidade"),
    ("Despesas do dia a dia", "Água"),
    ("Despesas do dia a dia", "Gás"),
    ("Despesas do dia a dia", "Internet"),
    ("Despesas do dia a dia", "Telecomunicações"),
    ("Despesas do dia a dia", "Supermercado"),
    ("Despesas do dia a dia", "Restaurantes e takeaway"),
    ("Despesas do dia a dia", "Transportes"),
    ("Despesas do dia a dia", "Saúde"),
    ("Despesas pessoais", "Transferência para Miguel"),
    ("Despesas pessoais", "Transferência para Júlia"),
    ("Poupanças", "Transferência para poupança"),
    ("Transferências", "Transferência entre contas"),
    ("Outros", "Por classificar"),
]


def conectar():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def inicializar_db():
    with conectar() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS movimentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_movimento TEXT,
                data_valor TEXT,
                descricao_original TEXT NOT NULL,
                montante REAL NOT NULL,
                saldo REAL,
                conta TEXT DEFAULT 'Conta conjunta',
                categoria TEXT DEFAULT 'Outros',
                descricao_personalizada TEXT DEFAULT 'Por classificar',
                estado TEXT DEFAULT 'Por classificar',
                origem_ficheiro TEXT,
                fingerprint TEXT UNIQUE,
                criado_em TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS regras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                texto_contem TEXT NOT NULL UNIQUE COLLATE NOCASE,
                categoria TEXT NOT NULL,
                descricao_personalizada TEXT NOT NULL,
                ativa INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS descricoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                categoria TEXT NOT NULL,
                nome TEXT NOT NULL COLLATE NOCASE,
                UNIQUE(categoria, nome)
            )
        """)
        for categoria, descricao in DESCRICOES_INICIAIS:
            conn.execute(
                "INSERT OR IGNORE INTO descricoes (categoria, nome) VALUES (?, ?)",
                (categoria, descricao),
            )


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
    # Formato português: 4.620,24; formato internacional: 4620.24
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
    base = "|".join([
        str(data or ""),
        str(descricao or "").strip().lower(),
        f"{float(montante):.2f}",
        "" if saldo is None else f"{float(saldo):.2f}",
        str(conta or "Conta conjunta").strip().lower(),
    ])
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
        if "data mov." in linha.lower() and "montante" in linha.lower() and ("descrição" in linha.lower() or "descricao" in linha.lower()):
            indice_cabecalho = i
            delimitador = ";" if linha.count(";") >= linha.count(",") else ","
            break

    if indice_cabecalho is not None:
        leitor = csv.DictReader(linhas[indice_cabecalho:], delimiter=delimitador)
        dados = list(leitor)
        return dados

    # CSV simples: tenta detetar o delimitador e ler a primeira linha como cabeçalho.
    amostra = "\n".join(linhas[:10])
    try:
        delimitador = csv.Sniffer().sniff(amostra, delimiters=";,\t|").delimiter
    except csv.Error:
        delimitador = ";"
    return list(csv.DictReader(io.StringIO(texto), delimiter=delimitador))


def texto_xml(elemento):
    return (elemento.text or "").strip()


def ler_xml(conteudo_bytes):
    raiz = ET.fromstring(conteudo_bytes)
    movimentos = []
    nomes_data = {"date", "bookingdate", "datamov", "datamovimento", "data"}
    nomes_valor = {"amount", "value", "montante", "valor", "creditdebitamount"}
    nomes_desc = {"description", "descricao", "remittanceinformation", "details", "memo"}
    nomes_saldo = {"balance", "saldo", "saldocontabilistico", "runningbalance"}
    nomes_data_valor = {"valuedate", "datavalor", "dataval"}

    # Procura nós que tenham descendentes que pareçam conter data, montante e descrição.
    for no in raiz.iter():
        filhos = list(no.iter())
        if len(filhos) < 3:
            continue
        campos = {}
        for filho in filhos:
            chave = normalizar_nome(filho.tag.split("}")[-1])
            valor = texto_xml(filho)
            if not valor:
                continue
            if chave in nomes_data and "data" not in campos:
                campos["data"] = valor
            elif chave in nomes_data_valor and "data_valor" not in campos:
                campos["data_valor"] = valor
            elif chave in nomes_valor and "montante" not in campos:
                campos["montante"] = valor
            elif chave in nomes_desc and "descricao" not in campos:
                campos["descricao"] = valor
            elif chave in nomes_saldo and "saldo" not in campos:
                campos["saldo"] = valor
        if "data" in campos and "montante" in campos and "descricao" in campos:
            montante = valor_monetario(campos.get("montante"))
            if montante is not None:
                movimentos.append({
                    "Data mov.": campos.get("data"),
                    "Data-valor": campos.get("data_valor", ""),
                    "Descrição": campos.get("descricao"),
                    "Montante": campos.get("montante"),
                    "Saldo contabilístico após movimento": campos.get("saldo", ""),
                })
    # Deduplica resultados repetidos gerados por nós-pai e nós-filho.
    unicos = {}
    for m in movimentos:
        chave = (m.get("Data mov."), m.get("Descrição"), m.get("Montante"), m.get("Saldo contabilístico após movimento"))
        unicos[chave] = m
    if not unicos:
        raise ValueError("Não consegui identificar movimentos neste XML. O formato XML do banco pode precisar de um leitor específico.")
    return list(unicos.values())


def obter_valor(registo, mapa, campo):
    coluna = mapa.get(campo)
    return registo.get(coluna) if coluna else None


def classificar(descricao, regras):
    texto = str(descricao or "").lower()
    for regra in regras:
        if regra["ativa"] and str(regra["texto_contem"]).lower() in texto:
            return regra["categoria"], regra["descricao_personalizada"], "Automática"
    return "Outros", "Por classificar", "Por classificar"


def importar_movimentos(registos, origem, conta):
    inseridos = 0
    ignorados = 0
    with conectar() as conn:
        regras = conn.execute("SELECT * FROM regras WHERE ativa = 1 ORDER BY LENGTH(texto_contem) DESC").fetchall()
        for registo in registos:
            mapa = mapear_colunas(list(registo.keys()))
            data = data_iso(obter_valor(registo, mapa, "data"))
            data_valor = data_iso(obter_valor(registo, mapa, "data_valor"))
            descricao = obter_valor(registo, mapa, "descricao")
            montante = valor_monetario(obter_valor(registo, mapa, "montante"))
            saldo = valor_monetario(obter_valor(registo, mapa, "saldo"))
            if not descricao or montante is None:
                continue
            categoria, descricao_personalizada, estado = classificar(descricao, regras)
            fp = fingerprint_movimento(data, descricao, montante, saldo, conta)
            try:
                conn.execute("""
                    INSERT INTO movimentos
                    (data_movimento, data_valor, descricao_original, montante, saldo, conta,
                     categoria, descricao_personalizada, estado, origem_ficheiro, fingerprint)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (data, data_valor, str(descricao).strip(), montante, saldo, conta,
                      categoria, descricao_personalizada, estado, origem, fp))
                inseridos += 1
            except sqlite3.IntegrityError:
                ignorados += 1
    return inseridos, ignorados


def carregar_movimentos():
    with conectar() as conn:
        return pd.read_sql_query("SELECT * FROM movimentos ORDER BY COALESCE(data_movimento, '') DESC, id DESC", conn)


def carregar_regras():
    with conectar() as conn:
        return conn.execute("SELECT * FROM regras ORDER BY LENGTH(texto_contem) DESC, texto_contem").fetchall()


def descricoes_por_categoria(categoria):
    with conectar() as conn:
        rows = conn.execute(
            "SELECT nome FROM descricoes WHERE categoria = ? ORDER BY nome", (categoria,)
        ).fetchall()
    return [r["nome"] for r in rows]


def guardar_descricao(categoria, nome):
    nome = nome.strip()
    if nome:
        with conectar() as conn:
            conn.execute("INSERT OR IGNORE INTO descricoes (categoria, nome) VALUES (?, ?)", (categoria, nome))


def aplicar_classificacao(movimento_id, categoria, descricao_personalizada, criar_regra, texto_regra):
    guardar_descricao(categoria, descricao_personalizada)
    with conectar() as conn:
        conn.execute("""
            UPDATE movimentos
            SET categoria = ?, descricao_personalizada = ?, estado = 'Manual'
            WHERE id = ?
        """, (categoria, descricao_personalizada, movimento_id))
        if criar_regra and texto_regra.strip():
            conn.execute("""
                INSERT INTO regras (texto_contem, categoria, descricao_personalizada, ativa)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(texto_contem) DO UPDATE SET
                    categoria = excluded.categoria,
                    descricao_personalizada = excluded.descricao_personalizada,
                    ativa = 1
            """, (texto_regra.strip(), categoria, descricao_personalizada))


# ----------------------------
# Gestão do ficheiro da base de dados
# ----------------------------
st.sidebar.header("Ficheiro da base de dados")
st.sidebar.caption("Carrega um ficheiro .db existente ou começa uma base de dados nova. Descarrega as alterações antes de fechar a sessão.")
db_upload = st.sidebar.file_uploader("Carregar base de dados (.db / .sqlite)", type=["db", "sqlite", "sqlite3"], key="database_upload")
if st.sidebar.button("Carregar base de dados", use_container_width=True):
    if db_upload is None:
        st.sidebar.warning("Seleciona primeiro um ficheiro de base de dados.")
    else:
        candidato = None
        try:
            fd, candidato = tempfile.mkstemp(prefix="financas_casal_import_", suffix=".db")
            os.close(fd)
            Path(candidato).write_bytes(db_upload.getvalue())
            # Valida que o ficheiro é uma base de dados SQLite íntegra antes de substituir a atual.
            with sqlite3.connect(candidato) as teste_conn:
                resultado = teste_conn.execute("PRAGMA integrity_check").fetchone()[0]
                if resultado != "ok":
                    raise ValueError("O ficheiro não passou a verificação de integridade do SQLite.")
            Path(st.session_state["db_work_path"]).write_bytes(Path(candidato).read_bytes())
            st.session_state["db_loaded_name"] = db_upload.name
            st.sidebar.success(f"Base de dados carregada: {db_upload.name}")
            st.rerun()
        except Exception as exc:
            st.sidebar.error(f"Não foi possível carregar esta base de dados: {exc}")
        finally:
            if candidato and Path(candidato).exists():
                try:
                    Path(candidato).unlink()
                except PermissionError:
                    # O Windows pode manter o ficheiro bloqueado durante o rerun.
                    pass

st.sidebar.caption(f"Em utilização: {st.session_state.get('db_loaded_name', 'Base de dados da sessão')}")
inicializar_db()
if DB_PATH.exists():
    st.sidebar.download_button(
        label="Descarregar base de dados (.db)",
        data=DB_PATH.read_bytes(),
        file_name="financas_casal.db",
        mime="application/vnd.sqlite3",
        use_container_width=True,
        help="Descarrega a base de dados atual, incluindo movimentos, categorias/descrições e regras.",
    )

# ----------------------------
# Navegação
# ----------------------------
pagina = st.sidebar.radio(
    "Navegação",
    ["Resumo mensal", "Importar movimentos", "Movimentos", "Regras automáticas", "Categorias e descrições"],
)

if pagina == "Importar movimentos":
    st.header("Importar movimentos bancários")
    st.write("Carrega um CSV ou XML exportado pelo banco. A aplicação tenta reconhecer as colunas mais comuns.")
    conta = st.text_input("Nome da conta", value="Conta conjunta")
    ficheiro = st.file_uploader("Escolhe o ficheiro", type=["csv", "xml", "txt"])
    if ficheiro is not None:
        st.info(f"Ficheiro selecionado: {ficheiro.name}")
        if st.button("Importar ficheiro", type="primary"):
            try:
                conteudo = ficheiro.getvalue()
                if ficheiro.name.lower().endswith(".xml"):
                    registos = ler_xml(conteudo)
                else:
                    registos = ler_csv(conteudo)
                if not registos:
                    st.warning("Não encontrei linhas de movimentos no ficheiro.")
                else:
                    inseridos, ignorados = importar_movimentos(registos, ficheiro.name, conta)
                    st.success(f"Importação concluída: {inseridos} movimentos novos; {ignorados} possíveis duplicados ignorados.")
                    st.caption("Confirma sempre alguns movimentos após a importação, especialmente na primeira utilização.")
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
        categorias_filtro = ["Todas"] + sorted(df["categoria"].dropna().unique().tolist())
        categoria_filtro = col1.selectbox("Filtrar por categoria", categorias_filtro)
        pesquisa = col2.text_input("Pesquisar descrição")
        estados = ["Todos"] + sorted(df["estado"].dropna().unique().tolist())
        estado_filtro = col3.selectbox("Estado da classificação", estados)
        filtrado = df.copy()
        if categoria_filtro != "Todas":
            filtrado = filtrado[filtrado["categoria"] == categoria_filtro]
        if pesquisa:
            filtrado = filtrado[filtrado["descricao_original"].str.contains(pesquisa, case=False, na=False)]
        if estado_filtro != "Todos":
            filtrado = filtrado[filtrado["estado"] == estado_filtro]

        colunas = ["id", "data_movimento", "descricao_original", "montante", "categoria", "descricao_personalizada", "estado"]
        st.dataframe(filtrado[colunas], use_container_width=True, hide_index=True)

        st.subheader("Editar classificação de um movimento")
        ids = filtrado["id"].tolist()
        if ids:
            escolha_id = st.selectbox(
                "Escolhe o movimento",
                ids,
                format_func=lambda x: f"#{x} | {filtrado.loc[filtrado['id'] == x, 'data_movimento'].iloc[0]} | {filtrado.loc[filtrado['id'] == x, 'descricao_original'].iloc[0]}",
            )
            movimento = df.loc[df["id"] == escolha_id].iloc[0]
            with st.form("editar_movimento"):
                categoria_nova = st.selectbox(
                    "Categoria principal",
                    CATEGORIAS,
                    index=CATEGORIAS.index(movimento["categoria"]) if movimento["categoria"] in CATEGORIAS else CATEGORIAS.index("Outros"),
                )
                existentes = descricoes_por_categoria(categoria_nova)
                atual = movimento["descricao_personalizada"]
                opcoes = existentes.copy()
                if atual and atual not in opcoes:
                    opcoes.insert(0, atual)
                descricao_nova = st.selectbox("Descrição personalizada", opcoes if opcoes else ["Por classificar"])
                descricao_manual = st.text_input("Ou escreve uma nova descrição", value="")
                criar_regra = st.checkbox("Criar/atualizar regra automática para movimentos futuros")
                texto_regra = st.text_input("Texto que a descrição bancária deve conter", value=movimento["descricao_original"])
                guardar = st.form_submit_button("Guardar classificação")
                if guardar:
                    descricao_final = descricao_manual.strip() or descricao_nova
                    aplicar_classificacao(escolha_id, categoria_nova, descricao_final, criar_regra, texto_regra)
                    st.success("Classificação guardada.")
                    st.rerun()

elif pagina == "Regras automáticas":
    st.header("Regras automáticas")
    st.write("Cada regra procura um texto dentro da descrição original do movimento. As regras mais específicas são aplicadas primeiro.")
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
                with conectar() as conn:
                    conn.execute("""
                        INSERT INTO regras (texto_contem, categoria, descricao_personalizada, ativa)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(texto_contem) DO UPDATE SET
                            categoria = excluded.categoria,
                            descricao_personalizada = excluded.descricao_personalizada,
                            ativa = excluded.ativa
                    """, (texto.strip(), categoria, nome_desc, int(ativa)))
                st.success("Regra guardada.")
                st.rerun()

    regras = carregar_regras()
    if regras:
        st.subheader("Regras existentes")
        df_regras = pd.DataFrame([dict(r) for r in regras])
        st.dataframe(df_regras[["id", "texto_contem", "categoria", "descricao_personalizada", "ativa"]], use_container_width=True, hide_index=True)
        apagar_id = st.selectbox("ID da regra a eliminar", [None] + [r["id"] for r in regras], format_func=lambda x: "Selecionar..." if x is None else str(x))
        if apagar_id is not None and st.button("Eliminar regra selecionada"):
            with conectar() as conn:
                conn.execute("DELETE FROM regras WHERE id = ?", (apagar_id,))
            st.success("Regra eliminada.")
            st.rerun()
    else:
        st.info("Ainda não criaste regras.")

elif pagina == "Categorias e descrições":
    st.header("Categorias e descrições")
    st.write("As categorias principais são predefinidas nesta primeira versão. Podes acrescentar descrições reutilizáveis.")
    categoria = st.selectbox("Categoria", CATEGORIAS)
    with st.form("nova_descricao"):
        nome = st.text_input("Nova descrição personalizada", placeholder="Ex.: Farmácia")
        submeter = st.form_submit_button("Adicionar descrição")
        if submeter:
            if not nome.strip():
                st.error("Escreve uma descrição.")
            else:
                guardar_descricao(categoria, nome)
                st.success("Descrição adicionada.")
                st.rerun()
    st.subheader("Descrições existentes")
    for cat in CATEGORIAS:
        descs = descricoes_por_categoria(cat)
        if descs:
            with st.expander(cat):
                st.write(", ".join(descs))

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

            # Rendimentos são entradas positivas classificadas como Rendimentos.
            rendimentos = mensal.loc[(mensal["categoria"] == "Rendimentos") & (mensal["montante"] > 0), "montante"].sum()
            despesas = mensal.loc[
                (~mensal["categoria"].isin(["Rendimentos", "Transferências", "Poupanças"])) &
                (mensal["montante"] < 0), "montante"
            ].sum() * -1
            transferencias = mensal.loc[
                (mensal["categoria"].isin(["Despesas pessoais", "Transferências", "Poupanças"])) &
                (mensal["montante"] < 0), "montante"
            ].sum() * -1
            variacao_saldo = mensal["montante"].sum()

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Rendimentos", f"{rendimentos:,.2f} €".replace(",", "X").replace(".", ",").replace("X", "."))
            c2.metric("Despesas classificadas", f"{despesas:,.2f} €".replace(",", "X").replace(".", ",").replace("X", "."))
            c3.metric("Transferências / poupanças", f"{transferencias:,.2f} €".replace(",", "X").replace(".", ",").replace("X", "."))
            c4.metric("Variação líquida dos movimentos", f"{variacao_saldo:,.2f} €".replace(",", "X").replace(".", ",").replace("X", "."))

            st.subheader("Totais por categoria")
            resumo_cat = mensal.groupby("categoria", dropna=False)["montante"].sum().reset_index()
            resumo_cat["total de saídas (positivo)"] = resumo_cat["montante"].apply(lambda x: -x if x < 0 else 0)
            st.dataframe(resumo_cat[["categoria", "montante", "total de saídas (positivo)"]], use_container_width=True, hide_index=True)

            st.subheader("Totais por descrição")
            resumo_desc = mensal.groupby(["categoria", "descricao_personalizada"], dropna=False)["montante"].sum().reset_index()
            st.dataframe(resumo_desc, use_container_width=True, hide_index=True)

            st.subheader("Movimentos do mês")
            st.dataframe(
                mensal[["data_movimento", "descricao_original", "montante", "categoria", "descricao_personalizada", "estado"]]
                .sort_values("data_movimento", ascending=False),
                use_container_width=True,
                hide_index=True,
            )

st.sidebar.divider()
st.sidebar.caption("A base de dados de trabalho fica temporariamente na sessão, não na pasta do projeto/Git.")
st.sidebar.caption("Descarrega o ficheiro .db antes de terminares a sessão e carrega-o novamente quando voltares.")
st.sidebar.caption("Confirma o formato do extrato e as classificações antes de confiares nos totais.")
