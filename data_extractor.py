"""
data_extractor.py
Rule-based structured data extraction from noisy OCR text.

Handles:
- OCR character mistakes: "12O" → "120", "l" → "1", "O" → "0"
- Flexible keyword matching with aliases for each field
- Two extraction strategies:
    1. Label-based  : "Feed: 120" → find label then grab adjacent number
    2. Range-based  : no label found → classify by value range (ABW, FCR)
- Returns clean structured dict ready for DB storage and display
"""

import re
from datetime import datetime


# ══════════════════════════════════════════════════════════════
# STEP 1: CHARACTER-LEVEL OCR CORRECTIONS
# ══════════════════════════════════════════════════════════════

# Letters that look like digits in OCR output
DIGIT_FIXES = {
    'O': '0', 'o': '0',
    'I': '1', 'l': '1',
    'S': '5', 'B': '8',
    'Z': '2', 'G': '6',
    'g': '9', 'q': '9',
}

# Symbol-level global replacements
SYMBOL_FIXES = {
    '|': 'I',
    '$': 'S',
    '!': '1',
    '"': '',
    "'": '',
    '\u2014': '-',  # em dash
    '\u2013': '-',  # en dash
}

def fix_ocr_number(token: str) -> str:
    """
    Fix letter→digit mistakes inside a token that contains at least one digit.
    "12O"  → "120"
    "24l"  → "241"
    "1O.O7" → "10.07"
    Only activates when the token has ≥1 real digit (avoids mangling words).
    """
    if not any(c.isdigit() for c in token):
        return token
    return ''.join(DIGIT_FIXES.get(c, c) for c in token)


def clean_ocr_text(raw: str) -> str:
    """
    Full text cleaning pipeline:
    1. Normalise line endings and whitespace
    2. Apply symbol fixes
    3. Fix digit look-alikes inside number-like tokens
    4. Remove non-printable garbage characters
    5. Normalise colon spacing
    """
    text = raw

    # 1. Normalise newlines and whitespace
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    # 2. Symbol fixes
    for wrong, right in SYMBOL_FIXES.items():
        text = text.replace(wrong, right)

    # 3. Fix digit look-alikes token-by-token
    def fix_token(m):
        return fix_ocr_number(m.group(0))
    text = re.sub(r'\b[\w.]+\b', fix_token, text)

    # 4. Remove characters that don't belong in farm records
    text = re.sub(r'[^a-zA-Z0-9\s:./\-,\n]', '', text)

    # 5. Normalise colon spacing: "Eggs  :  24" → "Eggs: 24"
    text = re.sub(r'\s*:\s*', ': ', text)

    return text.strip()


# ══════════════════════════════════════════════════════════════
# STEP 2: KEYWORD ALIASES FOR EACH FIELD
# ══════════════════════════════════════════════════════════════

FIELD_KEYWORDS = {
    'feed': [
        'feed', 'fed', 'feeds', 'feed qty', 'feed quantity',
        'feed kg', 'feedkg', 'fced', 'feeed', 'khana', 'aahar',
    ],
    'eggs': [
        'eggs', 'egg', 'egs', 'eg', 'egss', 'egas', 'anda', 'ande',
        'total eggs', 'egg count', 'no of eggs', 'no. of eggs',
        'egg production',
    ],
    'mortality': [
        'mortality', 'mort', 'mortal', 'dead', 'death', 'deaths',
        'mortlty', 'mortalty', 'birds dead', 'bird death',
        'mrutyu', 'maut', 'no of dead',
    ],
    'date': [
        'date', 'dt', 'day', 'dated',
    ],
    'batch': [
        'batch', 'flock', 'lot', 'shed', 'house', 'batch no',
        'flock no', 'group',
    ],
    'birds': [
        'birds', 'bird count', 'total birds', 'stock', 'placement',
        'no of birds', 'opening stock',
    ],
}


