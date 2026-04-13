# 유상증자 + 무상증자 통합 파서
# - 저장 탭은 기존 K_유상증자 하나만 사용
# - 컬럼은 기존 유상증자 컬럼 구조 + 맨 앞 '구분'만 추가
# - 구분: 유 / 무
# - 유무상증자결정은 '유' row + '무' row 2줄로 저장
# - 보고서명은 원래 제목 그대로 유지해서 유무상 보고서 여부가 남도록 처리
# - 컬럼 추가 없이, 내부적으로만 (접수번호 + 구분) 기준 upsert

import re
import time
from typing import Dict, Any, List, Tuple, Optional

import pandas as pd

from parse_common import (
    RAW_SHEET_NAME,
    RIGHTS_SHEET_NAME,
    RUN_ONLY_ACPTNO,
    gs_open,
    ensure_ws,
    ensure_header,
    load_raw_records,
    clean_title,
    is_correction_title,
    extract_correction_after_map,
    detect_company_from_tables,
    extract_company_name_from_title,
    detect_market_from_title,
    detect_market_from_tables,
    get_valid_date_by_labels,
    scan_label_value,
    scan_label_value_preferring_correction,
    parse_float_like,
    _to_int,
    _to_float,
    _max_int_in_text,
    find_row_best_int,
    find_row_best_float,
    fmt_number,
    fmt_eok_from_won,
    first_nonempty,
    normalize_text,
    _norm,
    _clean_label,
)

# ==========================================================
# 저장 헤더
# - 기존 유상증자 컬럼 구조 유지
# - 맨 앞에 '구분'만 추가
# ==========================================================
RIGHTS_BONUS_HEADERS = [
    "구분",
    "회사명",
    "보고서명",
    "상장시장",
    "최초 이사회결의일",
    "증자방식",
    "발행상품",
    "신규발행주식수",
    "확정발행가(원)",
    "기준주가",
    "확정발행금액(억원)",
    "할인(할증률)",
    "증자전 주식수",
    "증자비율",
    "납입일",
    "신주의 배당기산일",
    "신주의 상장 예정일",
    "이사회결의일",
    "자금용도",
    "투자자",
    "링크",
    "접수번호",
]

RIGHTS_REQUIRED_HEADERS = [
    "구분",
    "회사명",
    "보고서명",
    "상장시장",
    "최초 이사회결의일",
    "증자방식",
    "발행상품",
    "신규발행주식수",
    "확정발행가(원)",
    "기준주가",
    "확정발행금액(억원)",
    "할인(할증률)",
    "증자전 주식수",
    "증자비율",
    "납입일",
    "신주의 배당기산일",
    "신주의 상장 예정일",
    "이사회결의일",
    "자금용도",
    "투자자",
    "링크",
    "접수번호",
]

# 무상증자는 기존 유상증자 컬럼 틀에 맞춰서 일부 컬럼만 채움
BONUS_REQUIRED_HEADERS = [
    "구분",
    "회사명",
    "보고서명",
    "상장시장",
    "최초 이사회결의일",
    "발행상품",
    "신규발행주식수",
    "증자전 주식수",
    "증자비율",
    "신주의 배당기산일",
    "신주의 상장 예정일",
    "이사회결의일",
    "링크",
    "접수번호",
]

KIND_IDX = RIGHTS_BONUS_HEADERS.index("구분")
RCEPT_IDX = RIGHTS_BONUS_HEADERS.index("접수번호")


def _blank_row() -> Dict[str, str]:
    return {h: "" for h in RIGHTS_BONUS_HEADERS}


