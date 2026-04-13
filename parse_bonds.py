# 주식연계채권 전용 파서
from parse_common import (
    RAW_SHEET_NAME, BOND_SHEET_NAME, RUN_ONLY_ACPTNO,
    gs_open, ensure_ws, ensure_header, load_raw_records, upsert_structured_row,
    clean_title, is_correction_title, extract_correction_after_map,
    detect_company_from_tables, extract_company_name_from_title,
    detect_market_from_title, detect_market_from_tables,
    get_valid_date_by_labels, scan_label_value_preferring_correction,
    parse_float_like, _to_int, _to_float, _max_int_in_text,
    clean_percent, fmt_number, first_nonempty,
    _norm, normalize_text, find_row_best_int, find_row_best_float
)
from typing import Dict, List, Tuple, Optional, Any
import pandas as pd

BOND_HEADERS = [
    "구분",
    "회사명",
    "보고서명",
    "상장시장",
    "최초 이사회결의일",
    "권면총액(원)",
    "Coupon",
    "YTM",
    "만기",
    "전환청구 시작",
    "전환청구 종료",
    "Put Option",
    "Call Option",
    "Call 비율",
    "YTC",
    "모집방식",
    "발행상품",
    "행사(전환)가액(원)",
    "전환주식수",
    "주식총수대비 비율",
    "Refixing Floor",
    "납입일",
    "자금용도",
    "투자자",
    "링크",
    "접수번호",
]
def bond_type_code(title: str) -> str:
    t = title.replace(" ", "")
    if "전환사채권발행결정" in t:
        return "CB"
    if "교환사채권발행결정" in t:
        return "EB"
    if "신주인수권부사채권발행결정" in t:
        return "BW"
    return ""

def bond_type_product_name(title: str) -> str:
    t = title.replace(" ", "")
    if "전환사채권발행결정" in t:
        return "전환사채"
    if "교환사채권발행결정" in t:
        return "교환사채"
    if "신주인수권부사채권발행결정" in t:
        return "신주인수권부사채"
    return ""

def extract_product_type_bond(
    dfs: List[pd.DataFrame],
    corr_after: Dict[str, str],
    title: str,
) -> str:
    primary_labels = [
        "1. 사채의 종류",
        "1.사채의종류",
        "사채의 종류",
        "사채의종류",
        "채권의 종류",
        "채권의종류",
        "증권의 종류",
        "증권의종류",
    ]
    fallback_labels = [
        "사채종류",
        "발행상품",
        "종류",
    ]

    def clean_candidate(text: str) -> str:
        if not text:
            return ""
        t = normalize_text(text)
        if not t:
            return ""

        t = re.sub(r"^\s*1\.\s*", "", t)
        t = re.sub(r"^(사채|채권|증권)의\s*종류\s*[:：]?\s*", "", t)
        t = re.sub(r"^사채종류\s*[:：]?\s*", "", t)
        t = re.sub(r"^발행상품\s*[:：]?\s*", "", t)
        t = t.strip()

        bad_exact = {"", "-", ".", "해당사항없음", "해당 없음", "없음", "해당사항 없음"}
        if t in bad_exact:
            return ""

        patterns = [
            r"((?:제\s*\d+\s*회\s*)?[^|,;/]{0,80}?(?:전환사채|교환사채|신주인수권부사채))",
            r"((?:무기명식|기명식|이권부|무보증|보증|사모|공모|비분리형|분리형|사모식)?[^|,;/]{0,80}?(?:전환사채|교환사채|신주인수권부사채))",
        ]
        for pat in patterns:
            m = re.search(pat, t)
            if m:
                val = normalize_text(m.group(1))
                if 3 <= len(val) <= 100:
                    return val

        for name in ["전환사채", "교환사채", "신주인수권부사채"]:
            if name in t:
                return t

        return ""

    if corr_after:
        for k, v in corr_after.items():
            k_clean = _clean_label(k)
            if any(_clean_label(lb) == k_clean for lb in primary_labels):
                cleaned = clean_candidate(v)
                if cleaned:
                    return cleaned

        for k, v in corr_after.items():
            k_norm = _norm(k)
            if any(_norm(lb) in k_norm for lb in primary_labels + fallback_labels):
                cleaned = clean_candidate(v)
                if cleaned:
                    return cleaned

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

                cell_clean = _clean_label(cell)
                is_primary = any(_clean_label(lb) == cell_clean for lb in primary_labels)
                is_fallback = any(_clean_label(lb) == cell_clean for lb in fallback_labels)

                if not (is_primary or is_fallback):
                    continue

                for cc in range(c + 1, min(C, c + 5)):
                    candidate = clean_candidate(arr[r][cc])
                    if candidate:
                        return candidate

                for rr in range(r + 1, min(R, r + 4)):
                    candidate = clean_candidate(arr[rr][c])
                    if candidate:
                        return candidate

                for rr in range(r + 1, min(R, r + 4)):
                    for cc in range(c + 1, min(C, c + 4)):
                        candidate = clean_candidate(arr[rr][cc])
                        if candidate:
                            return candidate

                row_text = " ".join([normalize_text(x) for x in arr[r].tolist() if normalize_text(x)])
                candidate = clean_candidate(row_text)
                if candidate:
                    return candidate

    for df in dfs:
        try:
            arr = df.fillna("").astype(str).values
        except Exception:
            continue

        for r in range(min(12, arr.shape[0])):
            row_text = " ".join([normalize_text(x) for x in arr[r].tolist() if normalize_text(x)])
            if "사채의 종류" in row_text or "사채종류" in row_text:
                candidate = clean_candidate(row_text)
                if candidate:
                    return candidate

    return bond_type_product_name(title)

