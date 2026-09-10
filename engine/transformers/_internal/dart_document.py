"""Read retained OpenDART documents without losing encoding or statement scope."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from bs4 import BeautifulSoup, NavigableString, Tag


@dataclass(frozen=True)
class DartFinancialDocument:
    html: str
    encoding: str
    source_sha256: str
    section_title: str = ""
    financial_scope: str = ""


def _decode(raw: bytes) -> tuple[str, str]:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        # Some original DART XML declares UTF-8 but contains Korean CP949.
        # Neither branch drops or substitutes undecodable bytes.
        return raw.decode("cp949"), "cp949"


def _title(value: str) -> str:
    value = re.sub(r"\s+", "", value)
    return re.sub(r"^\d+[.．]", "", value)


def _has_statement_table(section: Tag) -> bool:
    return any(table.get("border") == "1" for table in section.find_all("table"))


def _fragment(nodes: list[Tag]) -> Tag:
    return BeautifulSoup("<div>" + "".join(map(str, nodes)) + "</div>", "html.parser").div


def _legacy_notes_scope(node: Tag, default_scope: str | None = None) -> str | None:
    if node.name != "p":
        return None
    first = node.get_text("\n", strip=True).splitlines()
    if not first:
        return None
    title = re.sub(r"^[가-힣][.．]", "", re.sub(r"\s+", "", first[0]))
    if title in {"주석사항", "주석"}:
        return default_scope
    match = re.fullmatch(r"(연결|별도|개별)?재무제표(?:에대한)?주석", title)
    if match:
        return "CFS" if match[1] == "연결" else ("OFS" if match[1] else default_scope or "OFS")
    return None


def _legacy_library_sections(library: Tag, title: str) -> dict:
    """Read the primary XBRL groups and their bounded, same-scope notes."""
    nodes: dict[tuple[str, str], list[Tag]] = {}
    active = None
    for node in library.find_all(recursive=False):
        group = re.fullmatch(r"\{XBRL\}(?:BS|IS|CF|EF)(_S)?[12]?", node.get("aclass", ""))
        if node.name == "table-group" and group:
            header = node.find("table", attrs={"border": "0"})
            explicit_title = node.find("title", recursive=False)
            first = (explicit_title.get_text("\n", strip=True).splitlines() if explicit_title
                     else header.get_text("\n", strip=True).splitlines() if header else [])
            caption = re.sub(r"\s+", "", first[0]) if first else ""
            matched = re.fullmatch(r"(연결|별도|개별)?(?:재무상태표(?:\(대차대조표\))?|대차대조표|포괄손익계산서|손익계산서|자본변동표|현금흐름표)", caption)
            if not matched:
                raise ValueError("Unidentified legacy OpenDART XBRL statement caption")
            scope = "CFS" if matched[1] == "연결" else "OFS"
            if group[1] and scope != "OFS":
                raise ValueError("Conflicting legacy OpenDART statement caption and scope")
            if (scope, "notes") in nodes:
                raise ValueError("Legacy OpenDART primary statement occurs inside its notes")
            active = (scope, "statement")
            nodes.setdefault(active, []).append(node)
            continue
        note_scope = _legacy_notes_scope(node, active[0] if active else None)
        if note_scope:
            if active != (note_scope, "statement"):
                raise ValueError("Legacy OpenDART notes do not follow their statement scope")
            active = (note_scope, "notes")
            nodes[active] = [node]
        elif active and active[1] == "notes":
            nodes[active].append(node)
    return {key: (_fragment(value), f"{title} / {key[0]} {key[1]}") for key, value in nodes.items()}


def _legacy_manual_sections(parent: Tag, title: str, embedded_scopes: set[str]) -> dict:
    nodes: dict[tuple[str, str], list[Tag]] = {}
    labels = {}
    active = None
    for node in parent.find_all(recursive=False):
        lines = [re.sub(r"\s+", "", line) for line in node.get_text("\n", strip=True).splitlines()] if node.name == "p" else []
        heading = re.fullmatch(r"(?:\d+|[가-힣])[.．](연결|별도|개별)?재무제표(?:에관한사항)?", lines[0]) if lines else None
        if heading:
            active = ("CFS" if heading[1] == "연결" else "OFS", "statement")
            if active in nodes:
                raise ValueError("Ambiguous legacy OpenDART financial scope")
            nodes[active] = [node]
            labels[active] = node.get_text("\n", strip=True).splitlines()[0]
            continue
        # These disclosures are outside the primary statements/notes in XI.
        # A boundary can share a P with an external-audit-report reference.
        if any(re.match(r"\d+[.．](?:대손충당금설정현황|재고자산(?:현황|의보유)|채무증권발행실적|사채관리계약|공정가치평가)", line) for line in lines):
            active = None
            continue
        default_scope = (active[0] if active and active[1] == "statement" else
                         next(iter(embedded_scopes)) if active is None and len(embedded_scopes) == 1 else None)
        note_scope = _legacy_notes_scope(node, default_scope)
        if note_scope:
            if ((note_scope, "notes") in nodes or
                    active != (note_scope, "statement") and note_scope not in embedded_scopes):
                raise ValueError("Legacy OpenDART notes do not follow their statement scope")
            active = (note_scope, "notes")
            nodes[active] = [node]
            labels[active] = node.get_text("\n", strip=True).splitlines()[0]
        elif active:
            nodes[active].append(node)
    return {key: (_fragment(value), f"{title} / {labels[key]}") for key, value in nodes.items()}


def _legacy_financial_sections(soup: BeautifulSoup) -> dict:
    matches = [heading for heading in soup.find_all("title")
               if re.fullmatch(r"(?:XI|Ⅺ|11)[.．]재무제표등", re.sub(r"\s+", "", heading.get_text()))
               and heading.parent.name.startswith("section")]
    if not matches:
        return {}
    if len(matches) != 1:
        raise ValueError("Ambiguous legacy OpenDART financial chapter")
    heading = matches[0]
    title = heading.get_text(" ", strip=True)
    libraries = [lib for lib in heading.parent.find_all("library")
                 if lib.find("table-group", attrs={"aclass": re.compile(r"^\{XBRL\}BS(?:_S)?$")}, recursive=False)]
    if len(libraries) > 1:
        raise ValueError("Ambiguous legacy OpenDART financial library")
    embedded = _legacy_library_sections(libraries[0], title) if libraries else {}
    scopes = {key[0] for key in embedded if key[1] == "statement"}
    manual = _legacy_manual_sections(heading.parent, title, scopes)
    for key, value in manual.items():
        if key in embedded:
            # A numbered outer heading may merely wrap the embedded library.
            # A following, separately captioned manual CFS must remain eligible.
            if key[1] == "statement" and value[0].find("library"):
                outside_tables = [table for table in value[0].find_all("table", attrs={"border": "1"})
                                  if table.find_parent("library") is None]
                if not outside_tables:
                    continue
            raise ValueError("Ambiguous embedded and manual OpenDART financial sections")
        embedded[key] = value
    return embedded


def read_dart_financial_document(path: str | Path, *, section: str = "statement") -> DartFinancialDocument:
    """Return the chosen statement/notes fragment; keep the raw file untouched.

    OpenDART's legacy markup includes its own entities and occasionally literal
    angle brackets in prose, so it is not consistently well-formed XML. Its
    table/section hierarchy is read as tolerant markup after strict decoding.
    Ordinary viewer HTML remains an already selected fragment.
    """
    if section not in {"statement", "notes"}:
        raise ValueError("section must be statement or notes")
    raw = Path(path).read_bytes()
    text, encoding = _decode(raw)
    digest = sha256(raw).hexdigest()
    if not re.search(r"<DOCUMENT(?:\s|>)", text, re.I):
        return DartFinancialDocument(text, encoding, digest)
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1)
    # &cr; separates packed cell lines; removing it would misalign amounts.
    text = text.replace("&cr;", "<br/>")
    soup = BeautifulSoup(text, "html.parser")
    titles = {
        "연결재무제표": ("CFS", "statement"),
        "재무제표": ("OFS", "statement"),
        "별도재무제표": ("OFS", "statement"),
        "개별재무제표": ("OFS", "statement"),
        "연결재무제표주석": ("CFS", "notes"),
        "재무제표주석": ("OFS", "notes"),
        "별도재무제표주석": ("OFS", "notes"),
        "개별재무제표주석": ("OFS", "notes"),
    }
    sections = {}
    for heading in soup.find_all("title"):
        key = titles.get(_title(heading.get_text(" ", strip=True)))
        if key is None or not heading.parent.name.startswith("section"):
            continue
        if key in sections:
            raise ValueError(f"Ambiguous OpenDART financial section: {key}")
        sections[key] = (heading.parent, heading.get_text(" ", strip=True))
    if not sections:
        sections = _legacy_financial_sections(soup)
    scope = None
    for candidate in ("CFS", "OFS"):
        statement = sections.get((candidate, "statement"))
        if statement and _has_statement_table(statement[0]):
            scope = candidate
            break
        if candidate == "CFS" and statement:
            content = re.sub(r"\s+", "", statement[0].get_text(" ", strip=True))
            if not any(text in content for text in ("해당사항없", "해당사항이없", "해당없", "작성하지않", "작성대상아")):
                raise ValueError("OpenDART consolidated section has no table or explicit non-applicability")
    if scope is None:
        raise ValueError("No identified OpenDART financial statement section")
    selected = sections.get((scope, section))
    if selected is None:
        if section == "notes":
            return DartFinancialDocument("<html><body></body></html>", encoding, digest, "", scope)
        raise ValueError("Missing OpenDART financial statement section")
    fragment, heading_text = selected
    # OpenDART uses TE for editable cells with the same displayed table value.
    for cell in fragment.find_all("te"):
        cell.name = "td"
    # DART's viewer renders each pair of source spaces as space + NBSP.
    # The statement context parser relies on this retained indentation to
    # distinguish parent totals from their component accounts.
    for node in list(fragment.descendants):
        if isinstance(node, NavigableString) and node.strip() and node.find_parent(["td", "th"]):
            node.replace_with(str(node).replace("  ", " \u00a0"))
    for heading in fragment.find_all("title"):
        if heading.parent.name == "table-group":
            heading.name = "p"
            heading["class"] = ["table-group-1"]
        else:
            heading.name = "h2"
    return DartFinancialDocument(str(fragment), encoding, digest, heading_text, scope)