# ==========================================================
# 로컬 upsert helper
# - 컬럼은 그대로 두고, (접수번호 + 구분) 기준으로 row 식별
# ==========================================================
def _col_to_a1(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _pad_row_values(values: List[Any]) -> List[str]:
    vals = [str(v).strip() for v in values]
    if len(vals) < len(RIGHTS_BONUS_HEADERS):
        vals += [""] * (len(RIGHTS_BONUS_HEADERS) - len(vals))
    return vals[: len(RIGHTS_BONUS_HEADERS)]


def _row_dict_to_values(row: Dict[str, Any]) -> List[str]:
    return [str(row.get(h, "")).strip() for h in RIGHTS_BONUS_HEADERS]


def _sheet_key_from_values(values: List[str]) -> Tuple[str, str]:
    values = _pad_row_values(values)
    return values[RCEPT_IDX].strip(), values[KIND_IDX].strip()


def _build_row_maps(ws):
    all_values = ws.get_all_values()
    row_map: Dict[Tuple[str, str], int] = {}
    value_map: Dict[Tuple[str, str], List[str]] = {}

    for idx, row in enumerate(all_values):
        if idx == 0:
            continue

        row_idx = idx + 1
        padded = _pad_row_values(row)
        key = _sheet_key_from_values(padded)

        if key[0] and key[1]:
            row_map[key] = row_idx
            value_map[key] = padded

    return row_map, value_map


def _shift_row_map_after_insert(row_map: Dict[Tuple[str, str], int], inserted_at: int):
    for k in list(row_map.keys()):
        if row_map[k] >= inserted_at:
            row_map[k] += 1


def _shift_row_map_after_delete(row_map: Dict[Tuple[str, str], int], deleted_at: int):
    for k in list(row_map.keys()):
        if row_map[k] > deleted_at:
            row_map[k] -= 1


def _delete_legacy_merged_row(ws, row_map, value_map, acpt_no: str):
    legacy_key = (str(acpt_no).strip(), "유무")
    legacy_row_idx = row_map.get(legacy_key)

    if not legacy_row_idx:
        return False

    ws.delete_rows(legacy_row_idx)
    time.sleep(0.3)

    row_map.pop(legacy_key, None)
    value_map.pop(legacy_key, None)
    _shift_row_map_after_delete(row_map, legacy_row_idx)
    return True


def _upsert_rights_bonus_row(ws, row_map, value_map, row: Dict[str, Any]):
    values = _row_dict_to_values(row)
    key = _sheet_key_from_values(values)

    if not key[0] or not key[1]:
        return "skip", None

    existing_row_idx = row_map.get(key)
    end_col = _col_to_a1(len(RIGHTS_BONUS_HEADERS))

    if existing_row_idx:
        old_values = value_map.get(key, [""] * len(RIGHTS_BONUS_HEADERS))
        if old_values != values:
            ws.update(
                range_name=f"A{existing_row_idx}:{end_col}{existing_row_idx}",
                values=[values],
            )
            time.sleep(0.3)
            value_map[key] = values
            return "update", existing_row_idx
        return "skip", existing_row_idx

    insert_at = 2
    ws.insert_rows([values], row=insert_at, value_input_option="USER_ENTERED")
    time.sleep(0.3)

    _shift_row_map_after_insert(row_map, insert_at)
    row_map[key] = insert_at
    value_map[key] = values
    return "insert", insert_at


# ==========================================================
# 공통 숫자/주식수 헬퍼
# ==========================================================
def parse_shares_from_text(text: str) -> Tuple[int, int, int]:
    text_norm = _norm(text)
    text_norm = re.sub(r"202\d[년월일\.]?", "", text_norm)
    text_norm = re.sub(r"\d+(?:\.\d+)?%", "", text_norm)

    boundaries = r"보통|기타|종류|우선|상환|합계|총계|총수|계|액면|자금|목적|발행가|할인"

    pattern_com = r"보통(?:(?!" + boundaries + r")[^0-9])*?((?:\d{1,3}(?:[,.]\d{3})+|\d+))"
    m_com = re.findall(pattern_com, text_norm)
    cvs = [int(re.sub(r"[,.]", "", x)) for x in m_com if int(re.sub(r"[,.]", "", x)) >= 50]
    cv = cvs[-1] if cvs else 0

    pattern_oth = r"(?:기타|종류|우선|상환)(?:(?!" + boundaries + r")[^0-9])*?((?:\d{1,3}(?:[,.]\d{3})+|\d+))"
    m_oth = re.findall(pattern_oth, text_norm)
    ovs = [int(re.sub(r"[,.]", "", x)) for x in m_oth if int(re.sub(r"[,.]", "", x)) >= 50]
    ov = ovs[-1] if ovs else 0

    pattern_tot = r"(?:합계|총계|총수|계)(?:(?!" + boundaries + r")[^0-9])*?((?:\d{1,3}(?:[,.]\d{3})+|\d+))"
    m_tot = re.findall(pattern_tot, text_norm)
    tvs = [int(re.sub(r"[,.]", "", x)) for x in m_tot if int(re.sub(r"[,.]", "", x)) >= 50]
    tv = tvs[-1] if tvs else 0

    if cv == 0 and ov == 0 and tv == 0:
        text_clean = text_norm
        for kw in [
            "신주의종류와수",
            "발행예정주식",
            "발행예정주식수",
            "신주발행",
            "발행할주식",
            "증자전발행주식총수",
            "기발행주식총수",
            "발행주식총수",
            "증자전주식수",
            "증자전",
        ]:
            text_clean = text_clean.replace(kw, "")

        text_clean = re.sub(r"^([①-⑩]|\(\d+\)|\d+\.)+", "", text_clean)
        nums_str = re.findall(r"\d{1,3}(?:[,.]\d{3})+(?!\d)|\d+", text_clean)
        v_nums = [int(re.sub(r"[,.]", "", x)) for x in nums_str if int(re.sub(r"[,.]", "", x)) >= 50]
        if v_nums:
            cv = v_nums[-1]

    return cv, ov, tv


def extract_issue_shares_and_type(
    dfs: List[pd.DataFrame],
    corr_after: Dict[str, str],
) -> Tuple[Optional[int], str]:
    target_kws = ["신주의종류와수", "발행예정주식수", "발행예정주식", "신주발행", "발행할주식"]
    stop_kws = ["증자전", "기발행", "총수", "발행가", "액면가", "자금조달", "증자방식", "일정", "목적"]

    stock_type = "보통주식"
    best_amt = 0

    if corr_after:
        for k, v in corr_after.items():
            k_norm = _norm(k)
            if any(t in k_norm for t in target_kws):
                if not any(s in k_norm for s in stop_kws):
                    cv, ov, tv = parse_shares_from_text(str(v))
                    calc = cv + ov
                    if tv > 0 and tv >= calc:
                        best_amt = tv
                    elif calc > 0:
                        best_amt = calc
                    elif cv > 0:
                        best_amt = cv
                    elif ov > 0:
                        best_amt = ov

                    if best_amt > 0:
                        v_norm = _norm(v)
                        if ov > 0 and cv == 0:
                            stock_type = "우선주식"
                        elif cv > 0 and ov == 0:
                            stock_type = "보통주식"
                        elif cv > 0 and ov > 0:
                            stock_type = "보통주식, 우선주식"
                        elif "우선" in v_norm or "종류" in v_norm or "기타" in v_norm:
                            stock_type = "보통주식, 우선주식" if "보통" in v_norm else "우선주식"
                        return best_amt, stock_type

    for df in dfs:
        try:
            arr = df.astype(str).values
        except Exception:
            continue

        R, C = arr.shape
        for r in range(R):
            row_str_norm = _norm("".join(arr[r]))
            combined_target = row_str_norm
            if r + 1 < R:
                combined_target += _norm("".join(arr[r + 1]))

            if any(t in combined_target for t in target_kws):
                if any(s in row_str_norm for s in stop_kws) and not any(t in row_str_norm for t in target_kws):
                    continue

                block_text = ""
                search_start = max(0, r - 1)

                for rr in range(search_start, min(r + 6, R)):
                    curr_row_norm = _norm("".join(arr[rr]))

                    if rr < r and any(s in curr_row_norm for s in stop_kws + ["액면", "자금", "방식"]):
                        continue

                    if rr > r + 1:
                        clean_next = _clean_label(curr_row_norm)
                        if len(curr_row_norm) != len(clean_next):
                            if any(k in curr_row_norm for k in ["액면", "자금", "가액", "증자", "목적", "방식", "총수", "예정"]):
                                break

                    for c in range(C):
                        cell_str = _norm(arr[rr][c])
                        if any(s in cell_str for s in stop_kws) and not any(t in cell_str for t in target_kws):
                            continue
                        block_text += " " + cell_str

                cv, ov, tv = parse_shares_from_text(block_text)
                calc_tot = cv + ov

                if tv > 0 and tv >= calc_tot:
                    best_amt = tv
                elif calc_tot > 0:
                    best_amt = calc_tot
                elif cv > 0:
                    best_amt = cv
                elif ov > 0:
                    best_amt = ov

                if best_amt > 0:
                    if ov > 0 and cv == 0:
                        stock_type = "우선주식"
                    elif cv > 0 and ov == 0:
                        stock_type = "보통주식"
                    elif cv > 0 and ov > 0:
                        stock_type = "보통주식, 우선주식"
                    elif "우선" in block_text or "종류" in block_text or "기타" in block_text:
                        stock_type = "보통주식, 우선주식" if "보통" in block_text else "우선주식"
                    return best_amt, stock_type

    val = scan_label_value(dfs, ["신주의 종류와 수", "발행예정주식", "발행예정주식수"])
    amt = _max_int_in_text(val)
    if amt and amt > 100:
        stock_type = "우선주식" if any(x in _norm(val) for x in ["우선", "기타", "종류"]) else "보통주식"
        return amt, stock_type

    return None, "보통주식"


def extract_issue_shares_and_type_section1_exact(
    dfs: List[pd.DataFrame],
    corr_after: Dict[str, str],
) -> Tuple[Optional[int], str]:
    """
    '1. 신주의 종류와 수' 섹션만 정확히 읽는 보정 로직
    """

    def _first_nonempty_cell(row_vals) -> str:
        for x in row_vals:
            s = normalize_text(x)
            if s:
                return s
        return ""

    def _is_section1_heading(text: str) -> bool:
        raw = normalize_text(text)
        n = _norm(raw)
        if not raw:
            return False

        patterns = [
            r"^1[\.\)]?신주의종류와수$",
            r"^1[\.\)]?신주의종류와수\(주\)$",
            r"^1[\.\)]?신주의\s*종류와\s*수$",
        ]
        if any(re.match(p, n) for p in patterns):
            return True
        if "1신주의종류와수" in n:
            return True
        return False

    def _is_new_top_heading(text: str) -> bool:
        raw = normalize_text(text)
        if not raw:
            return False
        return bool(re.match(r"^\d+\s*[\.\)]\s*[가-힣A-Za-z]", raw))

    def _is_bad_share_candidate(s: str) -> bool:
        t = normalize_text(s)
        n = _norm(t)

        if not t:
            return True
        if t in ("-", "—", "해당없음", "없음"):
            return True
        if re.match(r"^\d+\s*[\.\)]\s*", t):
            return True
        if "신주의종류와수" in n:
            return True
        if re.search(r"\d{4}[-./년]\s*\d{1,2}", t):
            return True

        return False

    def _extract_num_from_row_by_label(row_vals: List[str], label_kws: List[str]) -> Optional[int]:
        cleaned = [normalize_text(x) for x in row_vals]
        normed = [_norm(x) for x in cleaned]
        label_kws_norm = [_norm(x) for x in label_kws]

        for i, cell in enumerate(normed):
            if any(kw in cell for kw in label_kws_norm):
                for cand in cleaned[i + 1:]:
                    if _is_bad_share_candidate(cand):
                        continue
                    v = _to_int(cand)
                    if v is not None and v >= 50:
                        return v
        return None

    if corr_after:
        for k, v in corr_after.items():
            if _is_section1_heading(k):
                txt = normalize_text(v)
                common = None
                other = None
                total = None

                m = re.search(r"보통주식\s*\(\s*주\s*\)\s*[:：]?\s*([0-9][0-9,]*)", txt)
                if m:
                    common = int(m.group(1).replace(",", ""))

                m = re.search(r"(?:기타주식|종류주식|우선주식)\s*\(\s*주\s*\)\s*[:：]?\s*([0-9][0-9,]*)", txt)
                if m:
                    other = int(m.group(1).replace(",", ""))

                m = re.search(r"(?:합계|총계|계)\s*[:：]?\s*([0-9][0-9,]*)", txt)
                if m:
                    total = int(m.group(1).replace(",", ""))

                amt = total if total else (common or 0) + (other or 0)
                if amt > 0:
                    if other and not common:
                        return amt, "우선주식"
                    if common and not other:
                        return amt, "보통주식"
                    if common and other:
                        return amt, "보통주식, 우선주식"
                    return amt, "보통주식"

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

            if not (_is_section1_heading(first_cell) or _is_section1_heading(row_join)):
                continue

            common = None
            other = None
            total = None
            joined_txt = []

            for rr in range(r, min(r + 8, R)):
                next_row = [normalize_text(x) for x in arr[rr].tolist()]
                next_first = _first_nonempty_cell(next_row)

                if rr > r and _is_new_top_heading(next_first):
                    break

                row_text = " ".join([x for x in next_row if x])
                if row_text:
                    joined_txt.append(row_text)

                if common is None:
                    common = _extract_num_from_row_by_label(next_row, ["보통주식", "보통주"])

                if other is None:
                    other = _extract_num_from_row_by_label(
                        next_row,
                        ["기타주식", "종류주식", "우선주식", "기타주", "종류주", "우선주"],
                    )

                if total is None:
                    total = _extract_num_from_row_by_label(next_row, ["합계", "총계", "계"])

            amt = total if total else (common or 0) + (other or 0)
            if amt > 0:
                if other and not common:
                    return amt, "우선주식"
                if common and not other:
                    return amt, "보통주식"
                if common and other:
                    return amt, "보통주식, 우선주식"

                joined_norm = _norm(" ".join(joined_txt))
                if "우선" in joined_norm or "종류" in joined_norm or "기타" in joined_norm:
                    return amt, "보통주식, 우선주식" if "보통" in joined_norm else "우선주식"
                return amt, "보통주식"

    return None, ""


def choose_issue_shares_and_type(
    dfs: List[pd.DataFrame],
    corr_after: Dict[str, str],
) -> Tuple[Optional[int], str]:
    old_amt, old_type = extract_issue_shares_and_type(dfs, corr_after)
    new_amt, new_type = extract_issue_shares_and_type_section1_exact(dfs, corr_after)

    if old_amt and new_amt:
        if new_amt < 50 <= old_amt:
            return old_amt, (old_type or new_type or "보통주식")
        if old_amt >= 100 and new_amt <= 5:
            return old_amt, (old_type or new_type or "보통주식")

    if old_amt and not new_amt:
        return old_amt, old_type

    if new_amt and not old_amt:
        return new_amt, (new_type or old_type or "보통주식")

    if not old_amt and not new_amt:
        return None, (old_type or new_type or "보통주식")

    if old_amt == new_amt:
        return old_amt, (old_type or new_type or "보통주식")

    return new_amt, (new_type or old_type or "보통주식")


def get_prev_shares_sum(dfs: List[pd.DataFrame], corr_after: Dict[str, str]) -> Optional[int]:
    """
    증자전 주식수는 반드시
    '3. 증자전 발행주식총수 (주)' 섹션에서만 읽는다.
    """

    def _extract_section_share_total(text: str) -> Optional[int]:
        if not text:
            return None

        cv, ov, tv = parse_shares_from_text(str(text))
        calc = cv + ov

        if tv > 0 and tv >= calc:
            return tv
        if calc > 0:
            return calc
        if cv > 0:
            return cv
        if ov > 0:
            return ov
        return None

    def _first_nonempty_cell(row_vals) -> str:
        for x in row_vals:
            s = normalize_text(x)
            if s:
                return s
        return ""

    def _is_section3_heading(text: str) -> bool:
        raw = normalize_text(text)
        n = _norm(raw)
        if not raw:
            return False

        patterns = [
            r"^3[\.\)]?증자전발행주식총수\(주\)$",
            r"^3[\.\)]?증자전발행주식총수$",
            r"^3[\.\)]?증자전\s*발행주식총수$",
            r"^3[\.\)]?증자전\s*발행주식총수\(주\)$",
        ]
        if any(re.match(p, n) for p in patterns):
            return True

        if "3증자전발행주식총수" in n:
            return True

        return False

    def _is_new_top_heading(text: str) -> bool:
        raw = normalize_text(text)
        if not raw:
            return False
        return bool(re.match(r"^\d+\s*[\.\)]\s*[가-힣A-Za-z]", raw))

    if corr_after:
        for k, v in corr_after.items():
            k_raw = normalize_text(k)
            k_norm = _norm(k_raw)
            if _is_section3_heading(k_raw) or "3증자전발행주식총수" in k_norm:
                amt = _extract_section_share_total(v)
                if amt:
                    return amt

    for df in dfs:
        try:
            arr = df.astype(str).values
        except Exception:
            continue

        R, C = arr.shape
        for r in range(R):
            row_list = arr[r].tolist()
            first_cell = _first_nonempty_cell(row_list)
            row_join = " ".join([normalize_text(x) for x in row_list if normalize_text(x)])

            if _is_section3_heading(first_cell) or _is_section3_heading(row_join):
                block_texts = []
                for rr in range(r, min(r + 6, R)):
                    next_row_list = arr[rr].tolist()
                    next_first = _first_nonempty_cell(next_row_list)
                    next_join = " ".join([normalize_text(x) for x in next_row_list if normalize_text(x)])

                    if rr > r and _is_new_top_heading(next_first):
                        break

                    if next_join:
                        block_texts.append(next_join)

                amt = _extract_section_share_total(" ".join(block_texts))
                if amt:
                    return amt

    return None


def get_base_price_by_exact_section(
    dfs: List[pd.DataFrame],
    corr_after: Dict[str, str],
) -> Optional[int]:
    """
    기준주가는 반드시 '7. 기준주가' 섹션에서만 추출한다.
    """

    def _extract_valid_prices(text: str) -> List[int]:
        if not text:
            return []
        txt = str(text)
        txt = re.sub(r"202\d[년월일\.]?", "", txt)
        txt = re.sub(r"\d+(?:\.\d+)?%", "", txt)
        txt = re.sub(r"^([①-⑩]|\(\d+\)|\d+\.)+", "", txt)

        nums = re.findall(
            r"(?<![\d.])\d{1,3}(?:,\d{3})*(?:\.\d+)?(?![\d.])|(?<![\d.])\d+(?:\.\d+)?(?![\d.])",
            txt,
        )

        vals = []
        for x in nums:
            try:
                val = int(float(x.replace(",", "")))
                if val >= 50 and val not in [2024, 2025, 2026, 2027]:
                    vals.append(val)
            except Exception:
                pass
        return vals

    def _first_nonempty_cell(row_vals) -> str:
        for x in row_vals:
            s = normalize_text(x)
            if s:
                return s
        return ""

    def _is_section7_heading(text: str) -> bool:
        raw = normalize_text(text)
        n = _norm(raw)
        if not raw:
            return False

        patterns = [
            r"^7[\.\)]?기준주가$",
            r"^7[\.\)]?기준발행가액$",
        ]
        if any(re.match(p, n) for p in patterns):
            return True
        if "7기준주가" in n or "7기준발행가액" in n:
            return True
        return False

    def _is_new_top_heading(text: str) -> bool:
        raw = normalize_text(text)
        if not raw:
            return False
        return bool(re.match(r"^\d+\s*[\.\)]\s*[가-힣A-Za-z]", raw))

    if corr_after:
        for k, v in corr_after.items():
            k_raw = normalize_text(k)
            k_norm = _norm(k_raw)
            if _is_section7_heading(k_raw) or "7기준주가" in k_norm or "7기준발행가액" in k_norm:
                vals = _extract_valid_prices(v)
                if vals:
                    return max(vals)

    for df in dfs:
        try:
            arr = df.astype(str).values
        except Exception:
            continue

        R, C = arr.shape
        for r in range(R):
            row_list = arr[r].tolist()
            first_cell = _first_nonempty_cell(row_list)
            row_join = " ".join([normalize_text(x) for x in row_list if normalize_text(x)])

            if _is_section7_heading(first_cell) or _is_section7_heading(row_join):
                block_texts = []
                for rr in range(r, min(r + 6, R)):
                    next_row_list = arr[rr].tolist()
                    next_first = _first_nonempty_cell(next_row_list)
                    next_join = " ".join([normalize_text(x) for x in next_row_list if normalize_text(x)])

                    if rr > r and _is_new_top_heading(next_first):
                        break
                    block_texts.append(next_join)

                vals = _extract_valid_prices(" ".join(block_texts))
                if vals:
                    return max(vals)

    return None


def get_price_by_exact_section(
    dfs: List[pd.DataFrame],
    corr_after: Dict[str, str],
) -> Optional[int]:
    """
    확정발행가(원)은 반드시 '6. 신주 발행가액' 섹션에서만 가져온다.
    """

    def _first_nonempty_cell(row_vals) -> str:
        for x in row_vals:
            s = normalize_text(x)
            if s:
                return s
        return ""

    def _is_section6_heading(text: str) -> bool:
        raw = normalize_text(text)
        n = _norm(raw)
        if not raw:
            return False

        patterns = [
            r"^6[\.\)]?신주발행가액$",
            r"^6[\.\)]?신주의발행가액$",
            r"^6[\.\)]?1주당신주발행가액$",
            r"^6[\.\)]?발행가액$",
        ]
        if any(re.match(p, n) for p in patterns):
            return True

        if "6신주발행가액" in n or "6신주의발행가액" in n:
            return True

        return False

    def _is_new_top_heading(text: str) -> bool:
        raw = normalize_text(text)
        if not raw:
            return False
        return bool(re.match(r"^\d+\s*[\.\)]\s*[가-힣A-Za-z]", raw))

    def _extract_valid_prices(text: str) -> List[int]:
        if not text:
            return []

        txt = str(text)
        txt = re.sub(r"202\d[년월일\.]?", "", txt)
        txt = re.sub(r"\d+(?:\.\d+)?%", "", txt)
        txt = re.sub(r"^([①-⑩]|\(\d+\)|\d+\.)+", "", txt)

        nums = re.findall(
            r"(?<![\d.])\d{1,3}(?:,\d{3})*(?![\d.])|(?<![\d.])\d+(?![\d.])",
            txt,
        )

        vals = []
        for x in nums:
            try:
                v = int(x.replace(",", ""))
                if v >= 50 and v not in [2024, 2025, 2026, 2027]:
                    vals.append(v)
            except Exception:
                pass
        return vals

    def _extract_common_stock_price_from_text(text: str) -> Optional[int]:
        if not text:
            return None

        txt = normalize_text(text)

        patterns = [
            r"보통주식\s*\(\s*원\s*\)\s*[:：]?\s*([0-9][0-9,]*)",
            r"보통주식\s*[:：]?\s*([0-9][0-9,]*)",
            r"보통주\s*\(\s*원\s*\)\s*[:：]?\s*([0-9][0-9,]*)",
            r"보통주\s*[:：]?\s*([0-9][0-9,]*)",
        ]
        for pat in patterns:
            m = re.search(pat, txt)
            if m:
                try:
                    v = int(m.group(1).replace(",", ""))
                    if v >= 50:
                        return v
                except Exception:
                    pass

        return None

    def _extract_last_common_stock_price(text: str) -> Optional[int]:
        if not text:
            return None

        txt = normalize_text(text)
        patterns = [
            r"보통주식\s*\(\s*원\s*\)\s*[:：]?\s*([0-9][0-9,]*)",
            r"보통주식\s*[:：]?\s*([0-9][0-9,]*)",
            r"보통주\s*\(\s*원\s*\)\s*[:：]?\s*([0-9][0-9,]*)",
            r"보통주\s*[:：]?\s*([0-9][0-9,]*)",
        ]

        hits = []
        for pat in patterns:
            for m in re.finditer(pat, txt):
                try:
                    v = int(m.group(1).replace(",", ""))
                    if v >= 50:
                        hits.append((m.start(), v))
                except Exception:
                    pass

        if not hits:
            return None

        hits.sort(key=lambda x: x[0])
        return hits[-1][1]

    def _extract_price_after_markers(text: str, markers: List[str]) -> Optional[int]:
        if not text:
            return None

        txt = normalize_text(text)

        positions = []
        for marker in markers:
            for m in re.finditer(marker, txt, flags=re.IGNORECASE):
                positions.append(m.end())

        if not positions:
            return None

        positions.sort(reverse=True)

        for pos in positions:
            sub = txt[pos:]
            v = _extract_common_stock_price_from_text(sub)
            if v is not None:
                return v

            vals = _extract_valid_prices(sub)
            if vals:
                return vals[0]

        return None

    def _extract_price_preferring_after(text: str) -> Optional[int]:
        if not text:
            return None

        txt = normalize_text(text)

        v = _extract_price_after_markers(txt, [r"정정후", r"변경후"])
        if v is not None:
            return v

        v = _extract_price_after_markers(txt, [r"확정발행가액", r"확정발행가"])
        if v is not None:
            return v

        v = _extract_common_stock_price_from_text(txt)
        if v is not None:
            return v

        vals = _extract_valid_prices(txt)
        if vals:
            return vals[0]

        return None

    def _extract_price_from_block_rows(block_rows: List[List[str]]) -> Optional[int]:
        row_texts = [
            " ".join([normalize_text(x) for x in row if normalize_text(x)])
            for row in block_rows
        ]
        block_text = " ".join([x for x in row_texts if x])

        for row_text in row_texts:
            if not row_text:
                continue

            v = _extract_price_after_markers(
                row_text,
                [r"정정후", r"변경후", r"확정발행가액", r"확정발행가"],
            )
            if v is not None:
                return v

        v = _extract_price_after_markers(
            block_text,
            [r"정정후", r"변경후", r"확정발행가액", r"확정발행가"],
        )
        if v is not None:
            return v

        if ("예정발행가" in block_text or "예정발행가액" in block_text) and (
            "확정발행가" in block_text or "확정발행가액" in block_text
        ):
            v = _extract_last_common_stock_price(block_text)
            if v is not None:
                return v

        for row in block_rows:
            row_clean = [normalize_text(x) for x in row]

            for c in range(len(row_clean) - 1, -1, -1):
                cell_norm = _norm(row_clean[c])

                if "보통주식" in cell_norm or "보통주" in cell_norm:
                    for cand in row_clean[c + 1 : c + 5]:
                        v = _to_int(cand)
                        if v is not None and v >= 50:
                            return v

        v = _extract_price_preferring_after(block_text)
        if v is not None:
            return v

        return None

    if corr_after:
        for k, v in corr_after.items():
            k_raw = normalize_text(k)
            k_norm = _norm(k_raw)

            if _is_section6_heading(k_raw) or "6신주발행가액" in k_norm or "6신주의발행가액" in k_norm:
                merged_text = f"{k_raw} {normalize_text(v)}"
                price = _extract_price_preferring_after(merged_text)
                if price is not None:
                    return price

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

            if _is_section6_heading(first_cell) or _is_section6_heading(row_join):
                block_rows = []

                for rr in range(r, min(r + 6, R)):
                    next_row_list = arr[rr].tolist()
                    next_first = _first_nonempty_cell(next_row_list)

                    if rr > r and _is_new_top_heading(next_first):
                        break

                    block_rows.append(next_row_list)

                price = _extract_price_from_block_rows(block_rows)
                if price is not None:
                    return price

    return None


def extract_fund_use_and_amount(
    dfs: List[pd.DataFrame],
    corr_after: Dict[str, str],
) -> Tuple[str, Optional[int]]:
    keys_map = {
        "시설자금": "시설자금",
        "영업양수자금": "영업양수자금",
        "운영자금": "운영자금",
        "채무상환자금": "채무상환자금",
        "타법인증권취득자금": "타법인 증권 취득자금",
        "타법인증권": "타법인 증권 취득자금",
        "기타자금": "기타자금",
        "취득자금": "취득자금",
    }
    found_amts: Dict[str, int] = {}

    def _first_nonempty_cell(row_vals) -> str:
        for x in row_vals:
            s = normalize_text(x)
            if s:
                return s
        return ""

    def _is_section4_heading(text: str) -> bool:
        raw = normalize_text(text)
        n = _norm(raw)
        if not raw:
            return False
        return bool(re.match(r"^4[\.\)]?\s*자금조달의목적$", n)) or "4자금조달의목적" in n

    def _is_new_top_heading(text: str) -> bool:
        raw = normalize_text(text)
        if not raw:
            return False
        return bool(re.match(r"^\d+\s*[\.\)]\s*[가-힣A-Za-z]", raw))

    def _collect_valid_amts(row: List[str]) -> List[int]:
        vals = []
        for cell in row:
            amt = _max_int_in_text(cell)
            if amt is not None and amt >= 100:
                vals.append(amt)
        return vals

    def _scan_rows(rows: List[List[str]]):
        block_total = None

        for row in rows:
            cleaned = [normalize_text(x) for x in row]
            row_joined = _norm("".join(cleaned))

            for k, std_name in keys_map.items():
                if _norm(k) in row_joined:
                    valid_amts = _collect_valid_amts(cleaned)
                    if valid_amts:
                        found_amts[std_name] = valid_amts[-1]

            if any(x in row_joined for x in ["합계", "총계"]):
                valid_amts = _collect_valid_amts(cleaned)
                if valid_amts:
                    block_total = valid_amts[-1]

        return block_total

    if corr_after:
        for itemk, v in corr_after.items():
            itemk_norm = _norm(itemk)
            for k, std_name in keys_map.items():
                if _norm(k) in itemk_norm:
                    amt = _max_int_in_text(v)
                    if amt and amt >= 100:
                        found_amts[std_name] = amt

    direct_total = None

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

            if not (_is_section4_heading(first_cell) or _is_section4_heading(row_join)):
                continue

            block_rows = []
            for rr in range(r, min(r + 12, R)):
                next_row = [normalize_text(x) for x in arr[rr].tolist()]
                next_first = _first_nonempty_cell(next_row)

                if rr > r and _is_new_top_heading(next_first):
                    break

                block_rows.append(next_row)

            section_total = _scan_rows(block_rows)
            if section_total is not None and section_total >= 100:
                direct_total = section_total

    for df in dfs:
        try:
            arr = df.fillna("").astype(str).values
        except Exception:
            continue

        for r in range(arr.shape[0]):
            row = [normalize_text(x) for x in arr[r].tolist()]
            row_joined = _norm("".join(row))

            for k, std_name in keys_map.items():
                if _norm(k) in row_joined:
                    valid_amts = _collect_valid_amts(row)
                    if valid_amts:
                        found_amts[std_name] = valid_amts[-1]

    std_order = [
        "시설자금",
        "영업양수자금",
        "운영자금",
        "채무상환자금",
        "타법인 증권 취득자금",
        "취득자금",
        "기타자금",
    ]
    uses = [name for name in std_order if found_amts.get(name, 0) > 0]
    total_sum = sum(found_amts.get(name, 0) for name in uses)

    final_total = direct_total if direct_total not in (None, 0) else (total_sum if total_sum > 0 else None)
    return ", ".join(uses), final_total


def extract_investors_rights(dfs: List[pd.DataFrame], corr_after: Dict[str, str]) -> str:
    investors = []
    blacklist = [
        "관계", "지분", "%", "주식", "배정", "선정", "경위", "비고", "해당사항",
        "정정전", "정정후", "정정", "변경", "합계", "소계", "총계", "발행", "납입",
        "예정", "목적", "주1", "주2", "주)", "기타", "참고", "출자자수", "본점", "소재지",
        "(명)", "명",
    ]

    def is_valid_name(s: str) -> bool:
        sn = s.strip()
        if not sn or sn in ("-", ".", ",", "(", ")", "0", "1"):
            return False
        if len(sn) > 40:
            return False
        if re.fullmatch(r"[\d,\.\s]+", sn):
            return False
        sn_norm = _norm(sn)
        for bw in blacklist:
            if bw in sn_norm:
                return False
        return True

    for df in dfs:
        try:
            arr = df.astype(str).values
        except Exception:
            continue

        R, C = arr.shape
        target_col = -1
        start_row = -1

        for r in range(R):
            row_str = "".join([_norm(str(x)) for x in arr[r]])
            if any(kw in row_str for kw in ["제3자배정대상자", "배정대상자", "성명(법인명)", "출자자"]):
                for c in range(C):
                    cell_norm = _norm(str(arr[r][c]))
                    if any(kw in cell_norm for kw in ["성명", "법인명", "대상자", "출자자", "투자자"]) and "관계" not in cell_norm and "주식" not in cell_norm:
                        target_col = c
                        start_row = r
                        break
                if target_col != -1:
                    break

        if target_col != -1:
            for rr in range(start_row + 1, R):
                val = str(arr[rr][target_col]).strip()
                val_norm = _norm(val)
                if "합계" in val_norm or "소계" in val_norm or "기타투자" in val_norm or val_norm.startswith("주1)"):
                    break

                chunks = [x.strip() for x in val.split("\n")]
                for chunk in chunks:
                    if is_valid_name(chunk) and chunk not in investors:
                        investors.append(chunk)

            if investors:
                return ", ".join(investors)

    if corr_after:
        for k, v in corr_after.items():
            if any(_norm(kw) in _norm(k) for kw in ["제3자배정대상자", "배정대상자", "투자자", "성명(법인명)"]):
                chunks = re.split(r"[\n,;/]", v)
                valid_chunks = []
                for chunk in chunks:
                    chunk = chunk.strip()
                    if is_valid_name(chunk) and chunk not in valid_chunks:
                        valid_chunks.append(chunk)
                if valid_chunks:
                    return ", ".join(valid_chunks)

    val = scan_label_value_preferring_correction(
        dfs,
        ["제3자배정대상자", "배정대상자", "투자자", "성명(법인명)"],
        corr_after,
    )
    if val:
        chunks = re.split(r"[\n,;/]", val)
        valid_chunks = []
        for chunk in chunks:
            chunk = chunk.strip()
            if is_valid_name(chunk) and chunk not in valid_chunks:
                valid_chunks.append(chunk)
        if valid_chunks:
            return ", ".join(valid_chunks)

    return ""


# ==========================================================
# 유상증자 파서
# ==========================================================
def parse_rights_record(rec: Dict[str, Any]):
    title = clean_title(rec["title"])
    tables = rec["tables"]
    corr_after = extract_correction_after_map(tables) if is_correction_title(title) else {}

    row = _blank_row()
    missing = []
    suspicious = []

    row["구분"] = "유"
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
        ["최초 이사회결의일", "최초이사회결의일"],
        corr_after,
    )
    row["이사회결의일"] = get_valid_date_by_labels(
        tables,
        ["이사회결의일", "이사회 결의일", "이사회결의일(결정일)", "결정일"],
        corr_after,
    )
    if not row["최초 이사회결의일"]:
        row["최초 이사회결의일"] = row["이사회결의일"]

    row["납입일"] = get_valid_date_by_labels(
        tables,
        ["납입일", "납입기일", "청약기일 및 납입일", "신주의 납입기일", "신주납입기일"],
        corr_after,
    )
    row["신주의 배당기산일"] = get_valid_date_by_labels(
        tables,
        ["신주의 배당기산일", "배당기산일"],
        corr_after,
    )
    row["신주의 상장 예정일"] = get_valid_date_by_labels(
        tables,
        ["신주의 상장예정일", "신주의 상장 예정일", "상장예정일", "신주 상장예정일", "상장 예정일", "신주상장예정일"],
        corr_after,
    )

    row["증자방식"] = scan_label_value_preferring_correction(
        tables,
        ["증자방식", "배정방법", "배정방식", "발행방법"],
        corr_after,
    )

    issue_shares, issue_type = choose_issue_shares_and_type(tables, corr_after)
    if issue_shares:
        row["신규발행주식수"] = fmt_number(issue_shares)
    if issue_type:
        row["발행상품"] = issue_type

    prev_shares = get_prev_shares_sum(tables, corr_after)
    if not prev_shares:
        prev_shares = (
            _max_int_in_text(
                scan_label_value_preferring_correction(
                    tables,
                    [
                        "증자전발행주식총수",
                        "기발행주식총수",
                        "발행주식총수",
                        "증자전 주식수",
                        "증자전발행주식총수(보통주식)",
                    ],
                    corr_after,
                )
            )
            or find_row_best_int(tables, ["증자전발행주식총수", "보통주식"], 50)
            or find_row_best_int(tables, ["발행주식총수", "보통주식"], 50)
        )
    if prev_shares:
        row["증자전 주식수"] = fmt_number(prev_shares)

    price = get_price_by_exact_section(tables, corr_after)
    if not price:
        price = (
            _max_int_in_text(
                scan_label_value_preferring_correction(
                    tables,
                    ["신주 발행가액", "신주발행가액", "예정발행가액", "확정발행가액", "발행가액", "1주당 확정발행가액"],
                    corr_after,
                )
            )
            or find_row_best_int(tables, ["신주발행가액", "보통주식"], 50)
            or find_row_best_int(tables, ["예정발행가액"], 50)
            or find_row_best_int(tables, ["발행가액", "원"], 50)
        )
    if price and price > 50:
        row["확정발행가(원)"] = fmt_number(price)

    base_price = get_base_price_by_exact_section(tables, corr_after)
    if base_price and base_price > 50:
        row["기준주가"] = fmt_number(base_price)

    disc = _to_float(
        scan_label_value_preferring_correction(
            tables,
            ["할인율", "할증률", "할인율 또는 할증률", "할인(할증)율", "할인(할증률)", "발행가액 산정시 할인율"],
            corr_after,
        )
    )
    if disc is None:
        disc = find_row_best_float(tables, ["할인율또는할증율"]) or find_row_best_float(tables, ["할인율"])
    if disc is not None:
        row["할인(할증률)"] = f"{disc:g}%"

    use_text, use_total = extract_fund_use_and_amount(tables, corr_after)
    row["자금용도"] = use_text
    row["투자자"] = extract_investors_rights(tables, corr_after)
    row["링크"] = rec["src_url"]
    row["접수번호"] = rec["acpt_no"]

    if not row["발행상품"] and row["신규발행주식수"]:
        row["발행상품"] = "보통주식"

    new_shares = parse_float_like(row["신규발행주식수"])
    price_val = parse_float_like(row["확정발행가(원)"])
    pre_shares = parse_float_like(row["증자전 주식수"])

    amount_won = None
    if new_shares is not None and price_val is not None:
        amount_won = int(round(new_shares * price_val))

    if amount_won is None and use_total is not None:
        amount_won = int(use_total)

    if amount_won is not None:
        row["확정발행금액(억원)"] = fmt_eok_from_won(amount_won)

    if new_shares is not None and pre_shares not in (None, 0):
        row["증자비율"] = f"{(new_shares / pre_shares) * 100:.2f}%"

    for h in RIGHTS_REQUIRED_HEADERS:
        if h in ["링크", "접수번호"]:
            continue
        if not normalize_text(row[h]):
            missing.append(h)

    if row["회사명"] in ["유", "코", "넥"]:
        suspicious.append("회사명")
    if price_val is not None and price_val <= 50:
        suspicious.append("확정발행가(원)")
    if base_price is not None and base_price <= 50:
        suspicious.append("기준주가")
    if row["투자자"] and any(x in row["투자자"] for x in ["관계", "지분", "합계", "소계", "정정", "출자자수", "명"]):
        suspicious.append("투자자")
    if row["보고서명"] and len(row["보고서명"]) < 5:
        suspicious.append("보고서명")

    return row, missing, suspicious