def extract_payment_date_bond(dfs: List[pd.DataFrame], corr_after: Dict[str, str]) -> str:
    if corr_after:
        for k, v in corr_after.items():
            if "납입" in k:
                pay_idx = v.find("납입") if "납입" in v else 0
                dates = re.findall(r"\d{4}[-년\.\s]+\d{1,2}[-월\.\s]+\d{1,2}", v[pay_idx:])
                if dates:
                    return _format_date(dates[-1])

    for df in dfs:
        try:
            arr = df.astype(str).values
        except Exception:
            continue

        R, C = arr.shape
        for r in range(R):
            row_str = " ".join([str(x) for x in arr[r] if str(x).lower() != "nan"])
            if "납입일" in _norm(row_str) or "납입기일" in _norm(row_str):
                pay_idx = row_str.find("납입")
                dates = re.findall(r"\d{4}[-년\.\s]+\d{1,2}[-월\.\s]+\d{1,2}", row_str[pay_idx:])
                if dates:
                    return _format_date(dates[-1])

                dates = re.findall(r"\d{4}[-년\.\s]+\d{1,2}[-월\.\s]+\d{1,2}", row_str)
                if dates:
                    return _format_date(dates[-1])

                if r + 1 < R:
                    next_row = " ".join([str(x) for x in arr[r + 1] if str(x).lower() != "nan"])
                    dates = re.findall(r"\d{4}[-년\.\s]+\d{1,2}[-월\.\s]+\d{1,2}", next_row)
                    if dates:
                        return _format_date(dates[-1])

    return ""

def extract_fund_usage_bond(dfs: List[pd.DataFrame], corr_after: Dict[str, str]) -> str:
    target_keys = [
        "시설자금",
        "영업양수자금",
        "운영자금",
        "채무상환자금",
        "타법인 증권 취득자금",
        "타법인증권취득자금",
        "기타자금",
    ]

    for df in dfs:
        found_funds = {}
        try:
            arr = df.astype(str).values
        except Exception:
            continue

        R, C = arr.shape
        for r in range(R):
            for c in range(C):
                cell_norm = _norm(str(arr[r][c]))
                for tk in target_keys:
                    if _norm(tk) in cell_norm:
                        amt = 0
                        for cc in range(c + 1, min(C, c + 3)):
                            a = _max_int_in_text(arr[r][cc])
                            if a and a > 100:
                                amt = max(amt, a)
                        if amt == 0 and r + 1 < R:
                            a = _max_int_in_text(arr[r + 1][c])
                            if a and a > 100:
                                amt = max(amt, a)

                        if amt > 0:
                            std_key = "타법인 증권 취득자금" if "타법인" in tk else tk
                            found_funds[std_key] = max(found_funds.get(std_key, 0), amt)

        if found_funds:
            result = [k for k, v in sorted(found_funds.items(), key=lambda x: x[1], reverse=True)]
            return _single_line(", ".join(result))

    if corr_after:
        found_funds = {}
        for k, v in corr_after.items():
            for tk in target_keys:
                if _norm(tk) in _norm(k):
                    amt = _max_int_in_text(v)
                    if amt and amt > 100:
                        std_key = "타법인 증권 취득자금" if "타법인" in tk else tk
                        found_funds[std_key] = amt

        if found_funds:
            result = [k for k, v in sorted(found_funds.items(), key=lambda x: x[1], reverse=True)]
            return _single_line(", ".join(result))

    return _single_line(
        scan_label_value_preferring_correction(dfs, ["조달자금의 구체적 사용 목적", "자금용도"], corr_after)
    )

def extract_investors_bond(dfs: List[pd.DataFrame], corr_after: Dict[str, str]) -> str:
    investors = []
    blacklist = [
        "관계",
        "배정",
        "비고",
        "합계",
        "소계",
        "해당사항",
        "내역",
        "금액",
        "주식수",
        "단위",
        "이사회",
        "총계",
        "주소",
        "근거",
        "선정경위",
        "거래내역",
        "목적",
        "취득내역",
        "잔고",
        "출자자수",
        "주요사항",
    ]

    def clean_investor_name(sn):
        if not sn or str(sn).lower() == "nan":
            return ""
        s = str(sn).replace("\n", " ").replace("\r", "").strip()
        s = re.sub(r"\([^)]*신탁업자[^)]*\)", "", s)
        s = re.sub(r"\([^)]*본건펀드[^)]*\)", "", s)
        s = re.sub(r"\([^)]*전문투자자[^)]*\)", "", s)
        s = re.sub(r"\([^)]*손익차등[^)]*\)", "", s)
        s = re.sub(r"주\s*\d+\)", "", s)
        return re.sub(r"\s+", " ", s).strip()

    def is_valid_investor_name(sn):
        if not sn:
            return False
        sn_clean = sn.replace(" ", "")
        if not (2 <= len(sn_clean) <= 50):
            return False
        if re.fullmatch(r"[\d,\.\s\-]+", sn_clean):
            return False
        sn_norm = _norm(sn_clean)
        for bw in blacklist:
            if bw in sn_norm:
                return False
        return True

    target_col_kws = [
        "대상자명",
        "대상사명",
        "성명",
        "법인명",
        "인수인",
        "투자기구",
        "투자업자",
        "발행대상",
        "투자자",
    ]

    for df in dfs:
        try:
            arr = df.astype(str).values
        except Exception:
            continue

        R, C = arr.shape
        found_cols = []
        start_row = 1

        for r in range(min(5, R)):
            for c in range(C):
                cell_v = _norm(arr[r][c])
                if any(kw in cell_v for kw in target_col_kws):
                    if "최대주주" in cell_v or "대표이사" in cell_v:
                        continue
                    found_cols.append(c)
            if found_cols:
                start_row = r + 1
                break

        for col_idx in found_cols:
            for rr in range(start_row, R):
                cell_data = str(arr[rr][col_idx])
                valid_found = False

                for line in cell_data.split("\n"):
                    c_line = clean_investor_name(line)
                    if is_valid_investor_name(c_line):
                        if c_line not in investors:
                            investors.append(c_line)
                        valid_found = True

                if not valid_found:
                    c_whole = clean_investor_name(cell_data.replace("\n", " "))
                    if is_valid_investor_name(c_whole) and c_whole not in investors:
                        investors.append(c_whole)

    if not investors and corr_after:
        for k, v in corr_after.items():
            if any(_norm(kw) in _norm(k) for kw in ["발행대상자", "배정대상자", "투자자", "인수인", "대상자"]):
                for chunk in re.split(r"[,;/]", v.replace("\n", ",")):
                    c_name = clean_investor_name(chunk)
                    if is_valid_investor_name(c_name) and c_name not in investors:
                        investors.append(c_name)

    if not investors:
        val = scan_label_value_preferring_correction(
            dfs,
            ["발행대상자", "배정대상자", "투자자", "성명(법인명)", "인수인"],
            corr_after,
        )
        if val:
            for chunk in re.split(r"[,;/]", val.replace("\n", ",")):
                c_name = clean_investor_name(chunk)
                if is_valid_investor_name(c_name) and c_name not in investors:
                    investors.append(c_name)

    if not investors:
        for df in dfs:
            try:
                arr = df.astype(str).values
            except Exception:
                continue

            for r in range(arr.shape[0]):
                for c in range(arr.shape[1]):
                    cell_val = clean_investor_name(str(arr[r][c]).replace("\n", " "))
                    if re.search(r"(투자조합|사모투자|펀드|파트너스|인베스트먼트|자산운용|증권)", cell_val):
                        if is_valid_investor_name(cell_val) and cell_val not in investors:
                            investors.append(cell_val)

    final_investors = []
    for inv in investors:
        if inv and inv not in final_investors:
            final_investors.append(inv)

    return _single_line(", ".join(final_investors[:15]))

