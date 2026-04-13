# 공통 파싱 유틸 / 시트 입출력 / RAW loader
import os
import re
import json
from typing import Dict, List, Tuple, Optional, Any

import gspread
import pandas as pd

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDENTIALS_JSON = (
    os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    or os.environ.get("GOOGLE_CREDS", "").strip()
)

RAW_SHEET_NAME = os.getenv("DUMP_SHEET_NAME", "RAW_dump")
RIGHTS_SHEET_NAME = os.getenv("RIGHTS_SHEET_NAME", "K_유상증자")
BOND_SHEET_NAME = os.getenv("BOND_SHEET_NAME", "K_주식연계채권")

RUN_ONLY_ACPTNO = os.getenv("RUN_ONLY_ACPTNO", "").strip()
def gs_open():
    if not GOOGLE_SHEET_ID or not GOOGLE_CREDENTIALS_JSON:
        raise RuntimeError(
            "GOOGLE_SHEET_ID / GOOGLE_CREDS(또는 GOOGLE_CREDENTIALS_JSON)가 비어있습니다."
        )
    creds = json.loads(GOOGLE_CREDENTIALS_JSON)
    gc = gspread.service_account_from_dict(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    return sh

def ensure_ws(sh, title: str, rows: int = 2000, cols: int = 60):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)

def ensure_header(ws, headers):
    current = ws.row_values(1)

    # 시트가 비어 있으면 헤더만 생성
    if not current:
        ws.update("A1", [headers])
        return

    # 기존 데이터가 있는 시트는 자동 삭제 금지
    if current != headers:
        raise RuntimeError(
            "헤더 불일치로 중단합니다. "
            "기존 데이터를 지우지 않기 위해 자동 clear를 막았습니다.\n"
            f"현재 헤더: {current}\n"
            f"기대 헤더: {headers}"
        )

def safe_cell(row: List[str], idx: int) -> str:
    return row[idx] if idx < len(row) else ""

def load_raw_records(raw_ws) -> List[Dict[str, Any]]:
    values = raw_ws.get_all_values()
    if not values:
        return []

    by_acpt: Dict[str, List[List[str]]] = {}
    for row in values:
        acpt_no = safe_cell(row, 0).strip()
        if not acpt_no or not acpt_no.isdigit():
            continue
        by_acpt.setdefault(acpt_no, []).append(row)

    records = []
    for acpt_no, rows in by_acpt.items():
        meta = {
            "acpt_no": acpt_no,
            "category": "",
            "title": "",
            "src_url": "",
            "run_ts": "",
        }
        table_buckets: Dict[str, Dict[str, Any]] = {}

        for row in rows:
            row_type = safe_cell(row, 2).strip()

            if row_type == "META":
                meta["category"] = safe_cell(row, 3)
                meta["title"] = safe_cell(row, 4)
                meta["src_url"] = safe_cell(row, 5)
                meta["run_ts"] = safe_cell(row, 6)

            elif row_type == "HEADER":
                tix = safe_cell(row, 1).strip()
                table_buckets.setdefault(tix, {"header": [], "data": []})
                table_buckets[tix]["header"] = row[3:]

            elif row_type == "DATA":
                tix = safe_cell(row, 1).strip()
                table_buckets.setdefault(tix, {"header": [], "data": []})
                table_buckets[tix]["data"].append(row[3:])

        dfs = []
        for tix in sorted(
            table_buckets.keys(),
            key=lambda x: int(x) if str(x).isdigit() else 999999,
        ):
            header = table_buckets[tix]["header"]
            data = table_buckets[tix]["data"]

            width = max(len(header), max((len(r) for r in data), default=0))
            if width == 0:
                continue

            header = header + [f"col_{i}" for i in range(len(header), width)]
            norm_data = [r + [""] * (width - len(r)) for r in data]
            dfs.append(pd.DataFrame(norm_data, columns=header))

        records.append(
            {
                "acpt_no": meta["acpt_no"],
                "category": meta["category"],
                "title": meta["title"],
                "src_url": meta["src_url"],
                "run_ts": meta["run_ts"],
                "tables": dfs,
            }
        )

    records.sort(key=lambda x: x["acpt_no"])
    return records