# ==========================================================
# 무상증자 파서
# - 기존 유상증자 컬럼 틀에 맞춰 일부 값만 채움
# ==========================================================
def parse_bonus_record(rec: Dict[str, Any]):
    title = clean_title(rec["title"])
    tables = rec["tables"]
    corr_after = extract_correction_after_map(tables) if is_correction_title(title) else {}

    row = _blank_row()
    missing = []
    suspicious = []

    row["구분"] = "무"
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
        ["최초 이사회결의일", "최초이사회결의일"],
        corr_after,
    )
    row["이사회결의일"] = get_valid_date_by_labels(
        tables,
        ["이사회결의일", "이사회 결의일", "이사회결의일(결정일)", "결정일"],
        corr_after,
    )
    if not row["최초 이사회결의일"]:
        row["최초 이사회결의일"] = row["이사회결의일"]

    issue_shares, issue_type = choose_issue_shares_and_type(tables, corr_after)
    if issue_shares:
        row["신규발행주식수"] = fmt_number(issue_shares)
    if issue_type:
        row["발행상품"] = issue_type

    prev_shares = get_prev_shares_sum(tables, corr_after)
    if prev_shares:
        row["증자전 주식수"] = fmt_number(prev_shares)

    row["신주의 배당기산일"] = get_valid_date_by_labels(
        tables,
        ["신주의 배당기산일", "배당기산일"],
        corr_after,
    )
    row["신주의 상장 예정일"] = get_valid_date_by_labels(
        tables,
        ["신주의 상장예정일", "신주의 상장 예정일", "상장예정일", "신주 상장예정일", "상장 예정일", "신주상장예정일"],
        corr_after,
    )

    row["링크"] = rec["src_url"]
    row["접수번호"] = rec["acpt_no"]

    new_shares = parse_float_like(row["신규발행주식수"])
    pre_shares = parse_float_like(row["증자전 주식수"])
    if new_shares is not None and pre_shares not in (None, 0):
        row["증자비율"] = f"{(new_shares / pre_shares) * 100:.2f}%"

    for h in BONUS_REQUIRED_HEADERS:
        if h in ["링크", "접수번호"]:
            continue
        if not normalize_text(row[h]):
            missing.append(h)

    if row["회사명"] in ["유", "코", "넥"]:
        suspicious.append("회사명")

    return row, missing, suspicious


