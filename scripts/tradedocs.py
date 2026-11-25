#!/usr/bin/env python3
"""무역서류 에이전트 CLI

사용법 (Windows는 python3 대신 python):
  python3 scripts/tradedocs.py validate deals/D2026-001.json
  python3 scripts/tradedocs.py generate deals/D2026-001.json [--docs PI,CI,PL,SI] [--pdf] [--force]
  python3 scripts/tradedocs.py bank-verify      # 사람이 터미널에서 직접 실행 (계좌 확인 기록)
  python3 scripts/tradedocs.py approve deals/D2026-001.json   # 사람이 직접 실행 (서류 승인)

필요 패키지: openpyxl (pip install openpyxl)
PDF 변환(--pdf): LibreOffice(soffice)가 설치되어 있어야 합니다.
"""
import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUTPUT = ROOT / "output"
BANK_LOCK = DATA / "bank_lock.json"

INCOTERMS = {
    # rule: (group, sea_only, seller_pays_main_freight, seller_buys_insurance)
    "EXW": ("E", False, False, False),
    "FCA": ("F", False, False, False),
    "FAS": ("F", True, False, False),
    "FOB": ("F", True, False, False),
    "CFR": ("C", True, True, False),
    "CIF": ("C", True, True, True),
    "CPT": ("C", False, True, False),
    "CIP": ("C", False, True, True),
    "DAP": ("D", False, True, False),
    "DPU": ("D", False, True, False),
    "DDP": ("D", False, True, False),
}

DOC_TITLES = {
    "PI": "PROFORMA INVOICE",
    "CI": "COMMERCIAL INVOICE",
    "PL": "PACKING LIST",
    "SI": "SHIPPING INSTRUCTION",
    "PO": "PURCHASE ORDER",
}

# 역할별로 우리가 '발행'하는 서류. 공급처 PI·CI는 만들지 않고 받은 서류를 대조합니다.
DOCS_BY_ROLE = {"seller": ("PI", "CI", "PL", "SI"), "buyer": ("PO",)}

# 금액 소수 자릿수(기본 2). 보조단위가 없는 통화는 0.
CURRENCY_DECIMALS = {"JPY": 0, "KRW": 0}

DEAL_ID_RE = re.compile(r"^[A-Za-z0-9-]+$")
BANK_FIELDS = ("bank_name", "branch", "swift", "account_no", "beneficiary")
BANK_REQUIRED = ("bank_name", "swift", "account_no", "beneficiary")
BANK_STALE_DAYS = 90
# "L/C", "LC", "Letter of Credit", "Documentary Credit"만 L/C로 봅니다("30 days credit" 같은 외상 조건은 제외).
LC_PAYMENT_RE = re.compile(r"\bL\s*/\s*C\b|\bLC\b|LETTER\s+OF\s+CREDIT|DOCUMENTARY\s+CREDIT", re.I)


# ---------------------------------------------------------------- helpers
def D(x):
    return Decimal(str(x if x not in (None, "") else 0))


def num(v, default=None):
    """값을 Decimal로 바꿉니다. 비어 있으면 default, 숫자가 아니면 ValueError."""
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        raise ValueError(repr(v))
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        raise ValueError(repr(v)) from None
    if not d.is_finite():
        raise ValueError(repr(v))
    return d