def normalize_text(x: Any) -> str:
    if x is None:
        return ""
    s = str(x).replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def _norm(s: Any) -> str:
    return re.sub(r"\s+", "", str(s or "")).replace(":", "")

def _clean_label(s: Any) -> str:
    return re.sub(r"^([①-⑩]|\(\d+\)|\d+\.)+", "", _norm(s))

def _single_line(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()

def _norm_date(s: Any) -> str:
    return re.sub(r"[^\d]", "", str(s or ""))

def _format_date(s: Any) -> str:
    txt = _single_line(s)

    m = re.search(r"(\d{4})[-년\./\s]+(\d{1,2})[-월\./\s]+(\d{1,2})", txt)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    m2 = re.search(r"\b(\d{4})(\d{2})(\d{2})\b", txt)
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"

    return txt

def norm_company_name(name: str) -> str:
    if not name:
        return ""
    n = name.replace("주식회사", "").replace("(주)", "").replace("㈜", "")
    return _norm(n)

def first_nonempty(*vals):
    for v in vals:
        if normalize_text(v):
            return normalize_text(v)
    return ""

def contains_any(text: str, keywords: List[str]) -> bool:
    return any(k in text for k in keywords)

def parse_float_like(s):
    if s is None:
        return None
    t = re.sub(r"[^\d\.\-]", "", str(s).replace(",", ""))
    if t in ("", "-", "."):
        return None
    try:
        return float(t)
    except Exception:
        return None

def parse_int(value: Any):
    s = normalize_text(value).replace(",", "")
    m = re.search(r"-?\d+", s)
    return int(m.group(0)) if m else None

def parse_float(value: Any):
    s = normalize_text(value).replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None

def _to_int(s: Any) -> Optional[int]:
    if s is None:
        return None
    t = re.sub(r"[^\d\-]", "", str(s).replace(",", ""))
    if t in ("", "-"):
        return None
    try:
        return int(t)
    except Exception:
        return None

def _to_float(s: Any) -> Optional[float]:
    if s is None:
        return None
    t = re.sub(r"[^\d\.\-]", "", str(s).replace(",", ""))
    if t in ("", "-", "."):
        return None
    try:
        return float(t)
    except Exception:
        return None

def _max_int_in_text(s: Any) -> Optional[int]:
    if not s:
        return None
    s_clean = re.sub(r"(^|\s)[\(①-⑩]?\s*\d+\s*[\.\)]\s+", " ", str(s))
    nums = re.findall(r"\d{1,3}(?:[,.]\d{3})+(?!\d)|\d+", s_clean)
    vals = []
    for x in nums:
        t = re.sub(r"[,.]", "", x)
        if t.isdigit():
            vals.append(int(t))
    return max(vals) if vals else None

def clean_percent(value: str) -> str:
    s = normalize_text(value)
    if not s:
        return ""
    if "%" in s:
        m = re.search(r"-?\d+(?:\.\d+)?\s*%", s)
        return m.group(0).replace(" ", "") if m else s
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
    return f"{m.group(0)}%" if m else s

def fmt_number(x):
    if x in (None, ""):
        return ""
    try:
        fx = float(x)
    except Exception:
        return str(x)
    if abs(fx - round(fx)) < 1e-9:
        return f"{int(round(fx)):,}"
    return f"{fx:,.2f}"

def fmt_eok_from_won(won):
    if won is None:
        return ""
    return f"{won / 100000000:.2f}"

def clean_title(title: str) -> str:
    return _single_line(title).replace("[자동복구대상]", "").strip()

def is_correction_title(title: str) -> bool:
    t = clean_title(title)
    return t.startswith("[정정]") or t.startswith("정정") or "[정정]" in t or "정정" in t

def normalize_market_value(value: Any) -> str:
    s = normalize_text(value)
    if not s:
        return ""

    n = _norm(s)
    n_low = n.lower()

    if (
        "코스닥" in s
        or "코스닥시장" in s
        or "[코]" in s
        or "kosdaq" in n_low
        or n in ["코", "코스닥", "코스닥시장"]
    ):
        return "코스닥"

    if (
        "유가증권" in s
        or "유가증권시장" in s
        or "코스피" in s
        or "[유]" in s
        or "kospi" in n_low
        or n in ["유", "유가증권", "유가증권시장", "코스피"]
    ):
        return "유가증권"

    if (
        "코넥스" in s
        or "코넥스시장" in s
        or "코넥스 상장" in s
        or "konex" in n_low
        or "[넥]" in s
        or "[코넥]" in s
        or "[코넥스]" in s
        or n in ["넥", "코넥", "코넥스", "코넥스시장"]
    ):
        return "코넥스"

    if "비상장" in s or n == "비상장":
        return "비상장"

    return ""

def detect_market_from_title(title: str) -> str:
    t = normalize_text(title)

    if re.match(r"^\[(유|KOSPI)\]", t, flags=re.IGNORECASE):
        return "유가증권"
    if re.match(r"^\[(코|KOSDAQ)\]", t, flags=re.IGNORECASE):
        return "코스닥"
    if re.match(r"^\[(넥|코넥|코넥스|KONEX)\]", t, flags=re.IGNORECASE):
        return "코넥스"

    return normalize_market_value(t)

def detect_report_family(title: str) -> str:
    for k in [
        "유무상증자결정",
        "유상증자결정",
        "무상증자결정",
        "전환사채권발행결정",
        "교환사채권발행결정",
        "신주인수권부사채권발행결정",
    ]:
        if k in title.replace(" ", ""):
            return k
    return ""

def extract_company_name_from_title(title: str) -> str:
    t = clean_title(title)
    t = re.sub(r"^\[(유|코|넥|코넥|KOSPI|KOSDAQ|KONEX)\]\s*", "", t).strip()
    t = re.sub(r"\[정정\]\s*", "", t).strip()

    for k in [
        "유무상증자결정",
        "유상증자결정",
        "무상증자결정",
        "전환사채권발행결정",
        "교환사채권발행결정",
        "신주인수권부사채권발행결정",
    ]:
        if k in t.replace(" ", ""):
            m = re.search(rf"^(.*?)\s*{k}", t)
            if m:
                return m.group(1).strip()

    parts = t.split()
    if not parts:
        return ""
    if len(parts) >= 2 and parts[0] in ("주식회사", "(주)", "㈜"):
        return f"{parts[0]} {parts[1]}".strip()
    return parts[0].strip()

def looks_like_valid_date(v: str) -> bool:
    v = _single_line(v)
    if not re.search(r"\d", v):
        return False

    bad_kws = [
        "정정",
        "변경",
        "요청",
        "사유",
        "기재",
        "오기",
        "추가상장",
        "상장주식",
        "총수",
        "교부예정일",
        "사항",
        "기준",
        "발행",
        "항목",
    ]
    if any(b in v for b in bad_kws):
        return False

    if not (
        re.search(r"\d{4}", v) or re.search(r"\d{2,4}[\.\-\/년]\s*\d{1,2}", v)
    ):
        return False

    return True

def all_text_lines(tables: List[pd.DataFrame]) -> List[str]:
    lines = []
    for df in tables:
        arr = df.fillna("").astype(str).values.tolist()
        for row in arr:
            joined = " | ".join([normalize_text(x) for x in row if normalize_text(x)])
            if joined:
                lines.append(joined)
    return lines

def flatten_table_text(table: pd.DataFrame) -> str:
    parts = []
    arr = table.fillna("").astype(str).values.tolist()
    for row in arr:
        for cell in row:
            s = normalize_text(cell)
            if s:
                parts.append(s)
    return " ".join(parts)

def df_to_pairs(df: pd.DataFrame) -> List[Tuple[str, str]]:
    pairs = []
    arr = df.fillna("").astype(str).values.tolist()
    for row in arr:
        row = [normalize_text(x) for x in row]
        if len(row) < 2:
            continue
        for i in range(len(row) - 1):
            left = row[i].strip()
            right = row[i + 1].strip()
            if left:
                pairs.append((left, right))
    return pairs

def all_pairs_from_tables(tables: List[pd.DataFrame]) -> List[Tuple[str, str]]:
    out = []
    for df in tables:
        out.extend(df_to_pairs(df))
    return out

def find_value_by_left_keywords(pairs: List[Tuple[str, str]], keywords: List[str]) -> str:
    for left, right in pairs:
        if contains_any(left, keywords) and normalize_text(right):
            return normalize_text(right)
    return ""

def find_numeric_value_by_keywords(pairs: List[Tuple[str, str]], keywords: List[str]):
    return parse_float(find_value_by_left_keywords(pairs, keywords))

def extract_correction_after_map(dfs: List[pd.DataFrame]) -> Dict[str, str]:
    out: Dict[str, str] = {}

    def _is_invalid_corr_value(v: Any) -> bool:
        txt = _single_line(v)
        nv = _norm(txt)

        if nv in ("", "정정후", "정정전", "변경후", "변경전", "항목", "변경사유", "정정사유", "-"):
            return True

        if re.search(r"^주\s*\d+\)\s*정정(?:전|후)$", txt):
            return True
        if re.search(r"^정정(?:전|후)\s*참조$", txt):
            return True
        if "원문참조" in txt or "공시확인바람" in txt:
            return True

        return False

    for df in dfs:
        try:
            arr = df.astype(str).values
        except Exception:
            continue

        R, C = arr.shape
        header_r = after_col = item_col = None

        for r in range(R):
            row_norm = [_norm(x) for x in arr[r].tolist()]
            has_before = any(w in x for w in ["정정전", "변경전"] for x in row_norm)
            has_after = any(w in x for w in ["정정후", "변경후"] for x in row_norm)

            if has_before and has_after:
                header_r = r
                after_col = next(
                    (i for i, x in enumerate(row_norm) if "정정후" in x or "변경후" in x),
                    None,
                )
                item_col = next(
                    (
                        i
                        for i, x in enumerate(row_norm)
                        if ("정정사항" in x or "항목" in x or "구분" in x)
                    ),
                    0,
                )
                break

        if header_r is None or after_col is None:
            continue

        last_item = ""
        for rr in range(header_r + 1, R):
            item = str(arr[rr][item_col]).strip() if item_col is not None and item_col < C else ""
            item = item if item and item.lower() != "nan" else last_item
            if not item:
                continue

            last_item = item
            after_val = ""

            if 0 <= after_col < C:
                v = str(arr[rr][after_col]).strip()
                if (
                    v
                    and v.lower() != "nan"
                    and not _is_invalid_corr_value(v)
                ):
                    after_val = _single_line(v)

            if not after_val:
                tail_vals = []
                for cc in range((item_col or 0) + 1, C):
                    v = str(arr[rr][cc]).strip()
                    if (
                        v
                        and v.lower() != "nan"
                        and not _is_invalid_corr_value(v)
                    ):
                        tail_vals.append(_single_line(v))

                if len(tail_vals) >= 2:
                    after_val = tail_vals[-1]

            if after_val:
                out[_norm(item)] = after_val
                out[_clean_label(item)] = after_val

    return out

def scan_label_value(dfs: List[pd.DataFrame], label_candidates: List[str]) -> str:
    cand_clean = {_clean_label(x) for x in label_candidates}

    for df in dfs:
        try:
            arr = df.astype(str).values
        except Exception:
            continue

        R, C = arr.shape
        for r in range(R):
            for c in range(C):
                if _clean_label(arr[r][c]) in cand_clean:
                    checks = []
                    for rr, cc in [(r, c + 1), (r, c + 2), (r + 1, c), (r + 1, c + 1)]:
                        if 0 <= rr < R and 0 <= cc < C:
                            checks.append(str(arr[rr][cc]).strip())

                    row_vals = [str(x).strip() for x in arr[r].tolist() if str(x).strip()]

                    for v in [v for v in checks + row_vals if v and v.lower() != "nan"]:
                        if _clean_label(v) in cand_clean:
                            continue
                        if re.fullmatch(r"([①-⑩]|\(\d+\)|\d+\.)", _norm(v)):
                            continue
                        return _single_line(v)

    return ""

def scan_label_value_preferring_correction(
    dfs: List[pd.DataFrame],
    label_candidates: List[str],
    corr_after: Dict[str, str],
) -> str:
    cand_clean = {_clean_label(x) for x in label_candidates}

    if corr_after:
        for c in cand_clean:
            if c in corr_after and str(corr_after[c]).strip():
                return _single_line(str(corr_after[c]))
        for k, v in corr_after.items():
            if str(v).strip() and any(c in k for c in cand_clean):
                return _single_line(str(v))

    return scan_label_value(dfs, label_candidates)

def _normalize_bond_method_value(text: str) -> str:
    t = normalize_text(text)
    if not t:
        return ""

    if "사모" in t and "공모" not in t:
        return "사모"
    if "공모" in t and "사모" not in t:
        return "공모"
    if "제3자" in t and "배정" in t:
        return "제3자배정"

    return ""

def find_row_best_int(
    dfs: List[pd.DataFrame],
    must_contain: List[str],
    min_val: int = 0,
) -> Optional[int]:
    keys = [_norm(x) for x in must_contain]
    best = None

    for df in dfs:
        try:
            arr = df.astype(str).values
        except Exception:
            continue

        for r in range(arr.shape[0]):
            row = [str(x).strip() for x in arr[r].tolist()]
            if all(k in _norm("".join(row)) for k in keys):
                valid_amts = []
                for cell in row:
                    if any(d in cell for d in ["년", "월", "일", "예정일", "납입일", "기일"]):
                        continue
                    amt = _max_int_in_text(cell)
                    if amt is not None and amt > min_val:
                        valid_amts.append(amt)
                if valid_amts:
                    best = valid_amts[-1]

    return best

def find_row_best_float(dfs: List[pd.DataFrame], must_contain: List[str]) -> Optional[float]:
    keys = [_norm(x) for x in must_contain]

    for df in dfs:
        try:
            arr = df.astype(str).values
        except Exception:
            continue

        for r in range(arr.shape[0]):
            row = [str(x).strip() for x in arr[r].tolist()]
            if all(k in _norm("".join(row)) for k in keys):
                vals = [x for x in [_to_float(x) for x in row] if x is not None]
                if vals:
                    return max(vals, key=lambda z: abs(z))

    return None

def get_valid_date_by_labels(
    dfs: List[pd.DataFrame],
    labels: List[str],
    corr_after: Optional[Dict[str, str]] = None,
) -> str:
    cand_clean = {_clean_label(x) for x in labels}

    if corr_after:
        for k, v in corr_after.items():
            if any(c in k for c in cand_clean):
                if looks_like_valid_date(v):
                    return _format_date(v)

    for df in dfs:
        try:
            arr = df.astype(str).values
        except Exception:
            continue

        R, C = arr.shape
        for r in range(R):
            row_vals = [
                str(x).strip()
                for x in arr[r].tolist()
                if str(x).strip() and str(x).strip().lower() != "nan"
            ]

            if any(_clean_label(x) in cand_clean for x in row_vals):
                possible_dates = []
                for v in row_vals:
                    if _clean_label(v) in cand_clean:
                        continue
                    if re.fullmatch(r"([①-⑩]|\(\d+\)|\d+\.)", _norm(v)):
                        continue
                    if looks_like_valid_date(v):
                        possible_dates.append(v)
                if possible_dates:
                    return _format_date(possible_dates[-1])

    val = scan_label_value(dfs, labels)
    if looks_like_valid_date(val):
        return _format_date(val)
    return ""

def detect_market_from_tables(
    dfs: List[pd.DataFrame],
    corr_after: Optional[Dict[str, str]] = None,
) -> str:
    market_labels = [
        "상장시장",
        "시장구분",
        "주권상장구분",
        "상장구분",
        "주권상장시장",
        "상장 여부",
        "상장여부",
    ]
    label_set = {_clean_label(x) for x in market_labels}

    if corr_after:
        for k, v in corr_after.items():
            k_clean = _clean_label(k)
            k_norm = _norm(k)
            if k_clean in label_set or any(_norm(lb) in k_norm for lb in market_labels):
                market = normalize_market_value(v)
                if market:
                    return market

    for df in dfs:
        try:
            arr = df.fillna("").astype(str).values
        except Exception:
            continue

        R, C = arr.shape
        for r in range(R):
            for c in range(C):
                cell = normalize_text(arr[r][c])
                if not cell:
                    continue
                if _clean_label(cell) not in label_set:
                    continue

                candidates = []
                for rr, cc in [
                    (r, c + 1),
                    (r, c + 2),
                    (r + 1, c),
                    (r + 1, c + 1),
                    (r + 1, c + 2),
                ]:
                    if 0 <= rr < R and 0 <= cc < C:
                        candidates.append(arr[rr][cc])

                row_text = " ".join([normalize_text(x) for x in arr[r].tolist() if normalize_text(x)])
                if row_text:
                    candidates.append(row_text)

                if r + 1 < R:
                    next_row_text = " ".join(
                        [normalize_text(x) for x in arr[r + 1].tolist() if normalize_text(x)]
                    )
                    if next_row_text:
                        candidates.append(next_row_text)

                for cand in candidates:
                    market = normalize_market_value(cand)
                    if market:
                        return market

    for line in all_text_lines(dfs):
        line_norm = _norm(line)
        if any(_norm(lb) in line_norm for lb in market_labels):
            market = normalize_market_value(line)
            if market:
                return market

    return ""

def detect_company_from_tables(
    dfs: List[pd.DataFrame],
    corr_after: Optional[Dict[str, str]] = None,
) -> str:
    labels = [
        "회사명",
        "회사 명",
        "발행회사",
        "발행회사명",
        "법인명",
        "종속회사명",
        "종속회사",
        "종속회사인",
    ]
    v = scan_label_value_preferring_correction(dfs, labels, corr_after or {})
    if not v:
        return ""

    v = v.split("\n")[0].strip()
    bad_kws = ["상장여부", "여부", "해당사항", "해당없음", "본점", "소재지", "신고", "경영사항", "결정"]
    if len(v) > 40 or any(k in v.replace(" ", "") for k in bad_kws) or v in ("-", "."):
        return ""
    return v

def find_row_by_key(ws, key_header: str, key_value: str) -> Optional[int]:
    vals = ws.get_all_values()
    if not vals:
        return None

    headers = vals[0]
    if key_header not in headers:
        return None

    idx = headers.index(key_header)
    for i, row in enumerate(vals[1:], start=2):
        if idx < len(row) and str(row[idx]).strip() == str(key_value).strip():
            return i
    return None

def find_event_row(
    ws,
    headers: List[str],
    row_dict: Dict[str, Any],
    sheet_type: str,
) -> Optional[int]:
    vals = ws.get_all_values()
    if not vals or len(vals) <= 1:
        return None

    hidx = {h: i for i, h in enumerate(headers)}
    target_company = norm_company_name(row_dict.get("회사명", ""))
    target_first = _norm_date(row_dict.get("최초 이사회결의일", ""))

    if not target_company or not target_first:
        return None

    target_type = _norm(row_dict.get("구분", "")) if sheet_type == "bond" else ""

    for i, row in enumerate(vals[1:], start=2):
        comp = norm_company_name(safe_cell(row, hidx.get("회사명", 0)))
        first = _norm_date(safe_cell(row, hidx.get("최초 이사회결의일", 0)))
        if comp != target_company or first != target_first:
            continue

        if sheet_type == "bond":
            btype = _norm(safe_cell(row, hidx.get("구분", 0)))
            if btype != target_type:
                continue

        return i

    return None

def upsert_structured_row(
    ws,
    headers: List[str],
    row_dict: Dict[str, Any],
    sheet_type: str,
):
    row_values = [row_dict.get(h, "") for h in headers]

    target_row = find_row_by_key(ws, "접수번호", str(row_dict.get("접수번호", "")))
    end_col = gspread.utils.rowcol_to_a1(1, len(headers)).rstrip("1")

    if target_row:
        if sheet_type == "bond":
            existing_row = ws.row_values(target_row)
            preserve_cols = ["Put Option", "Call Option", "Call 비율", "YTC", "행사(전환)가액(원)"]

            for col in preserve_cols:
                if col not in headers:
                    continue

                idx = headers.index(col)
                new_val = row_dict.get(col, "")
                old_val = safe_cell(existing_row, idx)

                if not normalize_text(new_val):
                    row_values[idx] = old_val

        # 기존 공시는 원래 위치에서만 업데이트
        ws.update(f"A{target_row}:{end_col}{target_row}", [row_values])
        return "UPDATE", target_row

    # 신규 공시만 헤더 바로 아래 삽입
    ws.insert_row(row_values, index=2, value_input_option="RAW")
    return "INSERT_TOP", 2