# ══════════════════════════════════════════════════════════════
# STEP 3: NUMBER EXTRACTION HELPERS
# ══════════════════════════════════════════════════════════════

def extract_number_from_segment(text: str):
    """
    Extract the first number (int or float) from a text segment.
    Applies OCR fix first.
    Returns int/float or None.
    """
    fixed   = fix_ocr_number(text)
    matches = re.findall(r'\d+(?:\.\d+)?', fixed)
    if matches:
        val = matches[0]
        return float(val) if '.' in val else int(val)
    return None


def find_field_in_lines(lines: list, keywords: list):
    """
    Search OCR lines for a keyword match, then extract the adjacent number.

    Strategy (two passes):
    Pass 1: keyword + number on the SAME line  → "Feed: 120"
    Pass 2: number on the NEXT line            → "Feed\n120"
    """
    for i, line in enumerate(lines):
        line_lower = line.lower()
        for kw in keywords:
            if kw in line_lower:
                # Pass 1 — same line
                num = extract_number_from_segment(line)
                if num is not None:
                    return num
                # Pass 2 — next line
                if i + 1 < len(lines):
                    num = extract_number_from_segment(lines[i + 1])
                    if num is not None:
                        return num
    return None


# ══════════════════════════════════════════════════════════════
# STEP 4: DATE EXTRACTION
# ══════════════════════════════════════════════════════════════

DATE_PATTERNS = [
    r'\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})\b',   # 12/05/2026
    r'\b(\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})\b',      # 2026/05/12
    r'\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})\b',
]

def extract_date(text: str) -> str | None:
    for pat in DATE_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


# ══════════════════════════════════════════════════════════════
# STEP 5: ABW / FCR TABLE EXTRACTION (for growth registers)
# ══════════════════════════════════════════════════════════════

def extract_abw_values(text: str) -> list:
    """
    ABW (Actual Body Weight) — integers 42–1800 grams.
    Masks decimals first so FCR values don't bleed into ABW list.
    Then keeps only values that follow a broadly increasing trend.
    """
    masked = re.sub(r'\b\d+\.\d+\b', 'DECIMAL', text)
    candidates = []
    for m in re.finditer(r'\b(\d{2,4})\b', masked):
        val = int(fix_ocr_number(m.group(1)))
        if 42 <= val <= 1800:
            candidates.append(val)

    # Deduplicate preserving order
    seen, unique = set(), []
    for v in candidates:
        if v not in seen:
            seen.add(v); unique.append(v)

    return _filter_increasing(unique)


def _filter_increasing(values: list) -> list:
    """
    Remove values that break the expected increasing ABW trend.
    Allow up to 30% dip (measurement variation).
    """
    if len(values) <= 3:
        return values
    filtered = [values[0]]
    for v in values[1:]:
        if v >= filtered[-1] * 0.70:
            filtered.append(v)
    return filtered


def extract_fcr_values(text: str) -> list:
    """
    FCR — decimals 0.10–1.50 (typical for broilers weeks 1–5).
    Fix OCR errors inside decimal patterns before parsing.
    """
    fixed = text
    def fix_decimal(m):
        whole = fix_ocr_number(m.group(1))
        frac  = fix_ocr_number(m.group(2))
        return f"{whole}.{frac}"

    fixed = re.sub(r'\b([0-9Ool]{1,2})\.([0-9OolBSZ]{2,3})\b',
                   fix_decimal, fixed)

    candidates = []
    for m in re.finditer(r'\b(\d{1,2}\.\d{2,3})\b', fixed):
        try:
            val = round(float(m.group(1)), 2)
            if 0.10 <= val <= 1.50:
                candidates.append(val)
        except ValueError:
            continue

    seen, unique = set(), []
    for v in candidates:
        if v not in seen:
            seen.add(v); unique.append(v)
    return unique