def q2(x):
    return D(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def q3(x):
    return D(x).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def decimals(cur):
    return CURRENCY_DECIMALS.get((cur or "").upper(), 2)


def qm(x, cur):
    """통화별 자릿수로 금액 반올림 (USD 0.01, JPY·KRW 1)"""
    return D(x).quantize(Decimal(1).scaleb(-decimals(cur)), rounding=ROUND_HALF_UP)


def money_fmt(cur):
    return "#,##0" if decimals(cur) == 0 else "#,##0.00"


def load_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def load_csv(p, key):
    raw = Path(p).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp949")
        except UnicodeDecodeError:
            sys.exit(f"{Path(p).name} 파일의 인코딩을 읽을 수 없습니다. 엑셀에서 'CSV UTF-8(쉼표로 분리)' 형식으로 다시 저장하세요.")
        print(f"[주의] {Path(p).name}이 UTF-8이 아니어서 cp949로 읽었습니다. "
              "엑셀에서 'CSV UTF-8(쉼표로 분리)' 형식으로 다시 저장하세요.", file=sys.stderr)
    rows = csv.DictReader(io.StringIO(text, newline=""))
    return {r[key].strip(): r for r in rows if (r.get(key) or "").strip()}


def parse_date(s):
    if not s:
        return None
    if not isinstance(s, str):
        return "INVALID"
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return "INVALID"


def _as_dict(v):
    return v if isinstance(v, dict) else {}


def incoterm_of(deal):
    return _as_dict(deal.get("incoterm"))


def lc_of(deal):
    return _as_dict(deal.get("lc"))


def valid_deal_id(v):
    return isinstance(v, str) and bool(DEAL_ID_RE.match(v))


def _place_key(s):
    """'Busan, Korea' → 'busan', 'Port of Tokyo (JP)' → 'tokyo'"""
    key = re.split(r"[,(/]", str(s or ""))[0].strip().lower()
    return re.sub(r"^port of\s+|\s+port$", "", key)


def same_place(a, b):
    ka, kb = _place_key(a), _place_key(b)
    return not ka or not kb or ka in kb or kb in ka


_ONES = ["", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE",
         "TEN", "ELEVEN", "TWELVE", "THIRTEEN", "FOURTEEN", "FIFTEEN", "SIXTEEN",
         "SEVENTEEN", "EIGHTEEN", "NINETEEN"]
_TENS = ["", "", "TWENTY", "THIRTY", "FORTY", "FIFTY", "SIXTY", "SEVENTY", "EIGHTY", "NINETY"]


def _words_999(n):
    out = []
    if n >= 100:
        out += [_ONES[n // 100], "HUNDRED"]
        n %= 100
    if n >= 20:
        out.append(_TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else ""))
    elif n:
        out.append(_ONES[n])
    return " ".join(out)


def int_words(n):
    if n == 0:
        return "ZERO"
    parts = []
    for value, name in ((10**9, "BILLION"), (10**6, "MILLION"), (10**3, "THOUSAND"), (1, "")):
        if n >= value:
            parts.append((_words_999(n // value) + " " + name).strip())
            n %= value
    return " ".join(parts)


CURRENCY_WORDS = {"USD": ("US DOLLARS", "CENTS"), "EUR": ("EUROS", "CENTS"),
                  "JPY": ("JAPANESE YEN", None), "CNY": ("CHINESE YUAN", "FEN"),
                  "KRW": ("KOREAN WON", None)}


def amount_words(amount, currency):
    # 금액란과 같은 자릿수로 반올림해야 숫자와 영문이 어긋나지 않습니다.
    amount = qm(amount, currency)
    major, minor = CURRENCY_WORDS.get(currency, (currency, "CENTS"))
    whole = int(amount)
    cents = int((amount - whole) * 100)
    s = f"SAY {major} {int_words(whole)}"
    if cents and minor:
        s += f" AND {minor} {int_words(cents)}"
    return s + " ONLY."


# ---------------------------------------------------------------- context
class Report:
    def __init__(self):
        self.items = []

    def add(self, level, code, msg):
        self.items.append({"level": level, "code": code, "message": msg})

    def error(self, code, msg):
        self.add("ERROR", code, msg)

    def warn(self, code, msg):
        self.add("WARNING", code, msg)

    def info(self, code, msg):
        self.add("INFO", code, msg)

    def count(self, level):
        return sum(1 for i in self.items if i["level"] == level)


def build_context(deal_path):
    deal = load_json(deal_path)
    if not isinstance(deal, dict):
        sys.exit(f"{deal_path}: deal JSON은 객체({{...}})여야 합니다.")
    company = load_json(DATA / "company.json")
    partners = load_csv(DATA / "partners.csv", "partner_id")
    products = load_csv(DATA / "products.csv", "sku")
    defaults = load_json(DATA / "defaults.json")
    rep = Report()

    for field in ("deal_id", "our_role", "partner_id", "doc_date", "incoterm", "payment_terms", "items"):
        if not deal.get(field):
            rep.error("REQ_FIELD", f"필수 항목 '{field}'이(가) 비어 있습니다.")

    # 형식 검사: 다른 에이전트가 형식을 틀려도 멈추지 않고 ERROR로 보고합니다.
    if deal.get("deal_id") and not valid_deal_id(deal.get("deal_id")):
        rep.error("DEAL_ID_INVALID", f"deal_id '{deal.get('deal_id')}'는 영문·숫자·하이픈만 쓸 수 있습니다.")
    role = str(deal.get("our_role") or "").strip().lower()
    if deal.get("our_role") and role not in DOCS_BY_ROLE:
        rep.error("ROLE_INVALID", f"our_role '{deal.get('our_role')}'는 seller 또는 buyer여야 합니다.")
    if deal.get("incoterm") and not isinstance(deal.get("incoterm"), dict):
        rep.error("INCOTERM_TYPE", 'incoterm은 {"rule": "FOB", "place": "Busan, Korea"} 형식이어야 합니다.')
    if deal.get("lc") is not None and not isinstance(deal.get("lc"), dict):
        rep.error("LC_TYPE", "lc는 객체({...})여야 합니다.")
    items = deal.get("items") or []
    if not isinstance(items, list):
        rep.error("ITEMS_TYPE", "items는 배열([...])이어야 합니다.")
        items = []

    pid = str(deal.get("partner_id") or "").strip()
    partner = partners.get(pid)
    if pid and not partner:
        rep.error("PARTNER_MISSING", f"거래처 {pid}가 data/partners.csv에 없습니다.")

    currency = str(deal.get("currency") or defaults.get("currency", "USD")).strip().upper()
    lines, carton_no = [], 1
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            rep.error("ITEM_FORMAT", f"{i}번 품목 형식이 잘못되었습니다.")
            continue
        sku = str(it.get("sku") or "").strip()
        p = products.get(sku)
        if not p:
            rep.error("PRODUCT_MISSING", f"{i}번 품목 SKU '{it.get('sku')}'가 data/products.csv에 없습니다.")
            continue
        try:
            qty = num(it.get("qty"), Decimal(0))
            shipped = num(it.get("shipped_qty"), qty)
            price_override = num(it.get("unit_price"))
            master_price = num(p.get("unit_price"), Decimal(0))
            per_ctn_d = num(p.get("qty_per_carton"), Decimal(0))
            net_unit = num(p.get("net_kg_per_unit"), Decimal(0))
            gross_ctn = num(p.get("gross_kg_per_carton"), Decimal(0))
            dims = [num(p.get(k), Decimal(0)) for k in ("carton_l_cm", "carton_w_cm", "carton_h_cm")]
        except ValueError as e:
            rep.error("NUM_FORMAT", f"{i}번 품목 {sku}: 숫자가 아닌 값이 있습니다 {e} (deal 또는 products.csv 확인).")
            continue
        price = price_override if price_override is not None else master_price
        per_ctn = int(per_ctn_d) if per_ctn_d > 0 and per_ctn_d == per_ctn_d.to_integral_value() else 0
        ctns = math.ceil(shipped / per_ctn) if per_ctn and shipped > 0 else 0
        cbm_per = dims[0] * dims[1] * dims[2] / D(1_000_000)
        net = shipped * net_unit if shipped > 0 else Decimal(0)
        # 덜 찬 마지막 박스는 '포장재 무게 + 실제 내용물'로 추정합니다(꽉 찬 박스 무게로 계산하면 과다).
        tare = gross_ctn - net_unit * per_ctn
        rest = shipped % per_ctn if per_ctn and shipped > 0 else Decimal(0)
        if rest and tare > 0:
            gross = D(ctns - 1) * gross_ctn + tare + rest * net_unit
            last_gross = tare + rest * net_unit
        else:
            gross, last_gross = D(ctns) * gross_ctn, None
        item_origin = str(it.get("origin") or "").strip()
        lines.append({
            "no": i, "sku": p["sku"], "description": p["name_en"], "model": p["model"],
            "name_ko": p["name_ko"], "hs_code": p["hs_code"], "unit": p["unit"],
            "origin": item_origin or p["origin"], "master_origin": p["origin"],
            "origin_source": "deal" if item_origin else "master",
            "product_currency": (p.get("currency") or "").strip().upper(),
            "qty": qty, "shipped_qty": shipped, "unit_price": price,
            "master_price": master_price, "price_from_master": price_override is None,
            "amount_contract": qm(qty * price, currency), "amount_shipped": qm(shipped * price, currency),
            "qty_per_carton": per_ctn, "cartons": ctns,
            "carton_from": carton_no if ctns else None,
            "carton_to": carton_no + ctns - 1 if ctns else None,
            "net_kg": q2(net), "gross_kg": q2(gross), "cbm": q3(cbm_per * ctns),
            "gross_per_ctn": gross_ctn,
            "net_per_ctn": net_unit * per_ctn, "last_carton_gross": last_gross,
            "change_reason": it.get("change_reason", ""),
        })
        carton_no += ctns

    try:
        freight = qm(num(deal.get("freight"), Decimal(0)), currency)
        insurance = qm(num(deal.get("insurance"), Decimal(0)), currency)
    except ValueError as e:
        rep.error("NUM_FORMAT", f"freight/insurance가 숫자가 아닙니다 {e}.")
        freight = insurance = qm(0, currency)
    goods_c = sum((l["amount_contract"] for l in lines), Decimal(0))
    goods_s = sum((l["amount_shipped"] for l in lines), Decimal(0))
    totals = {
        "currency": currency,
        "goods_contract": goods_c, "goods_shipped": goods_s,
        "freight": freight, "insurance": insurance,
        "contract_amount": qm(goods_c + freight + insurance, currency),
        "invoice_amount": qm(goods_s + freight + insurance, currency),
        "cartons": sum(l["cartons"] for l in lines),
        "net_kg": q2(sum((l["net_kg"] for l in lines), Decimal(0))),
        "gross_kg": q2(sum((l["gross_kg"] for l in lines), Decimal(0))),
        "cbm": q3(sum((l["cbm"] for l in lines), Decimal(0))),
    }
    if role == "buyer":
        seller, buyer = _party_from_partner(partner), _party_from_company(company)
    else:
        seller, buyer = _party_from_company(company), _party_from_partner(partner)
        if not deal.get("port_of_loading") and defaults.get("port_of_loading"):
            deal["port_of_loading"] = defaults["port_of_loading"]
            rep.info("POL_DEFAULT", f"선적항이 비어 있어 기본값 '{defaults['port_of_loading']}'을 썼습니다(data/defaults.json).")

    return {"deal": deal, "company": company, "partner": partner, "defaults": defaults,
            "lines": lines, "totals": totals, "seller": seller, "buyer": buyer,
            "role": role, "currency": currency, "report": rep, "bank_fingerprint": None,
            "inputs_sha256": inputs_fingerprint(deal_path)}


def _sha256_file(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def inputs_fingerprint(deal_path):
    """서류 내용을 결정하는 입력 파일(deal + 기준 데이터)의 해시. 승인본과 같은 입력인지 판단할 때 씁니다."""
    h = hashlib.sha256()
    for p in (Path(deal_path), DATA / "company.json", DATA / "partners.csv", DATA / "products.csv", DATA / "defaults.json"):
        h.update(p.name.encode() + b"\0" + (p.read_bytes() if p.exists() else b"") + b"\0")
    return h.hexdigest()


def _party_from_company(c):
    return {"name": c.get("name_en", ""), "address": c.get("address_en", ""),
            "contact": c.get("contact", ""), "email": c.get("email", ""), "phone": c.get("phone", ""),
            "is_us": True}


def _party_from_partner(p):
    p = p or {}
    return {"name": p.get("name_en", ""), "address": p.get("address_en", ""),
            "contact": p.get("contact", ""), "email": p.get("email", ""), "phone": p.get("phone", ""),
            "is_us": False}


# ---------------------------------------------------------------- bank lock
def bank_fingerprint(bank):
    payload = "|".join(str(bank.get(k) or "").strip() for k in BANK_FIELDS)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def check_bank(bank, rep):
    """계좌가 사람이 마지막으로 확인한 값과 같은지 확인합니다(계좌 변경 사기 방지)."""
    fp = bank_fingerprint(bank)
    lock, broken = None, False
    if BANK_LOCK.exists():
        try:
            lock = _as_dict(load_json(BANK_LOCK))
        except (ValueError, OSError):
            broken = True
            rep.error("BANK_LOCK_BROKEN", "data/bank_lock.json을 읽을 수 없습니다. 사람이 bank-verify를 다시 실행하세요.")
    if not lock:
        if not broken:
            rep.warn("BANK_NOT_LOCKED", "계좌 확인 기록(data/bank_lock.json)이 없습니다. 사람이 계좌를 확인한 뒤 "
                     "터미널에서 직접 `python scripts/tradedocs.py bank-verify`를 실행하세요.")
        ref = parse_date(bank.get("verified_on"))
    else:
        if lock.get("fingerprint") != fp:
            rep.error("BANK_CHANGED", f"data/company.json의 계좌 정보가 마지막 확인({lock.get('verified_at', '?')}) 이후 바뀌었습니다. "
                      "계좌 변경 사기일 수 있습니다. 메일에 적힌 연락처가 아니라 기존에 알던 연락처로 확인한 뒤, "
                      "사람이 bank-verify를 다시 실행하세요.")
        ref = parse_date((lock.get("verified_at") or "")[:10])
    if isinstance(ref, dt.date) and (dt.date.today() - ref).days > BANK_STALE_DAYS:
        rep.warn("BANK_STALE", f"계좌를 마지막으로 확인한 날({ref})이 {BANK_STALE_DAYS}일보다 오래되었습니다. 다시 확인하세요.")
    return fp


# ---------------------------------------------------------------- validation
def validate(ctx):
    rep, deal, lines, t = ctx["report"], ctx["deal"], ctx["lines"], ctx["totals"]
    company, cur = ctx["company"], ctx["currency"]

    # dates
    dates = {}
    for k in ("doc_date", "etd", "eta"):
        v = parse_date(deal.get(k))
        if v == "INVALID":
            rep.error("DATE_FORMAT", f"{k} 날짜 형식이 잘못되었습니다(YYYY-MM-DD): {deal.get(k)}")
        else:
            dates[k] = v
    if dates.get("etd") and dates.get("eta") and dates["eta"] < dates["etd"]:
        rep.error("DATE_ORDER", "ETA가 ETD보다 빠릅니다.")
    if dates.get("etd") and dates.get("doc_date") and dates["etd"] < dates["doc_date"]:
        rep.warn("ETD_PAST", "ETD가 서류 작성일보다 이전입니다.")

    # incoterms
    inc = incoterm_of(deal)
    rule = str(inc.get("rule") or "").upper()
    mode = deal.get("transport_mode") or ctx["defaults"].get("transport_mode", "sea")
    if rule not in INCOTERMS:
        rep.error("INCOTERM_INVALID", f"인코텀즈 '{rule}'는 Incoterms 2020의 11개 규칙이 아닙니다.")
    else:
        group, sea_only, pays_freight, buys_ins = INCOTERMS[rule]
        if not inc.get("place"):
            rep.error("INCOTERM_PLACE", "인코텀즈 지정장소(place)가 없습니다. 예: FOB Busan, Korea")
        # 지정장소가 실제 선적항/도착지와 맞는지 (EXW·FCA는 장소가 다양해서 제외)
        if rule in ("FAS", "FOB"):
            ref_field = "port_of_loading"
        elif rule in ("CFR", "CIF"):
            ref_field = "port_of_discharge"
        elif rule in ("CPT", "CIP") or group == "D":
            ref_field = "final_destination" if deal.get("final_destination") else "port_of_discharge"
        else:
            ref_field = None
        if ref_field and inc.get("place") and deal.get(ref_field) and not same_place(inc["place"], deal[ref_field]):
            rep.warn("INCOTERM_PLACE_MISMATCH", f"{rule} 지정장소 '{inc['place']}'가 {ref_field} '{deal[ref_field]}'와 다릅니다. "
                     f"{rule}의 지정장소는 보통 {'선적항' if ref_field == 'port_of_loading' else '도착항(지)'}입니다.")
        if sea_only and mode != "sea":
            rep.warn("INCOTERM_MODE", f"{rule}은 해상·내수로 운송 전용입니다. {mode} 운송이면 "
                     f"{'FCA' if rule in ('FOB', 'FAS') else ('CPT' if rule == 'CFR' else 'CIP')}를 검토하세요.")
        if sea_only and mode == "sea":
            rep.info("INCOTERM_CONTAINER", f"컨테이너 화물이면 {rule} 대신 "
                     f"{'FCA' if rule in ('FOB', 'FAS') else ('CPT' if rule == 'CFR' else 'CIP')}가 권장됩니다"
                     "(위험 이전 시점 차이). 벌크 화물이면 무시하세요.")
        if ctx["role"] == "seller":
            if not pays_freight and t["freight"] > 0:
                rep.warn("FREIGHT_NOT_EXPECTED", f"{rule} 조건은 주운임을 매수인이 부담하는데 freight {t['freight']}가 송장에 포함되어 있습니다.")
            if pays_freight and t["freight"] == 0:
                rep.warn("FREIGHT_MISSING", f"{rule} 조건은 매도인이 운임을 부담합니다. freight 금액이 0입니다(단가에 포함했는지 확인).")
            if buys_ins and t["insurance"] == 0:
                rep.warn("INSURANCE_MISSING", f"{rule} 조건은 매도인이 보험에 가입해야 합니다. insurance 금액이 0입니다"
                         f"(최소 담보 금액: 송장 금액의 110%{', ICC(C)' if rule == 'CIF' else ', ICC(A)'}).")
            if not buys_ins and t["insurance"] > 0:
                rep.warn("INSURANCE_NOT_EXPECTED", f"{rule} 조건에서는 매도인에게 보험 의무가 없습니다. 보험료를 송장에 넣을지 확인하세요.")
        if rule == "DDP":
            rep.warn("DDP_RISK", "DDP는 매도인이 수입국 관세와 부가세, 수입통관까지 책임집니다. 수입국 세금을 원가에 넣었는지 확인하세요.")
    if t["freight"] < 0 or t["insurance"] < 0:
        rep.error("AMOUNT_NEGATIVE", "freight 또는 insurance가 음수입니다.")

    # parties / bank
    if ctx["role"] == "seller":
        bank = _as_dict(company.get("bank"))
        for k in BANK_REQUIRED:
            if not bank.get(k):
                rep.error("BANK_MISSING", f"data/company.json의 은행 정보 '{k}'가 비어 있습니다.")
        if bank.get("beneficiary") and bank.get("beneficiary") != company.get("name_en"):
            rep.warn("BENEFICIARY_NAME", "예금주(beneficiary)와 자사 영문 상호가 다릅니다.")
        ctx["bank_fingerprint"] = check_bank(bank, rep)
        rep.info("BANK_VERIFY", f"송장에 들어가는 계좌({bank.get('account_no', '')})를 사람이 확인해야 합니다. 마지막 확인일: {bank.get('verified_on', '없음')}")
    if ctx["partner"] and ctx["partner"].get("currency") and ctx["partner"]["currency"] != cur:
        rep.warn("PARTNER_CURRENCY", f"거래처 기본 통화({ctx['partner']['currency']})와 이번 거래 통화({cur})가 다릅니다.")
    partner = ctx["partner"] or {}
    ptype = (partner.get("type") or "").strip().lower()
    expected = {"seller": "buyer", "buyer": "supplier"}.get(ctx["role"])
    if ptype and expected and ptype != expected:
        rep.error("PARTNER_ROLE", f"거래처 {partner.get('partner_id')}는 '{ptype}'인데 our_role={ctx['role']} 거래입니다"
                  f"(거래처는 '{expected}'여야 함). partner_id나 our_role을 확인하세요.")
    if partner.get("default_incoterm") and rule and partner["default_incoterm"].strip().upper() != rule:
        rep.info("PARTNER_TERMS_DIFF", f"거래처 기본 인코텀즈({partner['default_incoterm']})와 이번 거래({rule})가 다릅니다. 합의된 조건인지 확인하세요.")
    if partner.get("default_payment") and deal.get("payment_terms") and \
            partner["default_payment"].strip().upper() != str(deal["payment_terms"]).strip().upper():
        rep.info("PARTNER_TERMS_DIFF", f"거래처 기본 결제조건({partner['default_payment']})과 이번 거래({deal['payment_terms']})가 다릅니다.")
    if not deal.get("port_of_discharge") and ctx["role"] == "seller":
        rep.warn("POD_MISSING", "도착항(port_of_discharge)이 비어 있습니다.")

    # lines
    for l in lines:
        tag = f"{l['no']}번 {l['sku']}"
        if l["qty"] <= 0:
            rep.error("QTY_INVALID", f"{tag}: 계약 수량(qty)이 {l['qty']}입니다. 0보다 커야 합니다.")
        if l["shipped_qty"] < 0:
            rep.error("QTY_INVALID", f"{tag}: 선적 수량(shipped_qty)이 음수({l['shipped_qty']})입니다.")
        hs = l["hs_code"].replace(".", "")
        if not hs.isdigit() or len(hs) not in (6, 8, 10):
            rep.error("HS_FORMAT", f"{tag}: HS코드 '{l['hs_code']}' 형식이 잘못되었습니다(6/8/10자리 숫자).")
        else:
            rep.info("HS_CONFIRM", f"{tag}: HS {l['hs_code']}는 후보값입니다. 관세사 확인 필요.")
        if ctx["role"] == "buyer" and l["origin_source"] == "master":
            rep.error("ORIGIN_REQUIRED", f"{tag}: 수입 거래인데 원산지가 품목 마스터 값({l['master_origin']})으로 들어갑니다. "
                      "공급처 서류 기준 원산지를 deal의 items[].origin에 입력하세요(최종 판정은 관세사).")
        elif l["origin_source"] == "deal" and l["origin"] != l["master_origin"]:
            rep.info("ORIGIN_OVERRIDE", f"{tag}: 원산지를 deal 값 '{l['origin']}'로 씁니다(마스터: {l['master_origin']}).")
        same_cur = not l["product_currency"] or l["product_currency"] == cur
        if not same_cur:
            if l["price_from_master"]:
                rep.error("LINE_CURRENCY", f"{tag}: unit_price가 없어 마스터 단가({l['master_price']} {l['product_currency']})를 "
                          f"{cur} 거래에 환산 없이 쓰게 됩니다. deal에 {cur} 단가를 입력하세요.")
            else:
                rep.info("LINE_CURRENCY", f"{tag}: 품목 마스터 통화({l['product_currency']})와 거래 통화({cur})가 달라 deal 단가를 씁니다.")
        if ctx["role"] == "buyer" and l["price_from_master"]:
            rep.error("PRICE_REQUIRED", f"{tag}: 수입 거래인데 unit_price가 없습니다. products.csv 단가는 우리 판매가라서 구매가로 쓰지 않습니다. "
                      "공급처 견적·PI 단가를 입력하세요.")
        if l["unit_price"] <= 0:
            rep.error("PRICE_ZERO", f"{tag}: 단가가 0 이하입니다.")
        elif ctx["role"] == "seller" and same_cur and l["master_price"] and abs(l["unit_price"] - l["master_price"]) / l["master_price"] > D("0.2"):
            rep.warn("PRICE_DEVIATION", f"{tag}: 단가 {l['unit_price']}가 마스터 단가 {l['master_price']}와 20% 넘게 차이 납니다.")
        if l["qty_per_carton"] <= 0:
            rep.error("CARTON_SPEC", f"{tag}: qty_per_carton이 없거나 정수가 아니어서 박스 수를 계산할 수 없습니다.")
        elif l["shipped_qty"] % l["qty_per_carton"] != 0:
            rest = l["shipped_qty"] % l["qty_per_carton"]
            rep.warn("PARTIAL_CARTON", f"{tag}: 수량 {l['shipped_qty']}가 박스당 {l['qty_per_carton']}의 배수가 아닙니다"
                     f"(마지막 박스 {rest}{l['unit']}). PL에는 마지막 박스 총중량을 "
                     f"{q2(l['last_carton_gross']) if l['last_carton_gross'] is not None else '?'}kg(포장재+내용물 추정)으로 넣었습니다. 실측해 확인하세요.")
        if l["net_per_ctn"] >= l["gross_per_ctn"]:
            rep.error("WEIGHT_LOGIC", f"{tag}: 박스당 순중량({q2(l['net_per_ctn'])}kg)이 총중량({l['gross_per_ctn']}kg) 이상입니다. 품목 마스터를 확인하세요.")
        if l["shipped_qty"] != l["qty"]:
            if l["change_reason"]:
                rep.info("QTY_CHANGED", f"{tag}: 계약 {l['qty']} → 선적 {l['shipped_qty']} (사유: {l['change_reason']})")
            else:
                rep.error("QTY_CHANGED_NO_REASON", f"{tag}: 계약 수량 {l['qty']}과 선적 수량 {l['shipped_qty']}이 다른데 change_reason이 없습니다.")
        if l["shipped_qty"] > l["qty"]:
            rep.warn("OVER_SHIPMENT", f"{tag}: 계약 수량보다 많이 선적합니다. 바이어 동의를 받았는지 확인하세요.")
    if lines and all(l["shipped_qty"] <= 0 for l in lines):
        rep.error("NOTHING_SHIPPED", "모든 품목의 선적 수량이 0입니다. 금액 0인 서류는 만들지 않습니다.")

    # PI vs CI
    if t["invoice_amount"] != t["contract_amount"]:
        diff = t["invoice_amount"] - t["contract_amount"]
        rep.info("PI_CI_DIFF", f"PI 금액 {t['contract_amount']} / CI 금액 {t['invoice_amount']} (차이 {diff}). 잔금을 청구할 때 CI 금액을 기준으로 하세요.")

    # L/C
    pay = str(deal.get("payment_terms") or "").upper()
    lc = lc_of(deal)
    if LC_PAYMENT_RE.search(pay) and not lc:
        rep.warn("LC_MISSING", "결제조건이 L/C인데 lc 정보가 없습니다. 신용장 사본을 받아 deal에 입력하세요.")
    if lc and not LC_PAYMENT_RE.search(pay):
        rep.warn("LC_TERMS_MISMATCH", f"lc 정보가 있는데 결제조건('{deal.get('payment_terms')}')에 L/C가 없습니다. 결제조건을 확인하세요.")
    if lc:
        lc_cur = str(lc.get("currency") or "").strip().upper()
        if lc_cur and lc_cur != cur:
            rep.error("LC_CURRENCY", f"L/C 통화({lc_cur})와 거래 통화({cur})가 다릅니다.")
        elif not lc_cur:
            rep.info("LC_CURRENCY", f"lc.currency가 없어 거래 통화({cur})와 같다고 보고 검사했습니다. 신용장 32B 통화를 입력하세요.")
        try:
            tol = num(lc.get("tolerance_pct"), Decimal(0)) / 100
            lc_amount = num(lc.get("amount"))
        except ValueError as e:
            rep.error("NUM_FORMAT", f"lc.amount/tolerance_pct가 숫자가 아닙니다 {e}.")
            tol, lc_amount = Decimal(0), None
        if lc_amount:
            upper = qm(lc_amount * (1 + tol), cur)
            if t["invoice_amount"] > upper:
                rep.error("LC_AMOUNT", f"CI 금액 {t['invoice_amount']}가 L/C 한도 {upper}(허용오차 포함)를 넘습니다.")
        lsd, exp = parse_date(lc.get("latest_shipment_date")), parse_date(lc.get("expiry_date"))
        if dates.get("etd") and isinstance(lsd, dt.date) and dates["etd"] > lsd:
            rep.error("LC_SHIPMENT_DATE", f"ETD {dates['etd']}가 L/C 최종선적일 {lsd}보다 늦습니다. 신용장 조건 변경(Amendment)이 필요합니다.")
        if isinstance(lsd, dt.date) and isinstance(exp, dt.date) and exp < lsd:
            rep.error("LC_EXPIRY", "L/C 유효기일이 최종선적일보다 빠릅니다.")
        if isinstance(exp, dt.date) and isinstance(dates.get("doc_date"), dt.date) and exp < dates["doc_date"]:
            rep.error("LC_EXPIRED", f"L/C 유효기일({exp})이 서류 작성일({dates['doc_date']})보다 이전입니다. 만료된 신용장입니다.")
        if isinstance(exp, dt.date) and dates.get("etd"):
            left = (exp - dates["etd"]).days
            try:
                pres_days = int(lc.get("presentation_days") or 21)
            except (TypeError, ValueError):
                pres_days = 21
            if left < 0:
                rep.error("LC_EXPIRY_BEFORE_ETD", f"L/C 유효기일({exp})이 ETD({dates['etd']})보다 빠릅니다. 선적 후 서류를 제시할 수 없습니다.")
            elif left < pres_days:
                rep.warn("LC_PRESENTATION", f"선적 후 L/C 만기까지 {left}일 남습니다. 서류 제시 기간({pres_days}일)보다 짧으니 일정을 확인하세요.")
        if not lc.get("bl_consignee"):
            rep.warn("LC_BL_CONSIGNEE", "L/C 거래인데 lc.bl_consignee가 없습니다. 신용장 46A의 B/L 수하인 문구(예: TO ORDER OF 개설은행)를 "
                     "그대로 입력하세요. 없으면 S/I Consignee에 바이어가 들어갑니다.")
        if lc.get("partial_shipment") is False and any(l["shipped_qty"] < l["qty"] for l in lines):
            rep.error("LC_PARTIAL", "L/C가 분할선적을 금지하는데 일부 품목이 계약 수량보다 적게 선적됩니다.")
        if lc.get("goods_description"):
            rep.info("LC_DESCRIPTION", f"CI 품명은 L/C 문구와 일치해야 합니다: \"{lc['goods_description']}\" (CI 하단에 자동 기재됨)")
    return rep


def write_report(ctx, out_dir):
    rep = ctx["report"]
    out_dir.mkdir(parents=True, exist_ok=True)
    data = {"deal_id": ctx["deal"].get("deal_id"), "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
            "errors": rep.count("ERROR"), "warnings": rep.count("WARNING"), "items": rep.items}
    (out_dir / "validation.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [f"# 검증 보고서: {data['deal_id']}", "",
          f"- 검사 시각: {data['checked_at']}",
          f"- ERROR {data['errors']}건 / WARNING {data['warnings']}건", ""]
    for level in ("ERROR", "WARNING", "INFO"):
        rows = [i for i in rep.items if i["level"] == level]
        if rows:
            md += [f"## {level}", ""] + [f"- `{i['code']}` {i['message']}" for i in rows] + [""]
    (out_dir / "validation.md").write_text("\n".join(md), encoding="utf-8")
    return data


# ---------------------------------------------------------------- excel
def _xl():
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    return Workbook, Alignment, Border, Font, PatternFill, Side


class Sheet:
    def __init__(self, title, ncols):
        Workbook, Alignment, Border, Font, PatternFill, Side = _xl()
        self.wb = Workbook()
        self.ws = self.wb.active
        self.ws.title = title[:31]
        self.n = ncols
        self.r = 1
        self.A, self.F, self.P = Alignment, Font, PatternFill
        thin = Side(style="thin", color="999999")
        self.border = Border(left=thin, right=thin, top=thin, bottom=thin)
        self.ws.sheet_view.showGridLines = False
        self.ws.page_setup.orientation = "portrait"
        self.ws.page_setup.paperSize = self.ws.PAPERSIZE_A4
        self.ws.page_setup.fitToWidth = 1
        self.ws.page_setup.fitToHeight = 0
        self.ws.sheet_properties.pageSetUpPr.fitToPage = True
        self.ws.print_options.horizontalCentered = True
        self.ws.page_margins.left = self.ws.page_margins.right = 0.4
        self._w = []

    def widths(self, ws_):
        from openpyxl.utils import get_column_letter
        self._w = list(ws_)
        for i, w in enumerate(ws_, 1):
            self.ws.column_dimensions[get_column_letter(i)].width = w

    def _cap(self, c1, c2):
        """c1~c2열(병합 범위)에 한 줄로 들어가는 대략적인 글자 수"""
        return max(8, int(sum(self._w[c1 - 1:c2]) * 1.1)) if self._w else 80

    def _fit(self, row, text, cap):
        """병합 셀은 엑셀·LibreOffice가 행 높이를 자동으로 맞추지 않으므로 줄 수를 계산해 높이를 정합니다."""
        if text in (None, ""):
            return
        n = sum(max(1, math.ceil(len(seg) / cap)) for seg in str(text).splitlines() or [""])
        if n > 1:
            cur = self.ws.row_dimensions[row].height or 15
            self.ws.row_dimensions[row].height = max(cur, 15 * n)

    def title(self, text):
        self.ws.merge_cells(start_row=self.r, start_column=1, end_row=self.r, end_column=self.n)
        c = self.ws.cell(self.r, 1, text)
        c.font = self.F(size=16, bold=True)
        c.alignment = self.A(horizontal="center")
        self.ws.row_dimensions[self.r].height = 28
        self.r += 2

    def block(self, left_label, left_lines, right_pairs):
        """왼쪽: 당사자 블록, 오른쪽: 번호/날짜 등 key-value"""
        half = self.n // 2
        start = self.r
        self.ws.cell(self.r, 1, left_label).font = self.F(bold=True, size=9, color="555555")
        rr = self.r + 1
        for line in left_lines:
            if line:
                self.ws.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=half)
                c = self.ws.cell(rr, 1, line)
                c.alignment = self.A(wrap_text=True, vertical="top")
                self._fit(rr, line, self._cap(1, half))
                rr += 1
        kr = start
        for k, v in right_pairs:
            self.ws.cell(kr, half + 1, k).font = self.F(bold=True, size=9, color="555555")
            self.ws.merge_cells(start_row=kr, start_column=half + 2, end_row=kr, end_column=self.n)
            c = self.ws.cell(kr, half + 2, v)
            c.alignment = self.A(wrap_text=True, vertical="top")
            self._fit(kr, v, self._cap(half + 2, self.n))
            kr += 1
        self.r = max(rr, kr) + 1

    def kv(self, pairs, label_span=2):
        for k, v in pairs:
            self.ws.merge_cells(start_row=self.r, start_column=1, end_row=self.r, end_column=label_span)
            self.ws.cell(self.r, 1, k).font = self.F(bold=True)
            self.ws.merge_cells(start_row=self.r, start_column=label_span + 1, end_row=self.r, end_column=self.n)
            c = self.ws.cell(self.r, label_span + 1, v)
            c.alignment = self.A(wrap_text=True, vertical="top")
            for col in range(1, self.n + 1):
                self.ws.cell(self.r, col).border = self.border
            self._fit(self.r, v, self._cap(label_span + 1, self.n))
            self.r += 1
        self.r += 1

    def table(self, headers, rows, num_fmt=None, total_row=None):
        num_fmt = num_fmt or {}
        fill = self.P("solid", fgColor="DDE5F0")
        for j, h in enumerate(headers, 1):
            c = self.ws.cell(self.r, j, h)
            c.font = self.F(bold=True)
            c.fill = fill
            c.border = self.border
            c.alignment = self.A(horizontal="center", vertical="center", wrap_text=True)
        self.ws.row_dimensions[self.r].height = 30
        self.r += 1
        for row in rows:
            for j, v in enumerate(row, 1):
                c = self.ws.cell(self.r, j, float(v) if isinstance(v, Decimal) else v)
                c.border = self.border
                c.alignment = self.A(vertical="top", wrap_text=True,
                                     horizontal="right" if isinstance(v, (int, float, Decimal)) else "left")
                if j in num_fmt:
                    c.number_format = num_fmt[j]
                if isinstance(v, str):
                    self._fit(self.r, v, self._cap(j, j))
            self.r += 1
        if total_row:
            for j, v in enumerate(total_row, 1):
                c = self.ws.cell(self.r, j, float(v) if isinstance(v, Decimal) else v)
                c.font = self.F(bold=True)
                c.border = self.border
                c.alignment = self.A(horizontal="right" if isinstance(v, (int, float, Decimal)) else "left")
                if j in num_fmt:
                    c.number_format = num_fmt[j]
            self.r += 1
        self.r += 1

    def text(self, s, bold=False, size=10):
        self.ws.merge_cells(start_row=self.r, start_column=1, end_row=self.r, end_column=self.n)
        c = self.ws.cell(self.r, 1, s)
        c.font = self.F(bold=bold, size=size)
        c.alignment = self.A(wrap_text=True, vertical="top")
        self._fit(self.r, s, self._cap(1, self.n))
        self.r += 1

    def signature(self, name, signatory):
        self.r += 1
        half = self.n // 2
        self.ws.cell(self.r, half + 1, f"For and on behalf of {name}").font = self.F(bold=True)
        self.r += 3
        self.ws.merge_cells(start_row=self.r, start_column=half + 1, end_row=self.r, end_column=self.n)
        self.ws.cell(self.r, half + 1, "_______________________________")
        self.r += 1
        self.ws.cell(self.r, half + 1, signatory or "Authorized Signature")

    def save(self, path):
        self.wb.save(path)


def _party_lines(p):
    return [p["name"], p["address"],
            " / ".join(x for x in (f"Attn: {p['contact']}" if p["contact"] else "", p["email"], p["phone"]) if x)]


def _term_str(ctx):
    inc = incoterm_of(ctx["deal"])
    return f"{inc.get('rule', '')} {inc.get('place', '')} ({inc.get('version', 'Incoterms 2020')})".strip()


def _bank_lines(company):
    b = _as_dict(company.get("bank"))
    return [("Bank", f"{b.get('bank_name', '')} {b.get('branch', '')}".strip()),
            ("SWIFT", b.get("swift", "")), ("Account No.", b.get("account_no", "")),
            ("Beneficiary", b.get("beneficiary", ""))]


def doc_no(ctx, kind):
    """서류 번호: {문서종류}-{deal_id}. 승인(발송)된 뒤 다시 만들면 -R1, -R2 ...가 붙습니다."""
    base = f"{kind}-{ctx['deal']['deal_id']}"
    rev = (ctx.get("revisions") or {}).get(kind, 0)
    return f"{base}-R{rev}" if rev else base


def _sign(s, ctx, issuer):
    """서류 발행자 명의로 서명란을 만듭니다. 서명권자는 발행자가 자사일 때만 씁니다."""
    s.signature(issuer["name"], ctx["company"].get("signatory") if issuer.get("is_us") else None)


def doc_invoice(ctx, kind, path):
    """PI / CI / PO 공통 레이아웃"""
    deal, t, cur = ctx["deal"], ctx["totals"], ctx["currency"]
    lc = lc_of(deal)
    shipped = kind == "CI"
    mfmt = money_fmt(cur)
    s = Sheet(kind, 7)
    s.widths([6, 40, 14, 11, 8, 13, 16])
    s.title(DOC_TITLES[kind])
    number = doc_no(ctx, kind)
    right =[(f"{kind} No.", number), ("Date", deal.get("doc_date", "")), ("Terms", _term_str(ctx)),
             ("Payment", deal.get("payment_terms", ""))]
    if kind == "PI":
        validity = deal.get("pi_validity_days") or ctx["defaults"].get("pi_validity_days", 30)
        d0 = parse_date(deal.get("doc_date"))
        if isinstance(d0, dt.date):
            right.append(("Valid until", (d0 + dt.timedelta(days=int(validity))).isoformat()))
    if kind == "CI" and lc:
        right.append(("L/C No.", lc.get("lc_no", "")))
    s.block("SELLER / SHIPPER" if kind != "PO" else "SUPPLIER (SELLER)",
            _party_lines(ctx["seller"]), right)
    buyer_label = "BUYER" if kind == "PO" else ("BUYER (APPLICANT)" if lc else "BUYER / CONSIGNEE")
    s.block(buyer_label, _party_lines(ctx["buyer"]), [])
    if kind in ("CI", "PO"):
        ship = [("Port of Loading", deal.get("port_of_loading", "")),
                ("Port of Discharge", deal.get("port_of_discharge", ""))]
        if kind == "CI":
            ship += [("Vessel / Voyage", deal.get("vessel_voyage", "")), ("ETD", deal.get("etd", "")),
                     ("Notify Party", deal.get("notify_party") or "Same as consignee"),
                     ("Shipping Mark", deal.get("shipping_mark", ""))]
        else:
            ship += [("Requested ETD", deal.get("etd", ""))]
        s.kv(ship)
    headers = ["No.", "Description of Goods", "HS Code", "Quantity", "Unit", f"Unit Price\n({cur})", f"Amount\n({cur})"]
    rows = []
    for l in ctx["lines"]:
        desc = f"{l['description']}\nModel: {l['model']}"
        if kind in ("CI", "PO"):
            desc += f"\nOrigin: {l['origin']}"
        rows.append([l["no"], desc, l["hs_code"], l["shipped_qty"] if shipped else l["qty"], l["unit"],
                     l["unit_price"], l["amount_shipped"] if shipped else l["amount_contract"]])
    fmt = {4: "#,##0", 6: "#,##0.00", 7: mfmt}
    goods = t["goods_shipped"] if shipped else t["goods_contract"]
    s.table(headers, rows, fmt)
    total = t["invoice_amount"] if shipped else t["contract_amount"]
    sub = [("Goods Total", goods)]
    if t["freight"]:
        sub.append(("Freight", t["freight"]))
    if t["insurance"]:
        sub.append(("Insurance", t["insurance"]))
    sub.append((f"TOTAL ({_term_str(ctx).split(' (')[0]})", total))
    for label, val in sub:
        s.ws.merge_cells(start_row=s.r, start_column=4, end_row=s.r, end_column=6)
        s.ws.cell(s.r, 4, label).font = s.F(bold=label.startswith("TOTAL"))
        c = s.ws.cell(s.r, 7, float(val))
        c.number_format = mfmt
        c.font = s.F(bold=label.startswith("TOTAL"))
        c.border = s.border
        s.r += 1
    s.r += 1
    s.text(amount_words(total, cur), bold=True)
    s.r += 1
    if kind == "CI" and lc.get("goods_description"):
        s.text(f"Description as per L/C: {lc['goods_description']}")
        s.r += 1
    if kind in ("PI", "CI") and ctx["role"] == "seller":
        s.text("BANK INFORMATION", bold=True)
        s.kv(_bank_lines(ctx["company"]))
    if kind == "PO":
        s.text("Please confirm this order by signing and returning a copy, together with your Proforma Invoice.")
    # remarks는 내부 메모, doc_remarks는 서류에 인쇄되는 비고입니다.
    if deal.get("doc_remarks"):
        s.text(f"Remarks: {deal['doc_remarks']}")
        s.r += 1
    if kind == "CI" and ctx["seller"].get("is_us"):
        origins = sorted({l["origin"] for l in ctx["lines"] if l["origin"]})
        if len(origins) == 1:
            s.text(f"We hereby certify that the goods described above are of {origins[0]} origin "
                   "and that this invoice is true and correct.")
        else:
            s.text("We hereby certify that the country of origin of each item is as stated above "
                   "and that this invoice is true and correct.")
    _sign(s, ctx, ctx["buyer"] if kind == "PO" else ctx["seller"])
    s.save(path)
    return number


def doc_pl(ctx, path):
    deal, t = ctx["deal"], ctx["totals"]
    s = Sheet("PL", 8)
    s.widths([6, 36, 13, 9, 11, 13, 13, 10])
    s.title(DOC_TITLES["PL"])
    number = doc_no(ctx, "PL")
    s.block("SELLER / SHIPPER", _party_lines(ctx["seller"]),
            [("PL No.", number), ("Invoice No.", doc_no(ctx, "CI")), ("Date", deal.get("doc_date", "")),
             ("Vessel", deal.get("vessel_voyage", "")), ("ETD", deal.get("etd", ""))])
    s.block("BUYER / CONSIGNEE", _party_lines(ctx["buyer"]), [])
    s.kv([("Port of Loading", deal.get("port_of_loading", "")),
          ("Port of Discharge", deal.get("port_of_discharge", "")),
          ("Shipping Mark", deal.get("shipping_mark", ""))])
    headers = ["No.", "Description of Goods", "Carton No.", "Cartons", "Quantity", "Net Weight\n(kg)", "Gross Weight\n(kg)", "CBM"]
    rows = []
    for l in ctx["lines"]:
        cno = f"{l['carton_from']}-{l['carton_to']}" if l["cartons"] else ""
        rows.append([l["no"], f"{l['description']}\n{l['qty_per_carton']} {l['unit']}/CTN", cno, l["cartons"],
                     f"{int(l['shipped_qty']):,} {l['unit']}", l["net_kg"], l["gross_kg"], l["cbm"]])
    fmt = {4: "#,##0", 6: "#,##0.00", 7: "#,##0.00", 8: "0.000"}
    s.table(headers, rows, fmt, ["", "TOTAL", "", t["cartons"], "", t["net_kg"], t["gross_kg"], t["cbm"]])
    s.text(f"TOTAL: {int_words(t['cartons'])} ({t['cartons']}) CARTONS ONLY.", bold=True)
    _sign(s, ctx, ctx["seller"])
    s.save(path)
    return number


def doc_si(ctx, path):
    deal, t = ctx["deal"], ctx["totals"]
    lc = lc_of(deal)
    s = Sheet("SI", 6)
    s.widths([14, 14, 16, 16, 16, 16])
    s.title(DOC_TITLES["SI"])
    number = doc_no(ctx, "SI")
    rule = str(incoterm_of(deal).get("rule") or "").upper()
    prepaid = INCOTERMS.get(rule, ("", False, False, False))[2]
    goods = ", ".join(sorted({l["description"] for l in ctx["lines"]}))
    hs = ", ".join(sorted({l["hs_code"][:6] for l in ctx["lines"]}))
    buyer_text = "\n".join(x for x in _party_lines(ctx["buyer"]) if x)
    # L/C 거래는 B/L 수하인을 신용장 46A 문구 그대로 쓰고, 통지처에 개설의뢰인(바이어)을 둡니다.
    if lc.get("bl_consignee"):
        consignee, notify = lc["bl_consignee"], deal.get("notify_party") or buyer_text
    else:
        consignee, notify = buyer_text, deal.get("notify_party") or "Same as consignee"
    s.kv([("SI No.", number), ("Date", deal.get("doc_date", "")),
          ("Shipper", "\n".join(x for x in _party_lines(ctx["seller"]) if x)),
          ("Consignee", consignee),
          ("Notify Party", notify),
          ("Port of Loading", deal.get("port_of_loading", "")),
          ("Port of Discharge", deal.get("port_of_discharge", "")),
          ("Final Destination", deal.get("final_destination") or deal.get("port_of_discharge", "")),
          ("Vessel / Voyage", deal.get("vessel_voyage", "")), ("ETD", deal.get("etd", "")),
          ("Terms", _term_str(ctx)),
          ("Freight", "FREIGHT PREPAID" if prepaid else "FREIGHT COLLECT"),
          ("Description", goods), ("HS Code (6)", hs),
          ("Packages", f"{t['cartons']} CARTONS"), ("Net Weight", f"{t['net_kg']:,} KGS"),
          ("Gross Weight", f"{t['gross_kg']:,} KGS"), ("Measurement", f"{t['cbm']} CBM"),
          ("Shipping Mark", deal.get("shipping_mark", "")),
          ("B/L Type", "Original B/L 3/3" if lc else "Surrendered B/L (to be confirmed)"),
          ("Documents Required", "B/L draft for confirmation before issuance"),
          ("Remarks", f"L/C No. {lc.get('lc_no')}" if lc else "")])
    s.save(path)
    return number


def find_soffice():
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if exe:
        return exe
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            cand = Path(base) / "LibreOffice" / "program" / "soffice.exe"
            if cand.exists():
                return str(cand)
    return None


def to_pdf(xlsx, out_dir):
    """성공하면 (pdf 경로, None), 실패하면 (None, 이유)."""
    pdf = out_dir / (xlsx.stem + ".pdf")
    # 이전 실행에서 남은 PDF(예전 금액)가 새 xlsx와 섞이지 않도록 먼저 지웁니다.
    try:
        pdf.unlink(missing_ok=True)
    except OSError as e:
        return None, f"이전 PDF를 지울 수 없습니다(다른 프로그램에서 열려 있는지 확인): {e}"
    exe = find_soffice()
    if not exe:
        return None, "LibreOffice(soffice)를 찾지 못했습니다"
    try:
        subprocess.run([exe, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(xlsx)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    except subprocess.TimeoutExpired:
        return None, "변환 시간 초과(120초)"
    except OSError as e:
        return None, f"LibreOffice 실행 실패: {e}"
    if pdf.exists() and pdf.stat().st_size > 0:
        return pdf, None
    return None, "변환 후 PDF 파일이 만들어지지 않았습니다"


# ---------------------------------------------------------------- manifest
def _num(x):
    return float(x) if isinstance(x, Decimal) else x


def build_manifest(ctx, docs, vdata):
    deal, t = ctx["deal"], ctx["totals"]
    lc = lc_of(deal)
    human = ["최종 금액·통화·은행 계좌 확인", "HS코드·원산지 확정(관세사)"]
    if lc:
        human.append("신용장 원문과 CI·PL·B/L 문구 대조")
    if vdata["warnings"]:
        human.append(f"검증 WARNING {vdata['warnings']}건 검토 (validation.md)")
    lines = [{"no": l["no"], "sku": l["sku"], "description": l["description"], "hs_code": l["hs_code"],
              "origin": l["origin"], "unit": l["unit"], "contract_qty": _num(l["qty"]),
              "shipped_qty": _num(l["shipped_qty"]), "unit_price": _num(l["unit_price"]),
              "amount": _num(l["amount_shipped"]), "cartons": l["cartons"],
              "net_kg": _num(l["net_kg"]), "gross_kg": _num(l["gross_kg"]), "cbm": _num(l["cbm"])}
             for l in ctx["lines"]]
    shipment = {k: deal.get(k, "") for k in ("transport_mode", "port_of_loading", "port_of_discharge",
                                               "vessel_voyage", "etd", "eta", "shipping_mark")}
    totals = {"currency": t["currency"], "contract_amount": _num(t["contract_amount"]),
              "invoice_amount": _num(t["invoice_amount"]), "goods_amount": _num(t["goods_shipped"]),
              "freight": _num(t["freight"]), "insurance": _num(t["insurance"]), "cartons": t["cartons"],
              "net_weight_kg": _num(t["net_kg"]), "gross_weight_kg": _num(t["gross_kg"]), "cbm": _num(t["cbm"])}
    by_type = {d["type"]: d for d in docs}
    finance = {
        "receivable" if ctx["role"] == "seller" else "payable": _num(
            t["invoice_amount"] if "CI" in by_type else t["contract_amount"]),
        "currency": t["currency"], "payment_terms": deal.get("payment_terms"),
        "invoice_no": (by_type.get("CI") or by_type.get("PI") or by_type.get("PO") or {}).get("number"),
        "doc_date": deal.get("doc_date"), "etd": deal.get("etd"),
        "lc_no": lc.get("lc_no"),
    }
    if ctx.get("bank_fingerprint"):
        finance["bank_fingerprint"] = ctx["bank_fingerprint"]
    return {
        "event": "docs_generated",
        "deal_id": deal["deal_id"],
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "status": "needs_human_review",
        "our_role": ctx["role"],
        "partner": {"partner_id": deal.get("partner_id"), "name": (ctx["partner"] or {}).get("name_en"),
                    "country": (ctx["partner"] or {}).get("country")},
        "incoterm": deal.get("incoterm"),
        "payment_terms": deal.get("payment_terms"),
        "shipment": shipment,
        "documents": docs,
        "totals": totals,
        "lines": lines,
        "validation": {"errors": vdata["errors"], "warnings": vdata["warnings"], "report": "validation.md"},
        "human_review": human,
        "handoff": {
            "customs_logistics_agent": {
                "documents": [by_type[k]["file"] for k in ("CI", "PL", "SI") if k in by_type],
                "hs_codes": sorted({l["hs_code"] for l in ctx["lines"]}),
                "origin": sorted({l["origin"] for l in ctx["lines"]}),
                "packages": t["cartons"], "gross_weight_kg": _num(t["gross_kg"]), "cbm": _num(t["cbm"]),
                "invoice_value": _num(t["invoice_amount"]), "currency": t["currency"],
                "incoterm": deal.get("incoterm"), "shipment": shipment,
            },
            "finance_agent": finance,
        },
    }


# ---------------------------------------------------------------- commands
def _out_dir(ctx):
    """deal_id가 올바를 때만 output 폴더를 돌려줍니다(경로 조작 방지)."""
    did = ctx["deal"].get("deal_id")
    if valid_deal_id(did):
        return OUTPUT / did
    print(f"deal_id({did!r})가 없거나 형식이 잘못되어 보고서와 서류를 저장하지 않았습니다.")
    for i in ctx["report"].items:
        if i["level"] == "ERROR":
            print(f"  ERROR {i['code']}: {i['message']}")
    return None


def cmd_validate(args):
    ctx = build_context(args.deal)
    validate(ctx)
    out = _out_dir(ctx)
    if out is None:
        return 1
    v = write_report(ctx, out)
    print(f"[{v['deal_id']}] ERROR {v['errors']} / WARNING {v['warnings']} -> {out / 'validation.md'}")
    for i in ctx["report"].items:
        if i["level"] != "INFO":
            print(f"  {i['level']:7} {i['code']}: {i['message']}")
    return 1 if v["errors"] else 0


DOC_ORDER = ("PI", "CI", "PL", "SI", "PO")


def content_hash(path):
    """xlsx의 '내용'(셀 값·병합) 해시. 파일 바이트는 저장 시각 때문에 매번 달라서 내용 비교에는 이것을 씁니다."""
    from openpyxl import load_workbook
    wb = load_workbook(path)
    h = hashlib.sha256()
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            h.update(repr(row).encode("utf-8"))
        h.update(repr(sorted(str(r) for r in ws.merged_cells.ranges)).encode("utf-8"))
    return h.hexdigest()


def read_approvals(out):
    p = out / "approvals.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def last_approved_docs(approvals):
    """문서 종류별로 가장 최근에 승인된 항목"""
    latest = {}
    for a in approvals:
        for d in a.get("documents", []):
            latest[d["type"]] = d
    return latest


def _build_doc(ctx, k, path):
    if k == "PL":
        return doc_pl(ctx, path)
    if k == "SI":
        return doc_si(ctx, path)
    return doc_invoice(ctx, k, path)


def cmd_generate(args):
    ctx = build_context(args.deal)
    validate(ctx)
    deal = ctx["deal"]
    out = _out_dir(ctx)
    if out is None:
        return 1
    v = write_report(ctx, out)
    # 형식 자체가 틀린 deal은 --force로도 만들지 않습니다.
    fatal = {"ROLE_INVALID", "INCOTERM_TYPE", "LC_TYPE", "ITEMS_TYPE", "NUM_FORMAT", "BANK_CHANGED", "BANK_LOCK_BROKEN",
             "PARTNER_ROLE"}
    fatal_hit = sorted({i["code"] for i in ctx["report"].items if i["code"] in fatal})
    if v["errors"] and (not args.force or fatal_hit):
        why = f" (--force로도 생성 불가: {', '.join(fatal_hit)})" if args.force and fatal_hit else " (강제 생성: --force)"
        print(f"[{v['deal_id']}] ERROR {v['errors']}건 때문에 서류를 생성하지 않았습니다. {out / 'validation.md'} 확인{why}")
        for i in ctx["report"].items:
            if i["level"] == "ERROR":
                print(f"  ERROR {i['code']}: {i['message']}")
        manifest = {"event": "docs_generated", "deal_id": deal.get("deal_id"), "status": "blocked",
                    "documents": [], "totals": {}, "validation": {"errors": v["errors"], "warnings": v["warnings"],
                                                                 "report": "validation.md"}}
        (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1
    allowed = DOCS_BY_ROLE.get(ctx["role"], ())
    if args.docs:
        kinds = [k.strip().upper() for k in args.docs.split(",") if k.strip()]
    else:
        kinds = list(ctx["defaults"].get("documents_by_role", {}).get(ctx["role"]) or allowed)
    # CI 번호를 PL이 참조하므로 정해진 순서로 만듭니다.
    kinds = sorted(dict.fromkeys(kinds), key=lambda k: DOC_ORDER.index(k) if k in DOC_ORDER else 99)

    approvals = read_approvals(out)
    approved = last_approved_docs(approvals)
    # 승인 이후 입력이 전혀 바뀌지 않았으면 승인본을 그대로 둡니다(같은 서류를 다시 만들 이유가 없음).
    if approvals and approvals[-1].get("inputs_sha256") == ctx["inputs_sha256"] and \
            all(k in approved for k in kinds if k in allowed) and not v["errors"]:
        print(f"[{deal['deal_id']}] 승인된 서류와 입력이 같아 다시 만들지 않았습니다(manifest 유지). "
              "개정이 필요하면 deal이나 기준 데이터를 수정하세요.")
        return 0
    ctx["revisions"] = {k: d.get("revision", 0) for k, d in approved.items()}

    docs, pdf_failures, revised = [], [], []
    for k in kinds:
        if k not in DOC_TITLES:
            print(f"  알 수 없는 문서 종류: {k}")
            continue
        if k not in allowed:
            print(f"  건너뜀: {k}는 our_role={ctx['role']} 거래에서 우리가 발행하는 서류가 아닙니다"
                  f"(발행 가능: {', '.join(allowed)}). 공급처 서류는 만들지 않고 받은 서류를 대조합니다.")
            continue
        prev = approved.get(k)
        try:
            if prev:
                # 승인본과 같은 번호로 만들어 보고, 내용이 같으면 승인본을 그대로 씁니다.
                tmp = out / f".tmp-{k}.xlsx"
                _build_doc(ctx, k, tmp)
                same = content_hash(tmp) == prev.get("content_sha256")
                prev_file = out / prev["file"]
                if same and prev_file.exists() and _sha256_file(prev_file) == prev.get("sha256"):
                    tmp.unlink()
                    entry = {key: prev[key] for key in ("type", "number", "file", "sha256", "content_sha256", "pdf", "pdf_sha256", "revision")
                             if key in prev}
                    entry["unchanged_since_approval"] = True
                    docs.append(entry)
                    print(f"  유지: {prev['file']} (승인본과 내용 같음)")
                    continue
                tmp.unlink()
                if not same:
                    ctx["revisions"][k] = prev.get("revision", 0) + 1
                    revised.append(doc_no(ctx, k))
            path = out / f"{doc_no(ctx, k)}.xlsx"
            num_ = _build_doc(ctx, k, path)
        except PermissionError as e:
            print(f"  [오류] {Path(e.filename or '').name or k} 파일을 저장할 수 없습니다. 엑셀에서 열려 있으면 닫고 다시 실행하세요.")
            return 1
        entry = {"type": k, "number": num_, "file": path.name, "revision": ctx["revisions"].get(k, 0),
                 "sha256": _sha256_file(path), "content_sha256": content_hash(path)}
        if args.pdf:
            pdf, why = to_pdf(path, out)
            if pdf:
                entry["pdf"] = pdf.name
                entry["pdf_sha256"] = _sha256_file(pdf)
            else:
                pdf_failures.append(f"{k}: {why}")
                print(f"  [경고] {k} PDF 생성 실패: {why}")
        docs.append(entry)
        print(f"  생성: {path.relative_to(ROOT)}" + (" (+pdf)" if entry.get("pdf") else ""))
    manifest = build_manifest(ctx, docs, v)
    manifest["inputs_sha256"] = ctx["inputs_sha256"]
    if revised:
        manifest["human_review"].append("개정판 발행: " + ", ".join(revised) + " (상대방에게 이전 판을 대체한다고 알릴 것)")
        print(f"  [안내] 승인된 서류 이후 내용이 바뀌어 개정판으로 만들었습니다: {', '.join(revised)}")
    if pdf_failures:
        manifest["human_review"].append("PDF 생성 실패: " + "; ".join(pdf_failures))
    if v["errors"]:
        manifest["status"] = "blocked"
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if not docs:
        print(f"[{deal['deal_id']}] 생성된 서류가 없습니다.")
        return 1
    print(f"[{deal['deal_id']}] 완료. ERROR {v['errors']} / WARNING {v['warnings']} -> {out / 'manifest.json'}")
    print("  사람이 검토한 뒤 터미널에서 직접 `python scripts/tradedocs.py approve "
          f"{Path(args.deal).as_posix()}`로 승인해야 다음 에이전트가 사용할 수 있습니다.")
    return 0


def cmd_approve(args):
    """사람이 서류를 검토하고 승인했다는 기록을 남깁니다. 에이전트는 이 명령을 실행하지 않습니다."""
    if not _interactive_console():
        print("approve는 사람이 터미널에서 직접 실행해야 합니다(에이전트·자동화 실행 불가).", file=sys.stderr)
        return 2
    ctx = build_context(args.deal)
    validate(ctx)
    out = _out_dir(ctx)
    if out is None:
        return 1
    mpath = out / "manifest.json"
    if not mpath.exists():
        print("manifest.json이 없습니다. 먼저 generate를 실행하세요.")
        return 1
    m = load_json(mpath)
    if m.get("status") == "approved":
        print(f"이미 승인되었습니다({(m.get('approval') or {}).get('approved_at')}).")
        if m.get("inputs_sha256") != ctx["inputs_sha256"]:
            print("  다만 승인 이후 deal 또는 기준 데이터가 바뀌었습니다. generate를 다시 실행해 개정판을 만든 뒤 승인하세요.")
            return 1
        return 0
    if m.get("status") != "needs_human_review" or ctx["report"].count("ERROR"):
        print(f"승인할 수 없습니다: status={m.get('status')}, 현재 ERROR {ctx['report'].count('ERROR')}건. validation.md를 확인하세요.")
        return 1
    # 생성 이후 deal·기준 데이터·서류 파일이 바뀌지 않았는지 확인합니다.
    problems = []
    if m.get("inputs_sha256") != ctx["inputs_sha256"]:
        problems.append("서류를 만든 뒤 deal 또는 기준 데이터가 바뀌었습니다. generate를 다시 실행하세요.")
    for d in m.get("documents", []):
        f = out / d["file"]
        if not f.exists() or _sha256_file(f) != d.get("sha256"):
            problems.append(f"{d['file']}이 없거나 생성 후 수정되었습니다.")
        if d.get("pdf") and (not (out / d["pdf"]).exists() or
                             (d.get("pdf_sha256") and _sha256_file(out / d["pdf"]) != d["pdf_sha256"])):
            problems.append(f"{d['pdf']}이 없거나 생성 후 수정되었습니다.")
    if problems:
        print("승인할 수 없습니다:")
        for p in problems:
            print(f"  - {p}")
        return 1

    t = m.get("totals", {})
    print(f"[{m['deal_id']}] 승인 전 확인")
    print(f"  서류: {', '.join(d['number'] for d in m.get('documents', []))}")
    print(f"  금액: PI {t.get('contract_amount')} / CI {t.get('invoice_amount')} {t.get('currency')}")
    if ctx["role"] == "seller":
        b = _as_dict(ctx["company"].get("bank"))
        print(f"  계좌: {b.get('bank_name')} / {b.get('swift')} / {b.get('account_no')} / {b.get('beneficiary')}")
    print("  사람 확인 항목:")
    for h in m.get("human_review", []):
        print(f"    - {h}")
    for i in ctx["report"].items:
        if i["level"] == "WARNING":
            print(f"    - WARNING {i['code']}: {i['message']}")
    try:
        who = input("승인자 이름: ").strip()
        confirm = input(f"위 항목을 모두 확인했으면 deal_id({m['deal_id']})를 입력하세요(취소: Enter): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n취소했습니다.")
        return 1
    if not who or confirm != m["deal_id"]:
        print("입력이 비었거나 일치하지 않아 취소했습니다.")
        return 1
    now = dt.datetime.now().isoformat(timespec="seconds")
    record = {"approved_by": who, "approved_at": now, "inputs_sha256": ctx["inputs_sha256"],
              "documents": m.get("documents", [])}
    with open(out / "approvals.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    m["status"] = "approved"
    m["approval"] = {"approved_by": who, "approved_at": now}
    mpath.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"승인 완료: {mpath.relative_to(ROOT)} (status=approved)")
    return 0


def _interactive_console():
    """사람이 직접 입력하는 터미널인지 확인합니다. Windows에서는 NUL 장치도 isatty()가 True라서 콘솔 여부를 따로 봅니다."""
    if not sys.stdin or not sys.stdin.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt
            handle = msvcrt.get_osfhandle(sys.stdin.fileno())
            mode = ctypes.c_uint32()
            return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
        except (OSError, AttributeError, ValueError):
            return False
    return True


def cmd_bank_verify(args):
    """사람이 계좌를 확인했다는 기록을 남깁니다. 에이전트는 이 명령을 실행하지 않습니다."""
    if not _interactive_console():
        print("bank-verify는 사람이 터미널에서 직접 실행해야 합니다(에이전트·자동화 실행 불가).", file=sys.stderr)
        return 2
    bank = _as_dict(load_json(DATA / "company.json").get("bank"))
    missing = [k for k in BANK_REQUIRED if not bank.get(k)]
    if missing:
        print(f"company.json 은행 정보가 비어 있습니다: {', '.join(missing)}")
        return 1
    print("아래 계좌를 은행 또는 기존에 알던 연락처로 확인했는지 점검하세요.")
    for k in BANK_FIELDS:
        print(f"  {k:12}: {bank.get(k, '')}")
    digits = re.sub(r"\D", "", str(bank["account_no"]))
    try:
        ans = input("확인했다면 계좌번호 끝 4자리를 입력하세요(취소: Enter): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n취소했습니다.")
        return 1
    if not ans or ans != digits[-4:]:
        print("입력이 일치하지 않아 취소했습니다.")
        return 1
    lock = {"fingerprint": bank_fingerprint(bank), "account_no_last4": digits[-4:],
            "verified_at": dt.datetime.now().isoformat(timespec="seconds"),
            "note": "사람이 bank-verify로 확인한 기록입니다. 직접 수정하지 마세요."}
    BANK_LOCK.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장: {BANK_LOCK.relative_to(ROOT)} (fingerprint {lock['fingerprint']})")
    return 0


def _utf8_console():
    # Windows 콘솔(cp949)에서 한글 메시지가 깨지지 않게 합니다.
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main():
    _utf8_console()
    ap = argparse.ArgumentParser(description="무역서류 에이전트 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pv = sub.add_parser("validate", help="deal을 검증하고 보고서만 만듭니다")
    pv.add_argument("deal")
    pg = sub.add_parser("generate", help="검증 후 서류와 manifest를 만듭니다")
    pg.add_argument("deal")
    pg.add_argument("--docs", help="예: PI,CI,PL,SI (기본: 역할별 기본값)")
    pg.add_argument("--pdf", action="store_true", help="LibreOffice로 PDF도 생성")
    pg.add_argument("--force", action="store_true", help="ERROR가 있어도 생성(status=blocked). 형식 오류·계좌 변경은 제외")
    sub.add_parser("bank-verify", help="(사람 전용) 계좌 확인 기록 저장")
    pa = sub.add_parser("approve", help="(사람 전용) 생성된 서류를 검토·승인")
    pa.add_argument("deal")
    args = ap.parse_args()
    handlers = {"validate": cmd_validate, "generate": cmd_generate, "bank-verify": cmd_bank_verify, "approve": cmd_approve}
    sys.exit(handlers[args.cmd](args))


if __name__ == "__main__":
    main()
