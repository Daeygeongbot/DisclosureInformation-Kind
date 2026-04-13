# Put&Call옵션에 RAW데이터를 주식연계시트에 있는 컬럼에 넣는 코드
# 채권 Put / Call 옵션 전용 파서
import os
import re
import time
import random
from typing import Dict, List, Tuple, Any, Optional

import pandas as pd
from gspread.exceptions import APIError
from gspread.utils import rowcol_to_a1

from parse_common import (
    RAW_SHEET_NAME, BOND_SHEET_NAME, RUN_ONLY_ACPTNO,
    normalize_text, all_text_lines, clean_title, is_correction_title,
    extract_correction_after_map, clean_percent, parse_float_like,
    scan_label_value_preferring_correction,
    gs_open, ensure_ws, load_raw_records,
)


# ==========================================================
# [기본 정리]
# ==========================================================
def _clean_line(text: Any) -> str:
    if text is None:
        return ""
    s = str(text).replace("\xa0", " ")
    s = re.sub(r"\s*\|\s*", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _n(s: Any) -> str:
    return re.sub(r"\s+", "", str(s or "")).replace(":", "")


def _lines_from_tables(tables: List[pd.DataFrame]) -> List[str]:
    out = []
    for line in all_text_lines(tables):
        s = _clean_line(normalize_text(line))
        if s:
            out.append(s)
    return out


def _corpus_from_lines(lines: List[str]) -> str:
    return "\n".join([x for x in lines if x]).strip()


def _safe_percent(value: Any) -> str:
    if value is None:
        return ""

    s = str(value).strip()
    if not s:
        return ""

    s2 = clean_percent(s)
    if s2:
        return s2

    f = parse_float_like(s)
    if f is None:
        return ""

    if float(f).is_integer():
        return f"{int(f)}%"
    return f"{f}%"


def _is_top_heading(text: str) -> bool:
    s = normalize_text(text)
    if not s:
        return False
    return bool(re.match(r"^\d+\s*[\.\)]\s*[가-힣A-Za-z]", s))


# ==========================================================
# [9.1 섹션 추출]
# - 일반 보고서: 첫 번째 실질 9.1 사용
# - 정정 보고서: 마지막 실질 9.1 사용
# ==========================================================
def _is_91_heading(line: str) -> bool:
    s = _clean_line(line)
    if not s:
        return False

    patterns = [
        r"^9\s*[\.\-]?\s*1\s*[\)\.]?\s*옵션에\s*관한\s*사항",
        r"^9\s*[\.\-]?\s*1\s*[\)\.]?\s*옵션사항",
    ]
    return any(re.search(p, s, flags=re.IGNORECASE) for p in patterns)


def _is_next_major_heading(line: str) -> bool:
    s = _clean_line(line)
    if not s:
        return False

    stop_patterns = [
        r"^9\s*[\.\-]?\s*2\s*[\)\.]?",
        r"^9\s*[\.\-]?\s*3\s*[\)\.]?",
        r"^10\s*[\)\.]?",
        r"^11\s*[\)\.]?",
        r"^12\s*[\)\.]?",
        r"^13\s*[\)\.]?",
        r"^20\s*[\)\.]?",
        r"^21\s*[\)\.]?",
        r"^22\s*[\)\.]?",
        r"^23\s*[\)\.]?",
        r"^24\s*[\)\.]?",
        r"^25\s*[\)\.]?",
    ]
    return any(re.search(p, s, flags=re.IGNORECASE) for p in stop_patterns)


def _strip_91_heading_prefix(text: str) -> str:
    s = _clean_line(text)
    if not s:
        return ""

    patterns = [
        r"^\s*9\s*[\.\-]?\s*1\s*[\)\.]?\s*옵션에\s*관한\s*사항\s*[:：]?\s*",
        r"^\s*9\s*[\.\-]?\s*1\s*[\)\.]?\s*옵션사항\s*[:：]?\s*",
    ]

    for pat in patterns:
        new_s = re.sub(pat, "", s, flags=re.IGNORECASE).strip()
        if new_s != s:
            return new_s

    return s


def _extract_91_section_from_start_idx(lines: List[str], start_idx: int) -> str:
    if start_idx < 0 or start_idx >= len(lines):
        return ""

    bucket = []

    first_body = _strip_91_heading_prefix(lines[start_idx])
    if first_body:
        bucket.append(first_body)

    for idx in range(start_idx + 1, len(lines)):
        s = _clean_line(lines[idx])
        if not s:
            continue

        if _is_next_major_heading(s):
            break

        bucket.append(s)

    text = " ".join(bucket).strip()
    text = re.sub(r"\s{2,}", " ", text)
    return text


def extract_all_91_sections_from_lines(lines: List[str]) -> List[str]:
    if not lines:
        return []

    sections = []
    for idx, line in enumerate(lines):
        s = _clean_line(line)
        if s and _is_91_heading(s):
            sec = _extract_91_section_from_start_idx(lines, idx)
            if sec:
                sections.append(sec)

    return sections


def extract_91_option_section_from_corpus(corpus: str, use_last_91: bool = False) -> str:
    if not corpus:
        return ""

    start_patterns = [
        r"(?:^|\n)\s*9\s*[\.\-]?\s*1\s*[\)\.]?\s*옵션에\s*관한\s*사항",
        r"(?:^|\n)\s*9\s*[\.\-]?\s*1\s*[\)\.]?\s*옵션사항",
    ]

    matches = []
    for pat in start_patterns:
        matches.extend(list(re.finditer(pat, corpus, flags=re.IGNORECASE | re.MULTILINE)))

    if not matches:
        return ""

    matches.sort(key=lambda m: m.start())
    start_match = matches[-1] if use_last_91 else matches[0]

    start_idx = start_match.end()
    sub = corpus[start_idx:]

    end_patterns = [
        r"(?:^|\n)\s*9\s*[\.\-]?\s*2\s*[\)\.]?",
        r"(?:^|\n)\s*9\s*[\.\-]?\s*3\s*[\)\.]?",
        r"(?:^|\n)\s*10\s*[\)\.]?",
        r"(?:^|\n)\s*11\s*[\)\.]?",
        r"(?:^|\n)\s*12\s*[\)\.]?",
        r"(?:^|\n)\s*13\s*[\)\.]?",
        r"(?:^|\n)\s*20\s*[\)\.]?",
        r"(?:^|\n)\s*21\s*[\)\.]?",
        r"(?:^|\n)\s*22\s*[\)\.]?",
        r"(?:^|\n)\s*23\s*[\)\.]?",
        r"(?:^|\n)\s*24\s*[\)\.]?",
        r"(?:^|\n)\s*25\s*[\)\.]?",
    ]

    cut = len(sub)
    for pat in end_patterns:
        m = re.search(pat, sub, flags=re.IGNORECASE | re.MULTILINE)
        if m and m.start() > 0:
            cut = min(cut, m.start())

    text = sub[:cut].strip()
    text = text.replace("\n", " ")
    text = re.sub(r"\s*\|\s*", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# ==========================================================
# [9.1 예외]
# 1) 9.1이 "-" 면 Put/Call 둘 다 "-"
# 2) 9.1 전체가 22./23. 참조형일 때만 공시 확인 바람
#    (내용 중간/끝에 23이 한번 나온다고 바로 공시 확인 바람 아님)
# ==========================================================
def _is_dash_91_section(text: str) -> bool:
    s = _clean_line(text)
    if not s:
        return False

    s = s.replace("－", "-").replace("–", "-").replace("—", "-")
    return bool(re.fullmatch(r"-+", s))


def _is_reference_only_22_or_23_section(text: str) -> bool:
    s = _clean_line(text)
    if not s:
        return False

    patterns = [
        r"^\s*(?:22|23)\s*[\.\)]\s*기타\s*투자판단에\s*참고할\s*사항(?:을)?\s*참고(?:하여)?\s*주시기\s*바랍니다\.?\s*$",
        r"^\s*(?:22|23)\s*[\.\)]\s*기타투자판단에참고할사항(?:을)?\s*참고(?:하여)?\s*주시기\s*바랍니다\.?\s*$",
        r"^\s*조기상환청구권\s*\(\s*Put\s*Option\s*\)\s*,?\s*매도청구권\s*\(\s*Call\s*Option\s*\)\s*에\s*관한\s*사항(?:은)?\s*,?\s*(?:22|23)\s*[\.\)]\s*기타\s*투자판단에\s*참고할\s*사항(?:을)?\s*참고(?:하여)?\s*주시기\s*바랍니다\.?\s*$",
        r"^\s*조기상환청구권\s*\(\s*Put\s*Option\s*\)\s*,?\s*매도청구권\s*\(\s*CALL\s*OPTION\s*\)\s*에\s*관한\s*사항(?:은)?\s*,?\s*(?:22|23)\s*[\.\)]\s*기타\s*투자판단에\s*참고할\s*사항(?:을)?\s*참고(?:하여)?\s*주시기\s*바랍니다\.?\s*$",
    ]
    return any(re.search(p, s, flags=re.IGNORECASE) for p in patterns)


def _looks_substantive_91_section(text: str) -> bool:
    s = _clean_line(text)
    if not s:
        return False

    if _is_dash_91_section(s):
        return False

    if _is_reference_only_22_or_23_section(s):
        return False

    substantive_patterns = [
        r"조기상환청구권\s*\(\s*Put\s*Option\s*\)",
        r"Put\s*Option",
        r"매도청구권\s*\(\s*Call\s*Option\s*\)",
        r"매도청구권\s*\(\s*CALL\s*OPTION\s*\)",
        r"중도상환청구권\s*\(\s*Call\s*Option\s*\)",
        r"중도상환청구권\s*\(\s*CALL\s*OPTION\s*\)",
        r"발행회사의\s*매도청구권",
        r"\[\s*Call Option에 관한 사항\s*\]",
        r"\[\s*매도청구권",
        r"가\.\s*조기상환청구권",
        r"나\.\s*(?:발행회사의\s*)?(?:매도청구권|중도상환청구권)",
    ]
    return any(re.search(p, s, flags=re.IGNORECASE) for p in substantive_patterns)


def _select_best_91_section(sections: List[str], is_corr: bool) -> str:
    if not sections:
        return ""

    if is_corr:
        # 정정 보고서는 아래쪽부터 올라오면서 실제 본문형 9.1을 우선 선택
        for sec in reversed(sections):
            if _looks_substantive_91_section(sec):
                return sec
        return sections[-1]

    # 일반 보고서는 위쪽부터 내려오면서 첫 실질 9.1 선택
    for sec in sections:
        if _looks_substantive_91_section(sec):
            return sec
    return sections[0]


# ==========================================================
# [Call Option 헤딩 / 종료 패턴]
# - Call은 Put Option 텍스트 안에서 잘라낸다
# - Call 헤딩은 삭제하지 않고 같이 가져간다
# ==========================================================
CALL_START_PATTERNS = [
    r"\[\s*Call Option에 관한 사항\s*\]",
    r"\[\s*call option에 관한 사항\s*\]",
    r"\[\s*매도청구권\s*\(\s*Call Option\s*\)\s*에\s*관한\s*사항\s*\]",
    r"\[\s*매도청구권\s*\(\s*CALL OPTION\s*\)\s*에\s*관한\s*사항\s*\]",
    r"\[\s*중도상환청구권\s*\(\s*Call Option\s*\)\s*에\s*관한\s*사항\s*\]",
    r"\[\s*중도상환청구권\s*\(\s*CALL OPTION\s*\)\s*에\s*관한\s*사항\s*\]",
    r"<\s*Call Option\s*>",
    r"발행회사의\s*매도청구권\s*\(\s*Call Option\s*\)\s*에\s*관한\s*사항",
    r"발행회사의\s*매도청구권\s*\(\s*CALL OPTION\s*\)\s*에\s*관한\s*사항",
    r"발행회사의\s*중도상환청구권\s*\(\s*Call Option\s*\)\s*에\s*관한\s*사항",
    r"발행회사의\s*중도상환청구권\s*\(\s*CALL OPTION\s*\)\s*에\s*관한\s*사항",
    r"매도청구권\s*\(\s*Call Option\s*\)\s*에\s*관한\s*사항",
    r"매도청구권\s*\(\s*CALL OPTION\s*\)\s*에\s*관한\s*사항",
    r"중도상환청구권\s*\(\s*Call Option\s*\)\s*에\s*관한\s*사항",
    r"중도상환청구권\s*\(\s*CALL OPTION\s*\)\s*에\s*관한\s*사항",
    r"매도청구권\s*\(\s*Call Option\s*\)",
    r"매도청구권\s*\(\s*CALL OPTION\s*\)",
    r"중도상환청구권\s*\(\s*Call Option\s*\)",
    r"중도상환청구권\s*\(\s*CALL OPTION\s*\)",
    r"콜옵션\s*\(\s*Call Option\s*\)",
    r"콜옵션\s*\(\s*CALL OPTION\s*\)",
    r"Call Option에 관한 사항",
]

CALL_END_PATTERNS = [
    r"이\s*외\s*Put Option",
    r"조기상환청구권\s*\(\s*Put Option\s*\)",
    r"사채권자의\s*조기상환청구권",
    r"(?:^|[\s\]])9\s*[-\.]?\s*2\s*[\)\.]?\s*[가-힣A-Za-z\(]",
    r"(?:^|[\s\]])9\s*[-\.]?\s*3\s*[\)\.]?\s*[가-힣A-Za-z\(]",
    r"(?:^|[\s\]])10\.\s*[가-힣A-Za-z\(]",
    r"(?:^|[\s\]])11\.\s*[가-힣A-Za-z\(]",
    r"(?:^|[\s\]])12\.\s*[가-힣A-Za-z\(]",
    r"(?:^|[\s\]])13\.\s*[가-힣A-Za-z\(]",
    r"(?:^|[\s\]])20\.\s*[가-힣A-Za-z\(]",
    r"(?:^|[\s\]])21\.\s*[가-힣A-Za-z\(]",
    r"(?:^|[\s\]])22\.\s*[가-힣A-Za-z\(]",
    r"(?:^|[\s\]])23\.\s*[가-힣A-Za-z\(]",
    r"(?:^|[\s\]])24\.\s*[가-힣A-Za-z\(]",
    r"(?:^|[\s\]])25\.\s*[가-힣A-Za-z\(]",
]

REFERENCE_TAIL_PATTERNS = [
    r'\s*(?:이\s*외|이외)\s*.*?기타 투자판단에 참고할 사항.*$',
    r'\s*세부내용은\s*.*?기타 투자판단에 참고할 사항.*$',
    r'\s*상세내용은\s*.*?기타 투자판단에 참고할 사항.*$',
    r'\s*".*?기타 투자판단에 참고할 사항".*$',
    r'\s*을\s*참고(?:하여)?\s*주시기\s*바랍니다\.?$',
]


def _find_earliest_match(text: str, patterns: List[str], start_pos: int = 0):
    best = None
    for pat in patterns:
        m = re.search(pat, text[start_pos:], flags=re.IGNORECASE)
        if m:
            abs_start = start_pos + m.start()
            abs_end = start_pos + m.end()
            if best is None or abs_start < best[0]:
                best = (abs_start, abs_end, pat)
    return best


def locate_call_option_span(text: str) -> Optional[Tuple[int, int]]:
    raw = _clean_line(text)
    if not raw:
        return None

    start_match = _find_earliest_match(raw, CALL_START_PATTERNS)
    if not start_match:
        return None

    start_idx = start_match[0]
    sub = raw[start_idx:]

    cut = len(sub)
    end_match = _find_earliest_match(sub[1:], CALL_END_PATTERNS)
    if end_match:
        cut = min(cut, end_match[0] + 1)

    end_idx = start_idx + cut
    if end_idx <= start_idx:
        return None

    return start_idx, end_idx


def _trim_reference_tail(text: str) -> str:
    s = _clean_line(text)
    for pat in REFERENCE_TAIL_PATTERNS:
        s = re.sub(pat, "", s, flags=re.IGNORECASE).strip()
    return s


def extract_call_option_text_from_section(section_text: str) -> str:
    if not section_text:
        return ""

    raw = _clean_line(section_text)
    span = locate_call_option_span(raw)
    if not span:
        return ""

    start_idx, end_idx = span
    result = raw[start_idx:end_idx].strip()
    result = _trim_reference_tail(result)
    result = re.sub(r"\s{2,}", " ", result)
    return result.strip()


def remove_call_option_text_from_section(section_text: str) -> str:
    if not section_text:
        return ""

    raw = _clean_line(section_text)
    span = locate_call_option_span(raw)
    if not span:
        return raw

    start_idx, end_idx = span
    kept = (raw[:start_idx] + " " + raw[end_idx:]).strip()

    kept = _trim_reference_tail(kept)
    kept = re.sub(r"\s{2,}", " ", kept)
    kept = re.sub(r"\s+([,\.\)])", r"\1", kept)
    kept = re.sub(r"(\(\s+)", "(", kept)
    return kept.strip()


# ==========================================================
# [표 grid에서 Call 비율 / YTC 읽기]
# ==========================================================
def _to_pct_text(cell: Any, min_v: float = None, max_v: float = None) -> str:
    s = normalize_text(cell)
    if not s:
        return ""

    if s in ["구분", "-", ".", "해당없음", "해당사항없음"]:
        return ""

    m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", s)
    if not m:
        raw = s.replace(",", "")
        m = re.fullmatch(r"(-?\d+(?:\.\d+)?)", raw)
        if not m:
            return ""

    try:
        val = float(m.group(1))
    except Exception:
        return ""

    if min_v is not None and val < min_v:
        return ""
    if max_v is not None and val > max_v:
        return ""

    if float(val).is_integer():
        return f"{int(val)}%"
    return f"{val}%"


def extract_call_ratio_ytc_from_table_grid(
    tables: List[pd.DataFrame],
) -> Tuple[str, str, List[Tuple[str, str]]]:
    call_header_kws = [
        "Call비율",
        "콜옵션비율",
        "행사비율",
        "매도청구권행사비율",
    ]
    ytc_header_kws = [
        "YTC",
        "조기상환수익률",
        "연복리수익률",
        "매도청구권보장수익률",
        "매도청구수익률",
    ]

    pairs: List[Tuple[str, str]] = []

    for df in tables:
        try:
            arr = df.fillna("").astype(str).values
        except Exception:
            continue

        R, C = arr.shape
        if R == 0 or C == 0:
            continue

        header_row = None
        call_col = None
        ytc_col = None

        for r in range(R):
            row_norm = [_n(x) for x in arr[r].tolist()]

            tmp_call = None
            tmp_ytc = None

            for c, cell in enumerate(row_norm):
                if tmp_call is None and any(k in cell for k in call_header_kws):
                    tmp_call = c
                if tmp_ytc is None and any(k in cell for k in ytc_header_kws):
                    tmp_ytc = c

            if tmp_call is not None and tmp_ytc is not None:
                header_row = r
                call_col = tmp_call
                ytc_col = tmp_ytc
                break

        if header_row is None:
            continue

        blank_streak = 0
        for rr in range(header_row + 1, R):
            row_vals = [normalize_text(x) for x in arr[rr].tolist()]
            row_join = " ".join([x for x in row_vals if x])

            if not row_join:
                blank_streak += 1
                if blank_streak >= 2:
                    break
                continue
            blank_streak = 0

            first_nonempty = next((x for x in row_vals if x), "")
            if _is_top_heading(first_nonempty):
                break

            call_val = ""
            ytc_val = ""

            if call_col is not None and call_col < C:
                call_val = _to_pct_text(arr[rr][call_col], min_v=0, max_v=100)

            if ytc_col is not None and ytc_col < C:
                ytc_val = _to_pct_text(arr[rr][ytc_col], min_v=0, max_v=30)

            if not call_val and call_col is not None:
                for cc in range(max(0, call_col - 1), min(C, call_col + 2)):
                    call_val = _to_pct_text(arr[rr][cc], min_v=0, max_v=100)
                    if call_val:
                        break

            if not ytc_val and ytc_col is not None:
                for cc in range(max(0, ytc_col - 1), min(C, ytc_col + 2)):
                    ytc_val = _to_pct_text(arr[rr][cc], min_v=0, max_v=30)
                    if ytc_val:
                        break

            if call_val or ytc_val:
                pairs.append((call_val, ytc_val))

    uniq_pairs = []
    for p in pairs:
        if p not in uniq_pairs:
            uniq_pairs.append(p)

    for call_val, ytc_val in uniq_pairs:
        if call_val and ytc_val:
            return call_val, ytc_val, uniq_pairs

    for call_val, ytc_val in uniq_pairs:
        if call_val or ytc_val:
            return call_val, ytc_val, uniq_pairs

    return "", "", []


# ==========================================================
# [Call 본문에서 Call 비율 / YTC 추출]
# ==========================================================
def extract_call_ratio_and_ytc_from_text(text: str) -> Tuple[str, str]:
    if not text:
        return "", ""

    ratio = ""
    ytc = ""

    ratio_patterns = [
        r"(?:행사비율|콜옵션비율|매도청구권\s*행사비율|Call\s*비율)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*%",
        r"(?:권면총액|권면액|전자등록총액|전자등록금액|인수금액|발행금액|사채원금)\s*(?:의|중)\s*(\d+(?:\.\d+)?)\s*%",
        r"(\d+(?:\.\d+)?)\s*%\s*(?:에\s*해당하는|이내의\s*범위|총\s*한도로)",
    ]
    ytc_patterns = [
        r"(?:YTC|매도청구권보장수익률|매도청구수익률|조기상환수익률|조기상환이율|연복리수익률)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*%",
        r"연\s*복리\s*(\d+(?:\.\d+)?)\s*%",
        r"연복리\s*(\d+(?:\.\d+)?)\s*%",
        r"IRR.*?연\s*(\d+(?:\.\d+)?)\s*%",
        r"내부수익률.*?연\s*(\d+(?:\.\d+)?)\s*%",
        r"연\s*(\d+(?:\.\d+)?)\s*%\s*\(\s*3개월\s*단위\s*복리계산\s*\)",
    ]

    for pat in ratio_patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            ratio = f"{m.group(1)}%"
            break

    for pat in ytc_patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            ytc = f"{m.group(1)}%"
            break

    return ratio, ytc


# ==========================================================
# [최종 파서]
# 로직 순서
# 1. 9.1 전체를 Put에 넣고
# 2. 그 Put 안에서 Call을 떼서
# 3. Call은 Call 컬럼으로 보내고
# 4. Put에서는 Call만 제거한다
# 5. 9.1이 "-" 면 Put/Call 둘 다 "-"
# 6. 9.1 전체가 22./23. 참조형이면 공시 확인 바람
# 7. 정정 보고서면 위쪽 9.1은 넘기고 아래쪽 "실제 본문형 9.1"을 우선 사용
# ==========================================================
def parse_bond_option_record(rec: Dict[str, Any]) -> Dict[str, str]:
    raw_title = rec.get("title", "") or ""
    title = clean_title(raw_title)
    tables = rec.get("tables", [])

    is_corr = is_correction_title(raw_title) or is_correction_title(title)
    corr_after = extract_correction_after_map(tables) if is_corr else {}

    row = {
        "Put Option": "",
        "Call Option": "",
        "Call 비율": "",
        "YTC": "",
    }

    lines = _lines_from_tables(tables)
    corpus = _corpus_from_lines(lines)

    # 1) lines 기준으로 9.1 후보 전부 추출
    sec_candidates = extract_all_91_sections_from_lines(lines)
    section_91 = _select_best_91_section(sec_candidates, is_corr=is_corr)

    # 2) lines에서 못 잡으면 corpus fallback
    if not section_91:
        section_91 = extract_91_option_section_from_corpus(corpus, use_last_91=is_corr)

    section_91 = _clean_line(section_91)

    # 3) 9.1 자체를 못 찾으면 공시 확인 바람
    if not section_91:
        row["Put Option"] = "공시 확인 바람"
        row["Call Option"] = "공시 확인 바람"
        call_text = ""

    # 4) 9.1이 "-" 이면 Put/Call 둘 다 "-"
    elif _is_dash_91_section(section_91):
        row["Put Option"] = "-"
        row["Call Option"] = "-"
        call_text = ""

    # 5) 9.1 전체가 22./23. 참조형이면 Put/Call 둘 다 공시 확인 바람
    elif _is_reference_only_22_or_23_section(section_91):
        row["Put Option"] = "공시 확인 바람"
        row["Call Option"] = "공시 확인 바람"
        call_text = ""

    else:
        # 6) 먼저 9.1 전체를 Put 원본으로 잡는다
        put_text = section_91

        # 7) 그 Put 안에서만 Call을 찾는다
        call_text = extract_call_option_text_from_section(put_text)

        # 8) Call은 Call 컬럼으로 보낸다
        row["Call Option"] = call_text if call_text else "-"

        # 9) Put에서는 Call만 제거한다
        if call_text:
            put_text = remove_call_option_text_from_section(put_text)

        row["Put Option"] = put_text if put_text else "공시 확인 바람"

    # 10) Call 비율 / YTC : 표 key-value 우선
    row["Call 비율"] = _safe_percent(
        scan_label_value_preferring_correction(
            tables,
            ["콜옵션 행사비율", "매도청구권 행사비율", "Call 비율", "행사비율"],
            corr_after,
        )
    )

    row["YTC"] = _safe_percent(
        scan_label_value_preferring_correction(
            tables,
            ["조기상환수익률", "YTC", "Yield To Call", "연복리수익률", "매도청구권보장수익률"],
            corr_after,
        )
    )

    # 11) 표 grid fallback
    if not row["Call 비율"] or not row["YTC"]:
        table_ratio, table_ytc, _ = extract_call_ratio_ytc_from_table_grid(tables)

        if not row["Call 비율"]:
            row["Call 비율"] = table_ratio
        if not row["YTC"]:
            row["YTC"] = table_ytc

    # 12) Call 본문 fallback
    if (not row["Call 비율"] or not row["YTC"]) and call_text and call_text != "공시 확인 바람":
        ext_ratio, ext_ytc = extract_call_ratio_and_ytc_from_text(call_text)

        if not row["Call 비율"]:
            row["Call 비율"] = ext_ratio
        if not row["YTC"]:
            row["YTC"] = ext_ytc

    return row


# ==========================================================
# [헤더 후보 / 시트 업데이트]
# ==========================================================
BOND_ACPTNO_CANDIDATES = ["접수번호", "acptNo", "acptno"]
PUT_COL_CANDIDATES = ["Put Option", "Put옵션", "Put"]
CALL_COL_CANDIDATES = ["Call Option", "Call옵션", "Call"]
CALL_RATIO_COL_CANDIDATES = ["Call 비율", "콜옵션 비율"]
YTC_COL_CANDIDATES = ["YTC"]


def gs_retry(fn, *args, **kwargs):
    last_err = None
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            last_err = e
            msg = str(e)
            if "429" in msg or "Quota exceeded" in msg:
                sleep_s = (2 ** attempt) + random.uniform(0.3, 1.2)
                time.sleep(sleep_s)
                continue
            raise
    raise last_err if last_err else RuntimeError("Unknown Google Sheets error")


def _normalize_header(s: Any) -> str:
    return str(s).strip()


def _header_to_col_map(header_row):
    out = {}
    for i, h in enumerate(header_row, start=1):
        key = _normalize_header(h)
        if key:
            out[key] = i
    return out


def _find_col(header_map: Dict[str, int], candidates) -> Optional[int]:
    for c in candidates:
        if c in header_map:
            return header_map[c]
    return None


def _truncate_sheet_text(value: Any, limit: int = 49000) -> str:
    s = "" if value is None else str(value)
    if len(s) <= limit:
        return s
    return s[: limit - 20] + " ...[TRUNCATED]"


def open_worksheets():
    sh = gs_open()
    raw_ws = ensure_ws(sh, RAW_SHEET_NAME, rows=5000, cols=250)
    bond_ws = ensure_ws(sh, BOND_SHEET_NAME, rows=3000, cols=60)
    return raw_ws, bond_ws


def build_bond_sheet_context(bond_ws):
    values = gs_retry(bond_ws.get_all_values)
    if not values:
        raise RuntimeError(f"{BOND_SHEET_NAME} 시트가 비어 있습니다.")

    header = values[0]
    rows = values[1:]
    header_map = _header_to_col_map(header)

    acptno_col = _find_col(header_map, BOND_ACPTNO_CANDIDATES)
    put_col = _find_col(header_map, PUT_COL_CANDIDATES)
    call_col = _find_col(header_map, CALL_COL_CANDIDATES)
    ratio_col = _find_col(header_map, CALL_RATIO_COL_CANDIDATES)
    ytc_col = _find_col(header_map, YTC_COL_CANDIDATES)

    missing = []
    if not acptno_col:
        missing.append("접수번호")
    if not put_col:
        missing.append("Put Option")
    if not call_col:
        missing.append("Call Option")
    if not ratio_col:
        missing.append("Call 비율")
    if not ytc_col:
        missing.append("YTC")
    if missing:
        raise RuntimeError(f"{BOND_SHEET_NAME} 시트 헤더 누락: {', '.join(missing)}")

    row_map: Dict[str, int] = {}
    for i, row in enumerate(rows, start=2):
        acptno = ""
        if len(row) >= acptno_col:
            acptno = str(row[acptno_col - 1]).strip()
        if acptno:
            row_map[acptno] = i

    return {
        "row_map": row_map,
        "put_col": put_col,
        "call_col": call_col,
        "ratio_col": ratio_col,
        "ytc_col": ytc_col,
    }


def update_option_row(ws, row_num: int, put_col: int, call_col: int, ratio_col: int, ytc_col: int, parsed: Dict[str, str]):
    put_val = _truncate_sheet_text(parsed.get("Put Option", ""))
    call_val = _truncate_sheet_text(parsed.get("Call Option", ""))
    ratio_val = _truncate_sheet_text(parsed.get("Call 비율", ""))
    ytc_val = _truncate_sheet_text(parsed.get("YTC", ""))

    data = [
        {"range": rowcol_to_a1(row_num, put_col), "values": [[put_val]]},
        {"range": rowcol_to_a1(row_num, call_col), "values": [[call_val]]},
        {"range": rowcol_to_a1(row_num, ratio_col), "values": [[ratio_val]]},
        {"range": rowcol_to_a1(row_num, ytc_col), "values": [[ytc_val]]},
    ]
    gs_retry(ws.batch_update, data)


def is_bond_title(title: str) -> bool:
    t = (title or "").replace(" ", "")
    return any(
        k in t
        for k in [
            "전환사채권발행결정",
            "교환사채권발행결정",
            "신주인수권부사채권발행결정",
        ]
    )


def run_parser():
    raw_ws, bond_ws = open_worksheets()

    raw_records = load_raw_records(raw_ws)
    raw_records = [r for r in raw_records if is_bond_title(clean_title(r.get("title", "")))]

    if RUN_ONLY_ACPTNO:
        raw_records = [r for r in raw_records if str(r.get("acpt_no", "")).strip() == RUN_ONLY_ACPTNO]

    ctx = build_bond_sheet_context(bond_ws)
    row_map = ctx["row_map"]

    print(f"[DEBUG] RAW bond records = {len(raw_records)}")
    print(f"[DEBUG] Bond sheet rows  = {len(row_map)}")
    print(f"[DEBUG] Target sheet     = {BOND_SHEET_NAME}")

    ok = 0
    skip = 0
    fail = 0

    for rec in raw_records:
        acptno = str(rec.get("acpt_no", "")).strip()
        title = clean_title(rec.get("title", "") or "")

        if not acptno:
            skip += 1
            print(f"[SKIP][NO_ACPTNO] {title}")
            continue

        row_num = row_map.get(acptno)
        if not row_num:
            skip += 1
            print(f"[SKIP][NO_ROW_IN_BOND] {acptno} {title}")
            continue

        try:
            parsed = parse_bond_option_record(rec)

            if not str(parsed.get("Put Option", "")).strip():
                parsed["Put Option"] = "공시 확인 바람"
            if not str(parsed.get("Call Option", "")).strip():
                parsed["Call Option"] = "공시 확인 바람"

            update_option_row(
                bond_ws,
                row_num=row_num,
                put_col=ctx["put_col"],
                call_col=ctx["call_col"],
                ratio_col=ctx["ratio_col"],
                ytc_col=ctx["ytc_col"],
                parsed=parsed,
            )

            put_found = parsed.get("Put Option", "") != "공시 확인 바람"
            call_found = parsed.get("Call Option", "") != "공시 확인 바람"

            print(
                f"[OK][OPTION][UPDATE] {acptno} {title} "
                f"(row={row_num}, put={'Y' if put_found else 'N'}, call={'Y' if call_found else 'N'})"
            )
            ok += 1
            time.sleep(0.15)

        except Exception as e:
            print(f"[FAIL][OPTION] {acptno} {title} :: {e}")
            fail += 1

    print(f"[DONE][OPTION] ok={ok} skip={skip} fail={fail}")


if __name__ == "__main__":
    run_parser()