def extract_medicine_notes(text: str) -> list:
    keywords = [
        'inject', 'vaccine', 'medicine', 'dose', r'\bml\b',
        'tylo', 'palm', 'lacto', 'genta', 'debu', 'linco',
        'hepa', 'sanit', 'disinfect', 'inj',
    ]
    pattern = '|'.join(keywords)
    notes   = []
    for line in text.split('\n'):
        if re.search(pattern, line, re.IGNORECASE):
            cleaned = line.strip()
            if len(cleaned) > 3:
                notes.append(cleaned)
    return notes


# ══════════════════════════════════════════════════════════════
# STEP 6: MAIN EXTRACTION FUNCTION
# ══════════════════════════════════════════════════════════════

def extract_structured_data(raw_text: str) -> dict:
    """
    Master function: takes raw OCR text, returns clean structured dict.

    Returns:
    {
        "feed":            int or None,
        "eggs":            int or None,
        "mortality":       int or None,
        "date":            str or None,
        "batch":           str or None,
        "birds":           int or None,
        "abw_values":      [list of ints],
        "fcr_values":      [list of floats],
        "latest_abw":      int or None,
        "latest_fcr":      float or None,
        "total_mortality": int or None,
        "medicine_notes":  [list of str],
        "cleaned_text":    str,
        "formatted":       str,
    }
    """
    cleaned = clean_ocr_text(raw_text)
    lines   = [l.strip() for l in cleaned.split('\n') if l.strip()]

    # Label-based extraction (daily record sheets)
    feed     = find_field_in_lines(lines, FIELD_KEYWORDS['feed'])
    eggs     = find_field_in_lines(lines, FIELD_KEYWORDS['eggs'])
    mortality= find_field_in_lines(lines, FIELD_KEYWORDS['mortality'])
    birds    = find_field_in_lines(lines, FIELD_KEYWORDS['birds'])
    date_val = extract_date(cleaned)

    # Batch — find text near 'batch'/'flock' keyword
    batch = None
    for line in lines:
        if any(kw in line.lower() for kw in FIELD_KEYWORDS['batch']):
            # Extract alphanumeric token after the keyword
            m = re.search(r'(?:batch|flock|lot|shed|house)[^\w]*(\w+)', line, re.IGNORECASE)
            if m:
                batch = m.group(1)
                break

    # Range-based extraction (growth register tables)
    abw_vals = extract_abw_values(cleaned)
    fcr_vals = extract_fcr_values(cleaned)
    medicines= extract_medicine_notes(cleaned)

    # Mortality from table if not found by label
    if mortality is None and abw_vals:
        mort_match = find_field_in_lines(lines, ['mort', 'dead', 'death'])
        mortality  = mort_match

    # Build one-line summary
    parts = []
    if feed       is not None: parts.append(f"Feed: {feed}")
    if eggs       is not None: parts.append(f"Eggs: {eggs}")
    if mortality  is not None: parts.append(f"Mortality: {mortality}")
    if date_val               : parts.append(f"Date: {date_val}")
    if abw_vals               : parts.append(f"Latest ABW: {abw_vals[-1]} gms")
    if fcr_vals               : parts.append(f"Latest FCR: {fcr_vals[-1]}")
    formatted = "  |  ".join(parts) if parts else "No structured data detected"

    return {
        # Daily record fields
        "feed":            int(feed)     if feed     is not None else None,
        "eggs":            int(eggs)     if eggs     is not None else None,
        "mortality":       int(mortality)if mortality is not None else None,
        "date":            date_val,
        "batch":           batch,
        "birds":           int(birds)   if birds    is not None else None,
        # Growth register fields
        "abw_values":      abw_vals,
        "fcr_values":      fcr_vals,
        "latest_abw":      abw_vals[-1] if abw_vals else None,
        "latest_fcr":      fcr_vals[-1] if fcr_vals else None,
        "total_mortality": int(mortality) if mortality is not None else None,
        "medicine_notes":  medicines,
        # Meta
        "cleaned_text":    cleaned,
        "formatted":       formatted,
    }