def _extract_dates_from_text(text: str) -> List[str]:
    if not text:
        return []

    found = []

    for m in re.finditer(r"(\d{4})[-년\./\s]+(\d{1,2})[-월\./\s]+(\d{1,2})", str(text)):
        d = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        found.append(d)

    for m in re.finditer(r"\b(\d{4})(\d{2})(\d{2})\b", str(text)):
        d = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        found.append(d)

    uniq = []
    for d in found:
        if d not in uniq:
            uniq.append(d)
    return uniq

def extract_period_dates_from_tables(
    dfs: List[pd.DataFrame],
    corr_after: Dict[str, str],
    period_keywords: List[str],
) -> Tuple[str, str]:
    """
    전환청구기간 / 교환청구기간 / 권리행사기간에서 시작일, 종료일 추출
    """

    keys = [_norm(x) for x in period_keywords if x]

    def _match_period_label(text: str) -> bool:
        t = _norm(text)
        return any(k in t for k in keys)

    if corr_after:
        for k, v in corr_after.items():
            if _match_period_label(k):
                dates = _extract_dates_from_text(f"{k} {v}")
                if len(dates) >= 2:
                    return dates[0], dates[1]
                if len(dates) == 1:
                    return dates[0], ""

    for df in dfs:
        try:
            arr = df.fillna("").astype(str).values
        except Exception:
            continue

        R, C = arr.shape
        for r in range(R):
            row_text = " ".join([normalize_text(x) for x in arr[r].tolist() if normalize_text(x)])
            if not row_text:
                continue

            if _match_period_label(row_text):
                block = [row_text]

                for rr in range(r + 1, min(r + 3, R)):
                    next_row = " ".join([normalize_text(x) for x in arr[rr].tolist() if normalize_text(x)])
                    if next_row:
                        block.append(next_row)

                dates = _extract_dates_from_text(" ".join(block))
                if len(dates) >= 2:
                    return dates[0], dates[1]
                if len(dates) == 1:
                    return dates[0], ""

    val = scan_label_value_preferring_correction(
        dfs,
        period_keywords,
        corr_after,
    )
    dates = _extract_dates_from_text(val)
    if len(dates) >= 2:
        return dates[0], dates[1]
    if len(dates) == 1:
        return dates[0], ""

    lines = all_text_lines(dfs)
    for i, line in enumerate(lines):
        if _match_period_label(line):
            merged = line
            if i + 1 < len(lines):
                merged += " " + lines[i + 1]
            if i + 2 < len(lines):
                merged += " " + lines[i + 2]

            dates = _extract_dates_from_text(merged)
            if len(dates) >= 2:
                return dates[0], dates[1]
            if len(dates) == 1:
                return dates[0], ""

    return "", ""

