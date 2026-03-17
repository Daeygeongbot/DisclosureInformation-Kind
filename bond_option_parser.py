def extract_91_option_section_from_lines(lines: List[str], use_last_91: bool = False) -> str:
    if not lines:
        return ""

    hit_indices = []
    for i, line in enumerate(lines):
        s = _clean_line(line)
        if s and _is_91_heading(s):
            hit_indices.append(i)

    if not hit_indices:
        return ""

    start_idx = hit_indices[-1] if use_last_91 else hit_indices[0]

    bucket = []
    first_line = _strip_91_heading_prefix(lines[start_idx])
    if first_line:
        bucket.append(first_line)

    for i in range(start_idx + 1, len(lines)):
        s = _clean_line(lines[i])
        if not s:
            continue
        if _is_next_major_heading(s):
            break
        bucket.append(s)

    text = " ".join(bucket).strip()
    text = re.sub(r"\s{2,}", " ", text)
    return text
