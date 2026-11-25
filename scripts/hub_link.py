"""trade_hub 공용 DB 연결 (6번 documents).

trade_hub와 합의한 규칙 (trade_hub 커밋 2102ccd, 스키마 v18):
  1) 수량: 최신 견적 quotes.min_qty. product_suppliers.max_order_qty(비면 구매 방식 기본값)를 넘으면 그 값으로 줄임.
     견적에 수량이 없으면 ERROR. qty_source = quote_min_qty / capped_max_order_qty
  2) 지정장소: quotes.incoterm_place. 비면 WARNING(공급처 국가로 추정하지 않음). 통화는 USD 고정
  3) 원산지: supplier_specs_json → specs_json의 origin_country, 없으면 suppliers.country + WARNING(추정)
  4) 승인: tradedocs.py approve(해시 확인)가 끝나면 db.approve_document. trade_hub done만으로는 승인 아님
  5) 기록: documents 표(db.add_document) + tasks(kind=document, 'PO 초안 검토: <품목>')
     검증 ERROR여도 check_result='error' 행을 남긴다(안 남기면 슈퍼바이저가 6시간마다 다시 실행)

쓰는 곳: agent_runs(documents), documents, tasks — 다른 표는 읽기만 합니다.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import tradedocs as td

ROOT = td.ROOT

# supervisor.json "documents" when_sql과 같은 조건 — 바꾸면 같이 바꿀 것 (품목 id 목록을 돌려줌)
PENDING_SQL = """
SELECT p.id FROM products p
WHERE p.stage IN ('negotiating', 'sampling')
  AND EXISTS (SELECT 1 FROM product_suppliers ps JOIN quotes q ON q.product_supplier_id = ps.id
              WHERE ps.product_id = p.id AND ps.status = 'selected')
  AND ((SELECT MAX(d.created_at) FROM documents d WHERE d.product_id = p.id AND d.kind = 'po') IS NULL
       OR EXISTS (SELECT 1 FROM product_suppliers ps JOIN quotes q ON q.product_supplier_id = ps.id
                  WHERE ps.product_id = p.id AND ps.status = 'selected'
                    AND q.created_at > (SELECT MAX(d.created_at) FROM documents d WHERE d.product_id = p.id AND d.kind = 'po'))
       OR EXISTS (SELECT 1 FROM compliance_checks c WHERE c.product_id = p.id
                    AND c.created_at > (SELECT MAX(d.created_at) FROM documents d WHERE d.product_id = p.id AND d.kind = 'po')))