def _extract_share_ratio_pair_from_text(
    text: str,
    share_labels: List[str],
    ratio_labels: List[str],
) -> Tuple[str, str]:
    """
    예:
    - '5,000,000 3.7' -> ('5,000,000', '3.7%')
    - '전환에 따라 발행할 주식수 5,000,000 주식총수 대비 비율(%) 3.7'
      -> ('5,000,000', '3.7%')
    """
    txt = normalize_text(text)
    if not txt:
        return "", ""

    txt = re.sub(r"20\d{2}[-년\./\s]+\d{1,2}[-월\./\s]+\d{1,2}", " ", txt)
    txt = re.sub(r"\b20\d{6}\b", " ", txt)
    txt = re.sub(r"주\s*\d+\)\s*", " ", txt)
    txt = normalize_text(txt)

    def _nums(sub: str) -> List[float]:
        toks = re.findall(r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?", sub)
        out = []
        for tok in toks:
            raw = tok.replace(",", "")
            try:
                val = float(raw) if "." in raw else int(raw)
                out.append(val)
            except Exception:
                continue
        return out

    share_val = ""
    ratio_val = ""

    for lb in share_labels:
        m = re.search(re.escape(lb), txt)
        if not m:
            continue

        sub = txt[m.end():]
        nums = _nums(sub)
        for v in nums:
            if float(v).is_integer():
                iv = int(v)
                if iv >= 1000 and iv not in [2024, 2025, 2026, 2027]:
                    share_val = f"{iv:,}"
                    break
        if share_val:
            break

    for lb in ratio_labels:
        m = re.search(re.escape(lb), txt)
        if not m:
            continue

        sub = txt[m.end():]
        nums = _nums(sub)
        cands = []
        for v in nums:
            fv = float(v)
            if 0 <= fv <= 100:
                cands.append(fv)

        if cands:
            ratio_val = f"{cands[-1]:g}%"
            break

    if share_val and ratio_val:
        return share_val, ratio_val

    nums = _nums(txt)
    if not nums:
        return share_val, ratio_val

    if not share_val:
        big_ints = []
        for v in nums:
            if float(v).is_integer():
                iv = int(v)
                if iv >= 1000 and iv not in [2024, 2025, 2026, 2027]:
                    big_ints.append(iv)
        if big_ints:
            share_val = f"{big_ints[0]:,}"

    if not ratio_val:
        small_nums = []
        for v in nums:
            fv = float(v)
            if 0 <= fv <= 100:
                small_nums.append(fv)
        if small_nums:
            ratio_val = f"{small_nums[-1]:g}%"

    return share_val, ratio_val

def _extract_share_ratio_from_footnotes(
    dfs: List[pd.DataFrame],
    share_labels: List[str],
    ratio_labels: List[str],
) -> Tuple[str, str]:
    """
    정정사항 표 아래쪽 footnote 블록(주1) / 주2) ...)까지 내려가서
    전환주식수 / 비율을 다시 확인
    """
    lines = [normalize_text(x) for x in all_text_lines(dfs) if normalize_text(x)]
    if not lines:
        return "", ""

    target_norms = [_norm(x) for x in share_labels + ratio_labels]

    for i, line in enumerate(lines):
        if not re.match(r"^주\s*\d+\)", line):
            continue

        block = [line]
        for j in range(i + 1, min(i + 6, len(lines))):
            nxt = lines[j]

            if re.match(r"^주\s*\d+\)", nxt):
                break
            if re.match(r"^\d+\s*[\.\)]\s*[가-힣A-Za-z]", nxt):
                break

            block.append(nxt)

        merged = " ".join(block)
        merged_norm = _norm(merged)

        if any(tn in merged_norm for tn in target_norms):
            s, r = _extract_share_ratio_pair_from_text(
                merged,
                share_labels,
                ratio_labels,
            )
            if s or r:
                return s, r

    for i in range(len(lines)):
        merged = " ".join(lines[i:i + 4])
        merged_norm = _norm(merged)

        if any(tn in merged_norm for tn in target_norms):
            s, r = _extract_share_ratio_pair_from_text(
                merged,
                share_labels,
                ratio_labels,
            )
            if s or r:
                return s, r

    return "", ""

def _first_nonempty_cell(row_vals) -> str:
    for x in row_vals:
        s = normalize_text(x)
        if s:
            return s
    return ""

def _is_new_top_heading(text: str) -> bool:
    raw = normalize_text(text)
    if not raw:
        return False
    return bool(re.match(r"^\d+\s*[\.\)]\s*[가-힣A-Za-z]", raw))

def _is_numbered_section_heading(text: str, section_no: int, heading_keywords: List[str]) -> bool:
    raw = normalize_text(text)
    n = _norm(raw)
    if not raw:
        return False

    for kw in heading_keywords:
        kw_n = _norm(kw)
        if re.match(rf"^{section_no}[\.\)]?{re.escape(kw_n)}$", n):
            return True
        if n.startswith(f"{section_no}{kw_n}"):
            return True
    return False

def _get_section_block_rows(
    dfs: List[pd.DataFrame],
    section_no: int,
    heading_keywords: List[str],
    max_rows: int = 10,
) -> List[List[str]]:
    """
    예: 8. 사채발행방법 / 9. 전환에 관한 사항 블록만 잘라서 반환
    """
    for df in dfs:
        try:
            arr = df.fillna("").astype(str).values
        except Exception:
            continue

        R, C = arr.shape
        for r in range(R):
            row_list = arr[r].tolist()
            first_cell = _first_nonempty_cell(row_list)
            row_join = " ".join([normalize_text(x) for x in row_list if normalize_text(x)])

            if not (
                _is_numbered_section_heading(first_cell, section_no, heading_keywords)
                or _is_numbered_section_heading(row_join, section_no, heading_keywords)
            ):
                continue

            block_rows = []
            for rr in range(r, min(r + max_rows, R)):
                next_row = arr[rr].tolist()
                next_first = _first_nonempty_cell(next_row)

                if rr > r and _is_new_top_heading(next_first):
                    break

                block_rows.append(next_row)

            if block_rows:
                return block_rows

    return []

def _clean_section_value_text(text: str, remove_labels: List[str]) -> str:
    t = normalize_text(text)
    if not t:
        return ""

    t = re.sub(r"^\d+\s*[\.\)]\s*", "", t)

    for lb in remove_labels:
        t = re.sub(rf"^{re.escape(lb)}\s*[:：]?\s*", "", t)
        t = re.sub(rf"{re.escape(lb)}\s*[:：]?\s*", "", t)

    t = normalize_text(t)

    if not t:
        return ""
    if re.fullmatch(r"[\d,\.\-%\s]+", t):
        return ""

    return t

def _extract_text_from_block_rows(
    block_rows: List[List[str]],
    label_keywords: List[str],
) -> str:
    if not block_rows:
        return ""

    label_norms = [_norm(x) for x in label_keywords]

    for row in block_rows:
        cleaned = [normalize_text(x) for x in row]
        normed = [_norm(x) for x in cleaned]

        for i, cell in enumerate(normed):
            if any(lb in cell for lb in label_norms):
                for cand in cleaned[i + 1:]:
                    val = _clean_section_value_text(cand, label_keywords)
                    if val:
                        return val

                raw = _clean_section_value_text(cleaned[i], label_keywords)
                if raw:
                    return raw

    block_text = " ".join(
        [" ".join([normalize_text(x) for x in row if normalize_text(x)]) for row in block_rows]
    )
    if "사모" in block_text and "공모" not in block_text:
        return "사모"
    if "공모" in block_text and "사모" not in block_text:
        return "공모"

    first_text = " ".join([normalize_text(x) for x in block_rows[0] if normalize_text(x)])
    first_text = _clean_section_value_text(first_text, label_keywords)
    if first_text:
        return first_text

    return ""

def _extract_int_from_block_rows(
    block_rows: List[List[str]],
    label_keywords: List[str],
    min_val: int = 1,
) -> Optional[int]:
    if not block_rows:
        return None

    label_norms = [_norm(x) for x in label_keywords]

    for row in block_rows:
        cleaned = [normalize_text(x) for x in row]
        normed = [_norm(x) for x in cleaned]

        for i, cell in enumerate(normed):
            if any(lb in cell for lb in label_norms):
                nums = []
                for cand in cleaned[i + 1:]:
                    v = _to_int(cand)
                    if v is not None and v >= min_val:
                        nums.append(v)
                if nums:
                    return max(nums)

                row_nums = []
                for cand in cleaned:
                    v = _to_int(cand)
                    if v is not None and v >= min_val:
                        row_nums.append(v)
                if row_nums:
                    return max(row_nums)

    for row in block_rows:
        row_text = " ".join([normalize_text(x) for x in row if normalize_text(x)])
        if any(lb in _norm(row_text) for lb in label_norms):
            vals = []
            for cell in row:
                v = _to_int(cell)
                if v is not None and v >= min_val:
                    vals.append(v)
            if vals:
                return max(vals)

    return None

def _extract_price_from_block_rows(
    block_rows: List[List[str]],
    label_keywords: List[str],
    min_val: int = 50,
    max_val: int = 100_000_000,
) -> Optional[int]:
    if not block_rows:
        return None

    label_norms = [_norm(x) for x in label_keywords]
    cleaned_rows = [[normalize_text(x) for x in row] for row in block_rows]

    def _valid_price(txt: Any) -> Optional[int]:
        s = normalize_text(txt)
        if not s:
            return None

        # 가격이 아닌 것들 차단
        if any(x in s for x in ["년", "월", "일", "%", "주식수", "비율", "권면총액", "총액"]):
            return None
        if s in ("정정전", "정정후", "변경전", "변경후", "-", "해당없음", "없음"):
            return None

        v = _to_int(s)
        if v is None:
            v = _max_int_in_text(s)

        if v is not None and min_val <= v <= max_val:
            return v
        return None

    # ------------------------------------------------------
    # 1) 9번 섹션 안에 정정표(정정전/정정후)가 있으면
    #    반드시 '정정후/변경후' 열을 우선 사용
    # ------------------------------------------------------
    for hr, row in enumerate(cleaned_rows):
        row_norm = [_norm(x) for x in row]

        has_before = any(("정정전" in x or "변경전" in x) for x in row_norm)
        has_after = any(("정정후" in x or "변경후" in x) for x in row_norm)

        if not (has_before and has_after):
            continue

        after_col = next(
            (i for i, x in enumerate(row_norm) if "정정후" in x or "변경후" in x),
            None,
        )

        if after_col is None:
            continue

        # 헤더 아래 행들 중 가격 라벨이 있는 행 찾기
        for rr in range(hr + 1, len(cleaned_rows)):
            curr = cleaned_rows[rr]
            curr_norm = [_norm(x) for x in curr]

            if not any(curr):
                continue

            # 다음 큰 제목 나오면 중단
            first_cell = curr[0] if curr else ""
            if rr > hr + 1 and re.match(r"^\d+\s*[\.\)]\s*[가-힣A-Za-z]", first_cell):
                break

            label_hit = any(
                any(lb in cell for lb in label_norms)
                for cell in curr_norm
            )
            if not label_hit:
                continue

            # 1-1. 정정후 열 값을 최우선
            if after_col < len(curr):
                v = _valid_price(curr[after_col])
                if v is not None:
                    return v

            # 1-2. 정정후 열이 비었으면, 라벨 오른쪽 유효 숫자들 중 마지막 값 사용
            vals = []
            for cand in curr:
                v = _valid_price(cand)
                if v is not None:
                    vals.append(v)
            if vals:
                return vals[-1]

    # ------------------------------------------------------
    # 2) 일반 표면(정정표가 아니면)
    #    라벨 오른쪽 숫자 중 '첫 값'이 아니라 '마지막 값' 사용
    # ------------------------------------------------------
    for row in cleaned_rows:
        row_norm = [_norm(x) for x in row]

        for i, cell in enumerate(row_norm):
            if any(lb in cell for lb in label_norms):
                vals = []
                for cand in row[i + 1:]:
                    v = _valid_price(cand)
                    if v is not None:
                        vals.append(v)

                if vals:
                    return vals[-1]

    return None

def _extract_percent_from_block_rows(
    block_rows: List[List[str]],
    label_keywords: List[str],
) -> str:
    if not block_rows:
        return ""

    label_norms = [_norm(x) for x in label_keywords]

    for row in block_rows:
        cleaned = [normalize_text(x) for x in row]
        normed = [_norm(x) for x in cleaned]

        for i, cell in enumerate(normed):
            if any(lb in cell for lb in label_norms):
                for cand in cleaned[i + 1:]:
                    if "%" in cand:
                        return clean_percent(cand)

                for cand in cleaned[i + 1:]:
                    v = _to_float(cand)
                    if v is not None:
                        return f"{v:g}%"

                for cand in cleaned:
                    if "%" in cand:
                        return clean_percent(cand)
                for cand in cleaned:
                    v = _to_float(cand)
                    if v is not None:
                        return f"{v:g}%"

    return ""

def extract_bond_method_from_section8(
    dfs: List[pd.DataFrame],
    corr_after: Dict[str, str],
) -> str:
    """
    모집방식은 반드시 '8. 사채발행방법'에서만 추출
    최종 반환값은 사모 / 공모 / 제3자배정만 허용
    """
    section_labels = ["사채발행방법", "모집방법", "모집방식", "발행방법"]

    # 1) 정정맵에서 "8번 섹션"만 인정
    if corr_after:
        for k, v in corr_after.items():
            if _is_numbered_section_heading(k, 8, section_labels):
                method = _normalize_bond_method_value(v)
                if method:
                    return method

    # 2) 실제 8번 섹션 블록만 확인
    block_rows = _get_section_block_rows(dfs, 8, section_labels, max_rows=6)
    if block_rows:
        block_text = " ".join(
            [
                " ".join([normalize_text(x) for x in row if normalize_text(x)])
                for row in block_rows
            ]
        )
        method = _normalize_bond_method_value(block_text)
        if method:
            return method

    return ""

def extract_bond_price_from_section9(
    dfs: List[pd.DataFrame],
    corr_after: Dict[str, str],
    bond_kind: str,
) -> str:
    """
    행사(전환)가액(원)은 실제 9번 섹션 블록에서만 추출
    - 다른 섹션 오염 방지용 strict mode
    - 9번 섹션 내부 정정표가 있으면 정정후 열 우선
    - 9번 섹션에 주석 footnote(주1) 정정후) 가 있으면 footnote까지 확인
    """

    if bond_kind == "CB":
        section_titles = ["전환에 관한 사항"]
        price_labels = ["전환가액(원/주)", "전환가액(원)", "전환가액"]
    elif bond_kind == "EB":
        section_titles = ["교환에 관한 사항"]
        price_labels = ["교환가액(원/주)", "교환가액(원)", "교환가액"]
    else:  # BW
        section_titles = ["권리행사에 관한 사항", "신주인수권에 관한 사항"]
        price_labels = [
            "권리행사가액(원/주)",
            "행사가액(원/주)",
            "권리행사가액(원)",
            "행사가액(원)",
            "권리행사가액",
            "행사가액",
        ]

    def _valid_prices_in_text(text: str) -> List[int]:
        txt = normalize_text(text)
        if not txt:
            return []

        txt = re.sub(r"20\d{2}[-년\./\s]+\d{1,2}[-월\./\s]+\d{1,2}", " ", txt)
        txt = re.sub(r"\b20\d{6}\b", " ", txt)
        txt = re.sub(r"\d+(?:\.\d+)?\s*%", " ", txt)

        vals = []
        for tok in re.findall(r"\d{1,3}(?:,\d{3})+|\d+", txt):
            try:
                v = int(tok.replace(",", ""))
            except Exception:
                continue
            if 50 <= v <= 100_000_000 and v not in [2024, 2025, 2026, 2027, 2028, 2029, 2030]:
                vals.append(v)
        return vals

    def _extract_price_from_text(text: str) -> Optional[int]:
        txt = normalize_text(text)
        if not txt:
            return None

        marker_hits = []
        for marker in ["정정후", "변경후"]:
            for m in re.finditer(marker, txt):
                sub = txt[m.end(): m.end() + 120]
                nums = _valid_prices_in_text(sub)
                if nums:
                    marker_hits.append(nums[0])
        if marker_hits:
            return marker_hits[-1]

        label_hits = []
        for lb in price_labels:
            for m in re.finditer(re.escape(lb), txt):
                sub = txt[m.end(): m.end() + 100]
                nums = _valid_prices_in_text(sub)
                if nums:
                    label_hits.append(nums[0])
        if label_hits:
            return label_hits[-1]

        nums = _valid_prices_in_text(txt)
        if nums:
            return nums[-1]

        return None

    def _extract_price_from_footnotes() -> Optional[int]:
        lines = [normalize_text(x) for x in all_text_lines(dfs) if normalize_text(x)]
        if not lines:
            return None

        target_norms = [_norm(x) for x in price_labels]

        for i, line in enumerate(lines):
            if not re.match(r"^주\s*\d+\)", line):
                continue

            block = [line]
            for j in range(i + 1, min(i + 6, len(lines))):
                nxt = lines[j]
                if re.match(r"^주\s*\d+\)", nxt):
                    break
                if re.match(r"^\d+\s*[\.\)]\s*[가-힣A-Za-z]", nxt):
                    break
                block.append(nxt)

            merged = " ".join(block)
            merged_norm = _norm(merged)

            if any(tn in merged_norm for tn in target_norms) or "정정후" in merged or "변경후" in merged:
                num = _extract_price_from_text(merged)
                if num is not None:
                    return num

        return None

    block_rows = _get_section_block_rows(dfs, 9, section_titles, max_rows=12)
    if not block_rows:
        return ""

    num = _extract_price_from_block_rows(
        block_rows,
        price_labels,
        min_val=50,
        max_val=100_000_000,
    )
    if num is not None:
        return f"{num:,}"

    block_text = " ".join(
        [
            " ".join([normalize_text(x) for x in row if normalize_text(x)])
            for row in block_rows
        ]
    )

    has_placeholder = any(
        re.search(r"주\s*\d+\)\s*정정(?:전|후)", normalize_text(cell))
        for row in block_rows
        for cell in row
    )

    has_correction_table = any(
        ("정정후" in _norm(normalize_text(cell)) or "변경후" in _norm(normalize_text(cell)))
        for row in block_rows
        for cell in row
    )

    num = _extract_price_from_text(block_text)
    if num is not None:
        return f"{num:,}"

    if has_placeholder or has_correction_table or corr_after:
        foot_num = _extract_price_from_footnotes()
        if foot_num is not None:
            return f"{foot_num:,}"

    return ""

def extract_bond_shares_and_ratio_from_section9(
    dfs: List[pd.DataFrame],
    corr_after: Dict[str, str],
    bond_kind: str,
) -> Tuple[str, str]:
    """
    전환주식수 / 주식총수대비 비율 추출

    우선순위
    1) 정정공시 corr_after 안의 복합값 분리 파싱
       예: '5,000,000 3.7'
    2) 실제 9번 섹션 블록
    3) 9번 섹션에 '주1) 정정후' 같은 placeholder가 있으면
       아래 footnote 블록까지 내려가서 재확인
    """
    if bond_kind == "CB":
        section_titles = ["전환에 관한 사항"]
        share_labels = [
            "전환에 따라 발행할 주식수",
            "전환에 따라 발행할 주식의 수",
            "전환주식수",
            "주식수",
        ]
    elif bond_kind == "EB":
        section_titles = ["교환에 관한 사항"]
        share_labels = [
            "교환대상 주식수",
            "교환대상주식수",
            "주식수",
        ]
    else:
        section_titles = ["권리행사에 관한 사항", "신주인수권에 관한 사항"]
        share_labels = [
            "행사주식수",
            "권리행사로 발행할 주식수",
            "주식수",
        ]

    ratio_labels = [
        "주식총수대비 비율(%)",
        "발행주식총수 대비 비율(%)",
        "주식총수 대비 비율",
        "총수대비 비율",
    ]

    share_val = ""
    ratio_val = ""

    if corr_after:
        for k, v in corr_after.items():
            k_norm = _norm(k)

            if any(_norm(lb) in k_norm for lb in share_labels + ratio_labels):
                merged = f"{normalize_text(k)} {normalize_text(v)}"
                s, r = _extract_share_ratio_pair_from_text(
                    merged,
                    share_labels,
                    ratio_labels,
                )

                if s and not share_val:
                    share_val = s
                if r and not ratio_val:
                    ratio_val = r

                if share_val and ratio_val:
                    return share_val, ratio_val

    block_rows = _get_section_block_rows(dfs, 9, section_titles, max_rows=12)

    if block_rows:
        if not share_val:
            num = _extract_int_from_block_rows(block_rows, share_labels, min_val=1)
            if num is not None and num > 0:
                share_val = f"{num:,}"

        if not ratio_val:
            ratio_val = _extract_percent_from_block_rows(block_rows, ratio_labels)

        if not share_val or not ratio_val:
            block_text = " ".join(
                [
                    " ".join([normalize_text(x) for x in row if normalize_text(x)])
                    for row in block_rows
                ]
            )
            s, r = _extract_share_ratio_pair_from_text(
                block_text,
                share_labels,
                ratio_labels,
            )

            if s and not share_val:
                share_val = s
            if r and not ratio_val:
                ratio_val = r

        has_placeholder = any(
            re.search(r"주\s*\d+\)\s*정정(?:전|후)", normalize_text(cell))
            for row in block_rows
            for cell in row
        )

        if has_placeholder and (not share_val or not ratio_val):
            s, r = _extract_share_ratio_from_footnotes(
                dfs,
                share_labels,
                ratio_labels,
            )
            if s and not share_val:
                share_val = s
            if r and not ratio_val:
                ratio_val = r

    if not share_val or not ratio_val:
        s, r = _extract_share_ratio_from_footnotes(
            dfs,
            share_labels,
            ratio_labels,
        )
        if s and not share_val:
            share_val = s
        if r and not ratio_val:
            ratio_val = r

    return share_val, ratio_val

def parse_bond_record(rec: Dict[str, Any]):
    title = clean_title(rec["title"])
    tables = rec["tables"]
    corr_after = extract_correction_after_map(tables) if is_correction_title(title) else {}

    row = {h: "" for h in BOND_HEADERS}
    missing = []
    suspicious = []

    row["구분"] = bond_type_code(title)
    row["회사명"] = first_nonempty(
        detect_company_from_tables(tables, corr_after),
        extract_company_name_from_title(title),
    )
    row["보고서명"] = title
    row["상장시장"] = first_nonempty(
        detect_market_from_title(title),
        detect_market_from_tables(tables, corr_after),
    )
    row["최초 이사회결의일"] = get_valid_date_by_labels(
        tables,
        ["최초 이사회결의일", "최초이사회결의일", "이사회결의일", "이사회결의일(결정일)", "결정일"],
        corr_after,
    )

    def get_corr_num(labels, fallback_keys=None, min_val=-1, as_float=False):
        fallback_keys = fallback_keys or []
        val = scan_label_value_preferring_correction(tables, labels, corr_after)

        if as_float:
            num = _to_float(val)
            if num is None and fallback_keys:
                num = find_row_best_float(tables, fallback_keys)
            return f"{num:g}" if num is not None else ""

        num = _to_int(val)
        if (num is None or num <= min_val) and fallback_keys:
            num = find_row_best_int(tables, fallback_keys, min_val)
        if num is not None:
            if num == 0:
                return "0"
            if num > 0:
                return f"{num:,}"
        return ""

    row["권면총액(원)"] = get_corr_num(
        ["사채의권면(전자등록)총액(원)", "권면(전자등록)총액(원)", "사채의 권면총액", "권면총액", "사채의 총액"],
        ["권면총액", "원"],
        50,
    )

    coupon = scan_label_value_preferring_correction(
        tables,
        ["표면이자율(%)", "표면이자율", "표면금리", "이표이자율"],
        corr_after,
    )
    ytm = scan_label_value_preferring_correction(
        tables,
        ["만기이자율(%)", "만기이자율", "만기보장수익률", "만기수익률", "Yield To Maturity"],
        corr_after,
    )

    row["Coupon"] = clean_percent(coupon) if coupon else get_corr_num(["표면이자율(%)", "표면이자율", "표면금리"], ["표면이자율"], -1, True)
    row["YTM"] = clean_percent(ytm) if ytm else get_corr_num(["만기이자율(%)", "만기이자율", "만기보장수익률"], ["만기이자율"], -1, True)

    row["만기"] = get_valid_date_by_labels(
        tables,
        ["사채만기일", "만기일", "상환기일", "만기"],
        corr_after,
    )
    row["납입일"] = extract_payment_date_bond(tables, corr_after)

    exact_method = extract_bond_method_from_section8(tables, corr_after)
    row["모집방식"] = exact_method
    
    if not row["모집방식"]:
        fallback_val = scan_label_value_preferring_correction(
            tables,
            ["사채발행방법", "모집방법", "모집방식", "발행방법", "공모여부"],
            corr_after,
        )
        row["모집방식"] = _normalize_bond_method_value(fallback_val)

    row["발행상품"] = extract_product_type_bond(tables, corr_after, title)

    if row["구분"] == "CB":
        price_labels = ["전환가액(원/주)", "전환가액(원)", "전환가액"]
    elif row["구분"] == "EB":
        price_labels = ["교환가액(원/주)", "교환가액(원)", "교환가액"]
    else:  # BW
        price_labels = [
            "권리행사가액(원/주)",
            "행사가액(원/주)",
            "권리행사가액(원)",
            "행사가액(원)",
            "권리행사가액",
            "행사가액",
        ]

    exact_price = extract_bond_price_from_section9(
        tables,
        corr_after,
        row["구분"],
    )

    if exact_price:
        row["행사(전환)가액(원)"] = exact_price
    else:
        # 정정공시는 전역 fallback 금지
        if not corr_after:
            fallback_price_raw = scan_label_value_preferring_correction(
                tables,
                price_labels,
                corr_after,
            )
            fallback_price_num = _to_int(fallback_price_raw)
            if fallback_price_num is None:
                fallback_price_num = _max_int_in_text(fallback_price_raw)

            row["행사(전환)가액(원)"] = (
                fmt_number(fallback_price_num) if fallback_price_num else ""
            )
        else:
            row["행사(전환)가액(원)"] = ""

    price_num = parse_float_like(row["행사(전환)가액(원)"])
    if price_num is not None and (price_num < 50 or price_num > 100_000_000):
        row["행사(전환)가액(원)"] = ""
        suspicious.append("행사(전환)가액(원)")
        
    exact_share_cnt, exact_share_ratio = extract_bond_shares_and_ratio_from_section9(
        tables,
        corr_after,
        row["구분"],
    )

    row["전환주식수"] = exact_share_cnt or get_corr_num(
        ["전환에 따라 발행할 주식수", "전환에 따라 발행할 주식의 수", "전환주식수", "교환대상 주식수", "교환대상주식수", "행사주식수", "권리행사로 발행할 주식수", "주식수"],
        ["주식수"],
        50,
    )
    row["주식총수대비 비율"] = exact_share_ratio or clean_percent(
        scan_label_value_preferring_correction(
            tables,
            ["주식총수 대비 비율(%)", "발행주식총수 대비 비율(%)", "주식총수 대비 비율", "총수대비 비율"],
            corr_after,
        )
    )

    refixing_raw = scan_label_value_preferring_correction(
        tables,
        ["최저 조정가액", "조정가액 하한", "Refixing Floor", "하한가액", "최저 조정가액(원)", "최저조정가액", "리픽싱 하한", "리픽싱하한"],
        corr_after,
    )
    if "%" in refixing_raw:
        row["Refixing Floor"] = clean_percent(refixing_raw)
    else:
        row["Refixing Floor"] = first_nonempty(
            fmt_number(_max_int_in_text(refixing_raw)) if _max_int_in_text(refixing_raw) else "",
            get_corr_num(["최저 조정가액", "조정가액 하한", "최저조정가액", "리픽싱하한"], ["최저조정가액", "원"], 50),
        )

    period_keywords = []
    if row["구분"] == "CB":
        period_keywords = ["전환청구기간"]
    elif row["구분"] == "EB":
        period_keywords = ["교환청구기간"]
    elif row["구분"] == "BW":
        period_keywords = ["권리행사기간"]

    s_date, e_date = extract_period_dates_from_tables(
        tables,
        corr_after,
        period_keywords,
    )
    row["전환청구 시작"], row["전환청구 종료"] = s_date, e_date

    row["투자자"] = extract_investors_bond(tables, corr_after)
    row["자금용도"] = extract_fund_usage_bond(tables, corr_after)
    row["링크"] = rec["src_url"]
    row["접수번호"] = rec["acpt_no"]

    for h in BOND_HEADERS:
        if h in ["링크", "접수번호"]:
            continue
        if not normalize_text(row[h]):
            missing.append(h)

    if not row["구분"]:
        suspicious.append("구분")
    if row["회사명"] in ["유", "코", "넥"]:
        suspicious.append("회사명")
    if row["보고서명"] and len(row["보고서명"]) < 5:
        suspicious.append("보고서명")

    return row, missing, suspicious

def run_parser():
    sh = gs_open()

    raw_ws = ensure_ws(sh, RAW_SHEET_NAME, rows=5000, cols=250)
    bond_ws = ensure_ws(sh, BOND_SHEET_NAME, rows=3000, cols=max(40, len(BOND_HEADERS) + 5))

    ensure_header(bond_ws, BOND_HEADERS)

    records = load_raw_records(raw_ws)
    if RUN_ONLY_ACPTNO:
        records = [r for r in records if r["acpt_no"] == RUN_ONLY_ACPTNO]

    if not records:
        print("[INFO] RAW_dump에 파싱할 데이터가 없습니다.")
        return

    ok = 0
    skip = 0
    fail = 0

    for rec in records:
        acpt_no = rec["acpt_no"]
        title = clean_title(rec["title"] or "")

        try:
            if any(
                k in title.replace(" ", "")
                for k in [
                    "전환사채권발행결정",
                    "교환사채권발행결정",
                    "신주인수권부사채권발행결정",
                ]
            ):
                row, missing, suspicious = parse_bond_record(rec)
                mode, rownum = upsert_structured_row(bond_ws, BOND_HEADERS, row, "bond")
                ok += 1
                print(f"[OK][BOND][{mode}] {acpt_no} {title}")
            else:
                skip += 1
                print(f"[SKIP] {acpt_no} {title}")

        except Exception as e:
            fail += 1
            print(f"[FAIL] {acpt_no} {title} :: {e}")

    print(f"[DONE][BOND] ok={ok} skip={skip} fail={fail}")


if __name__ == "__main__":
    run_parser()