# ==========================================================
# 제목 분기
# ==========================================================
def is_rights_title(title: str) -> bool:
    t = (title or "").replace(" ", "")
    return ("유상증자결정" in t) or ("유무상증자결정" in t)


def is_bonus_title(title: str) -> bool:
    t = (title or "").replace(" ", "")
    return ("무상증자결정" in t) or ("유무상증자결정" in t)


# ==========================================================
# 통합 파서
# - 유상: [유 row]
# - 무상: [무 row]
# - 유무상: [유 row, 무 row]
# ==========================================================
def parse_rights_bonus_record(rec: Dict[str, Any]):
    title = clean_title(rec.get("title", ""))
    title_n = title.replace(" ", "")

    if "유무상증자결정" in title_n:
        rights_row, rights_missing, rights_suspicious = parse_rights_record(rec)
        bonus_row, bonus_missing, bonus_suspicious = parse_bonus_record(rec)

        return [
            (rights_row, rights_missing, rights_suspicious),
            (bonus_row, bonus_missing, bonus_suspicious),
        ]

    if "유상증자결정" in title_n:
        row, missing, suspicious = parse_rights_record(rec)
        return [(row, missing, suspicious)]

    if "무상증자결정" in title_n:
        row, missing, suspicious = parse_bonus_record(rec)
        return [(row, missing, suspicious)]

    row = _blank_row()
    row["구분"] = ""
    row["회사명"] = extract_company_name_from_title(title)
    row["보고서명"] = title
    row["링크"] = rec.get("src_url", "")
    row["접수번호"] = rec.get("acpt_no", "")
    return [(row, [], ["구분"])]