ORDER BY p.id
"""

COUNTRY_NAMES = {"CN": "China", "JP": "Japan", "US": "USA", "KR": "Korea", "VN": "Vietnam", "TW": "Taiwan",
                 "HK": "Hong Kong", "TH": "Thailand", "IN": "India", "DE": "Germany"}


def hub_db():
    """trade_hub의 db 모듈. 설치(pip install -e ../trade_hub)가 안 되어 있으면 옆 폴더에서 찾습니다."""
    try:
        from trade_hub import db
    except ImportError:
        sys.path.insert(0, str(ROOT.parent / "trade_hub"))
        try:
            from trade_hub import db
        except ImportError:
            sys.exit("trade_hub를 찾을 수 없습니다. `pip install -e ../trade_hub`로 설치하세요.")
    return db


def _json(v):
    if not v:
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def _origin(ps, supplier):
    """(원산지, 추정 여부)"""
    for col in ("supplier_specs_json", "specs_json"):
        flags = (_json(ps[col]) or {}).get("flags") or {}
        oc = flags.get("origin_country")
        if oc:
            return COUNTRY_NAMES.get(str(oc).upper(), str(oc)), False
    country = supplier["country"]
    if country:
        return COUNTRY_NAMES.get(str(country).upper(), str(country)), True
    return "", False


def _unit_price(quote, qty):
    """가격 구간표가 있으면 발주 수량에 맞는 구간 단가, 없으면 견적 단가."""
    price, note = quote["unit_price_usd"], None
    tiers = _json(quote["price_tiers_json"]) or []
    ok = [t for t in tiers if isinstance(t, dict) and t.get("qty") is not None and t.get("usd") is not None
          and qty is not None and t["qty"] <= qty]
    if ok:
        best = max(ok, key=lambda t: t["qty"])
        price, note = best["usd"], f"견적 가격 구간({best['qty']}개 이상 {best['usd']} USD)을 적용했습니다."
    return price, note


def build_deal(conn, db, product, ps, quote):
    """공용 DB 행으로 deal(JSON)을 만듭니다. 값을 추측해 채우지 않고, 모르는 것은 external_checks로 알립니다."""
    supplier = conn.execute("SELECT * FROM suppliers WHERE id = ?", (ps["supplier_id"],)).fetchone()
    checks = []

    def check(level, code, msg):
        checks.append({"level": level, "code": code, "message": msg})

    # 1) 수량
    limit = db.order_limit(ps["purchase_mode"], ps["max_order_qty"])
    min_qty = quote["min_qty"]
    if min_qty is None:
        qty, qty_source = 0, None
        check("ERROR", "HUB_QTY_MISSING", "견적에 수량(quotes.min_qty)이 없어 발주 수량을 정할 수 없습니다. 사람이 수량을 입력해야 합니다.")
    elif limit is not None and min_qty > limit:
        qty, qty_source = limit, "capped_max_order_qty"
        check("WARNING", "HUB_QTY_CAPPED", f"견적 최소 수량 {min_qty}개가 첫 구매 한도 {limit}개를 넘어 {limit}개로 줄였습니다. "
              "공급처가 이 수량을 받아 주는지 확인하세요.")
    else:
        qty, qty_source = min_qty, "quote_min_qty"
    check("INFO", "HUB_QTY_SOURCE", f"발주 수량 {qty}개 (출처: {qty_source or '없음'}). 최종 수량은 승인 전에 확인하세요.")

    # 2) 단가·인코텀즈
    price, tier_note = _unit_price(quote, qty)
    if tier_note:
        check("INFO", "HUB_PRICE_TIER", tier_note)
    keys = quote.keys()
    place = (quote["incoterm_place"] if "incoterm_place" in keys else None) or ""
    rule = (quote["incoterm"] or "").upper()
    if not rule:
        check("ERROR", "HUB_INCOTERM_MISSING", "견적에 인코텀즈(quotes.incoterm)가 없습니다. 공급처와 조건을 확정하세요.")

    # 3) 원산지
    origin, estimated = _origin(ps, supplier)
    if not origin:
        check("ERROR", "HUB_ORIGIN_MISSING", "원산지를 알 수 없습니다(사양의 origin_country와 공급처 국가가 모두 비어 있음).")

    # 품명(영문): 1번 조사 검색어. 사람 확인 필요
    name_en = (product["search_keyword_en"] or "").strip()
    if name_en:
        name_en = name_en[:1].upper() + name_en[1:]
        check("WARNING", "HUB_DESCRIPTION", f"영문 품명은 조사 검색어('{name_en}')를 썼습니다. 공급처 견적의 품명·모델과 맞게 고치세요.")
    else:
        name_en = product["name"]
        check("WARNING", "HUB_DESCRIPTION", "영문 품명(search_keyword_en)이 없어 한국어 품목명을 넣었습니다. 영문으로 바꾸세요.")

    # 통관 에이전트의 계약 조건(한국어) — PO에 자동 번역해 넣지 않고 사람에게 넘김
    cc = conn.execute("SELECT contract_clauses_json, hs_candidates_json FROM compliance_checks WHERE product_id = ? "
                      "ORDER BY id DESC LIMIT 1", (product["id"],)).fetchone()
    clauses = (_json(cc["contract_clauses_json"]) if cc else None) or []
    hs_candidates = (_json(cc["hs_candidates_json"]) if cc else None) or []
    if clauses:
        check("WARNING", "HUB_CONTRACT_CLAUSES", f"통관 에이전트가 정리한 계약 조건 {len(clauses)}개는 한국어라 PO에 자동으로 넣지 않았습니다. "
              "영문 조항으로 PO에 넣을지 확인하세요(할 일 상세에 원문).")

    remarks = []
    if quote["lead_time_days"]:
        remarks.append(f"Lead time: {quote['lead_time_days']} days as per supplier quotation.")
    if quote["valid_until"]:
        remarks.append(f"Quotation valid until {quote['valid_until']}.")

    deal_id = f"HUB-P{product['id']}-S{ps['id']}"
    deal = {
        "deal_id": deal_id,
        "our_role": "buyer",
        "partner_id": f"HUB-SUP{supplier['id']}",
        "partner": {"type": "supplier", "name_en": supplier["company"], "address_en": "", "country": supplier["country"] or "",
                    "contact": supplier["contact_name"] or "", "email": supplier["contact_email"] or "",
                    "phone": supplier["contact_other"] or "", "currency": "USD"},
        "doc_date": dt.date.today().isoformat(),
        "currency": "USD",
        "incoterm": {"rule": rule, "place": place},
        "payment_terms": quote["payment_terms"] or "",
        "transport_mode": "sea",
        "port_of_loading": place if rule in ("FOB", "FAS", "FCA") else "",
        "port_of_discharge": "",
        "items": [{
            "sku": f"HUB-P{product['id']}", "qty": qty, "unit_price": price,
            "origin": origin, "origin_estimated": estimated,
            "product": {"name_en": name_en, "name_ko": product["name"], "model": "", "hs_code": product["hs_code"] or "",
                        "unit": "PCS", "unit_price": price, "currency": "USD", "origin": origin},
        }],
        "doc_remarks": " ".join(remarks),
        "remarks": "trade_hub 공용 DB에서 from-hub로 자동 생성한 PO 초안입니다. 이 파일을 직접 고치지 말고 공용 DB를 고친 뒤 다시 실행하세요.",
        "external_checks": checks,
        "source": {"agent": "trade_hub", "product_id": product["id"], "product_supplier_id": ps["id"],
                   "quote_id": quote["id"], "qty_source": qty_source},
    }
    extra = {"clauses": clauses, "hs_candidates": hs_candidates, "qty_source": qty_source}
    return deal, extra


def _task_detail(product, doc_no, path, total, ctx, extra):
    rep = ctx["report"]
    lines = [f"파일: {path}", f"서류 번호: {doc_no}",
             f"금액: {total} USD" if total is not None else "금액: (생성 안 됨)",
             f"검증: ERROR {rep.count('ERROR')} / WARNING {rep.count('WARNING')}"]
    lines += [f"- [{i['level']}] {i['message']}" for i in rep.items if i["level"] in ("ERROR", "WARNING")]
    if extra["clauses"]:
        lines += ["", "통관 에이전트 계약 조건(PO 반영 검토):"] + [f"- {c}" for c in extra["clauses"]]
    if extra["hs_candidates"]:
        lines += ["", "HS 후보(관세사 확인 전, PO에는 넣지 않음): " +
                  ", ".join(str(h.get("code")) for h in extra["hs_candidates"] if isinstance(h, dict))]
    lines += ["", "승인: 검토 후 터미널에서 직접 `python scripts/tradedocs.py approve "
              f"deals/{ctx['deal']['deal_id']}.json` (trade_hub done만으로는 승인되지 않음)"]
    return "\n".join(lines)


def process(conn, db, run, product, ps, quote, pdf=False):
    """PO 1건: deal 저장 → 검증 → (ERROR 없으면) PO 생성 → documents·tasks 기록. 결과 요약을 돌려줍니다."""
    deal, extra = build_deal(conn, db, product, ps, quote)
    deal_path = ROOT / "deals" / f"{deal['deal_id']}.json"
    deal_path.write_text(json.dumps(deal, ensure_ascii=False, indent=2), encoding="utf-8")

    ctx = td.build_context(deal_path)
    td.validate(ctx)
    out = td.OUTPUT / deal["deal_id"]
    v = td.write_report(ctx, out)
    base_no = f"PO-{deal['deal_id']}"
    prev = conn.execute("SELECT MAX(revision) FROM documents WHERE doc_no = ?", (base_no,)).fetchone()[0]
    rev = 0 if prev is None else prev + 1
    ctx["revisions"] = {"PO": rev}
    doc_no = td.doc_no(ctx, "PO")
    checks = [{"level": i["level"].lower(), "msg": i["message"]} for i in ctx["report"].items if i["level"] != "INFO"]
    result = "error" if v["errors"] else ("warning" if v["warnings"] else "ok")
    total = float(ctx["totals"]["contract_amount"])

    if v["errors"]:
        path, sha, total_rec = out / "validation.md", None, None
        manifest = {"event": "docs_generated", "deal_id": deal["deal_id"], "status": "blocked", "documents": [],
                    "totals": {}, "validation": {"errors": v["errors"], "warnings": v["warnings"], "report": "validation.md"}}
    else:
        path = out / f"{doc_no}.xlsx"
        td.doc_invoice(ctx, "PO", path)
        sha, total_rec = td._sha256_file(path), total
        entry = {"type": "PO", "number": doc_no, "file": path.name, "revision": rev, "sha256": sha,
                 "content_sha256": td.content_hash(path)}
        if pdf:
            p, why = td.to_pdf(path, out)
            if p:
                entry.update(pdf=p.name, pdf_sha256=td._sha256_file(p))
        manifest = td.build_manifest(ctx, [entry], v)
        manifest["inputs_sha256"] = ctx["inputs_sha256"]

    rel = path.relative_to(ROOT).as_posix()
    with conn:
        task_id = db.add_task(conn, "document", f"PO 초안 검토: {product['name']}", product_id=product["id"], run_id=run.id,
                              detail=_task_detail(product, doc_no, rel, total_rec, ctx, extra),
                              priority=1 if v["errors"] else 2)
        # 같은 할 일이 이미 대기 중이면 add_task가 그 id를 돌려주므로 상세를 최신으로 바꿉니다.
        conn.execute("UPDATE tasks SET detail = ?, run_id = ? WHERE id = ?",
                     (_task_detail(product, doc_no, rel, total_rec, ctx, extra), run.id, task_id))
        doc_id = db.add_document(conn, "po", base_no, product["id"], rel, product_supplier_id=ps["id"], quote_id=quote["id"],
                                 run_id=run.id, task_id=task_id, sha256=sha, qty=deal["items"][0]["qty"] or None,
                                 total_usd=total_rec, qty_source=extra["qty_source"], check_result=result, checks=checks)
    manifest["hub"] = {"document_id": doc_id, "doc_no": base_no, "revision": rev, "task_id": task_id,
                       "product_id": product["id"], "product_supplier_id": ps["id"], "quote_id": quote["id"]}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"product_id": product["id"], "doc_no": doc_no, "result": result, "document_id": doc_id, "path": rel}


def cmd_from_hub(args):
    db = hub_db()
    conn = db.connect()
    if args.product:
        ids = [args.product]
    else:
        ids = [r[0] for r in conn.execute(PENDING_SQL)]
    if args.dry_run:
        print(f"처리할 품목 {len(ids)}개: {ids}")
        return 0
    if not ids:
        print("처리할 품목이 없습니다.")
        return 0
    done = []
    with db.agent_run(conn, "documents", args={"product": args.product, "pdf": args.pdf}) as run:
        run.report_dir = str(td.OUTPUT)
        for pid in ids:
            product = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
            if product is None:
                print(f"  품목 {pid} 없음")
                continue
            lines = conn.execute("SELECT * FROM product_suppliers WHERE product_id = ? AND status = 'selected' ORDER BY id",
                                 (pid,)).fetchall()
            for ps in lines:
                quote = conn.execute("SELECT * FROM quotes WHERE product_supplier_id = ? ORDER BY id DESC LIMIT 1",
                                     (ps["id"],)).fetchone()
                if quote is None:
                    continue
                r = process(conn, db, run, product, ps, quote, pdf=args.pdf)
                done.append(r)
                print(f"  [{r['result']}] {r['doc_no']} (품목 {pid}) -> {r['path']}")
    print(f"완료: PO {len(done)}건 기록 (ERROR {sum(r['result'] == 'error' for r in done)}건). 승인은 사람이 approve로 합니다.")
    return 0


def approve_in_hub(hub, sha256):
    """tradedocs approve가 해시 확인을 마친 뒤 부릅니다. 공용 DB의 documents를 approved로, 연결된 할 일을 done으로."""
    db = hub_db()
    conn = db.connect()
    row = conn.execute("SELECT status, sha256 FROM documents WHERE id = ?", (hub["document_id"],)).fetchone()
    if row is None:
        raise ValueError(f"공용 DB에 서류 {hub['document_id']}가 없습니다.")
    if row["sha256"] != sha256:
        raise ValueError("공용 DB에 기록된 파일 해시와 지금 파일이 다릅니다.")
    with conn:
        db.approve_document(conn, hub["document_id"])