# ==========================================================
# Runner
# ==========================================================
def run_parser():
    sh = gs_open()

    raw_ws = ensure_ws(sh, RAW_SHEET_NAME, rows=5000, cols=250)
    rights_ws = ensure_ws(sh, RIGHTS_SHEET_NAME, rows=3000, cols=max(40, len(RIGHTS_BONUS_HEADERS) + 5))

    ensure_header(rights_ws, RIGHTS_BONUS_HEADERS)

    records = load_raw_records(raw_ws)
    if RUN_ONLY_ACPTNO:
        records = [r for r in records if r["acpt_no"] == RUN_ONLY_ACPTNO]

    if not records:
        print("[INFO] RAW_dump에 파싱할 데이터가 없습니다.")
        return

    row_map, value_map = _build_row_maps(rights_ws)

    ok = 0
    skip = 0
    fail = 0

    for rec in records:
        acpt_no = rec["acpt_no"]
        title = clean_title(rec["title"] or "")
        title_n = title.replace(" ", "")

        try:
            if any(x in title_n for x in ["유상증자결정", "무상증자결정", "유무상증자결정"]):
                if "유무상증자결정" in title_n:
                    deleted = _delete_legacy_merged_row(rights_ws, row_map, value_map, acpt_no)
                    if deleted:
                        print(f"[CLEANUP] legacy 유무 row 삭제 완료 :: {acpt_no} {title}")

                parsed_rows = parse_rights_bonus_record(rec)

                # row=2에 개별 insert 되므로 reversed로 처리해야 최종 시트에서 유가 위, 무가 아래로 유지됨
                rec_results = []
                for row, missing, suspicious in reversed(parsed_rows):
                    mode, rownum = _upsert_rights_bonus_row(
                        rights_ws,
                        row_map,
                        value_map,
                        row,
                    )
                    rec_results.append(f"{row.get('구분', '')}:{mode}")
                    if mode in ("insert", "update", "skip"):
                        ok += 1

                print(f"[OK][RIGHTS_BONUS] {acpt_no} {title} :: {', '.join(reversed(rec_results))}")
            else:
                skip += 1
                print(f"[SKIP] {acpt_no} {title}")

        except Exception as e:
            fail += 1
            print(f"[FAIL] {acpt_no} {title} :: {e}")

    print(f"[DONE][RIGHTS_BONUS] ok={ok} skip={skip} fail={fail}")


if __name__ == "__main__":
    run_parser()
