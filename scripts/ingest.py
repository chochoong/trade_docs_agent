#!/usr/bin/env python3
"""다른 에이전트가 보낸 inbound 이벤트(JSON)를 deals/<deal_id>.json에 반영합니다.

사용법 (Windows는 python3 대신 python):
  python3 scripts/ingest.py interfaces/inbound/order_confirmed.example.json [--generate]

- order_confirmed: deal 파일을 새로 만듭니다. 이미 있으면 중단합니다.
  --overwrite를 주면 이전 파일을 deals/_archive/에 보관하고 교체합니다(history는 이어 붙입니다).
- price_update: 기존 deal의 items[].unit_price를 SKU 기준으로 업데이트합니다(이전 단가는 history에 남김).
- lc_received: 기존 deal의 lc 블록을 교체합니다(이전 lc는 lc_history에 남김 = Amendment 이력).

이미 사람이 승인(approve)한 서류가 있는 deal은 금액·조건이 바뀌면 개정판이 되므로,
--allow-after-approval 없이는 바꾸지 않습니다.
"""
import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEAL_ID_RE = re.compile(r"^[A-Za-z0-9-]+$")


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def _approved(deal_id):
    return (ROOT / "output" / deal_id / "approvals.jsonl").exists()


def main():
    for s in (sys.stdout, sys.stderr):  # Windows 콘솔 한글 깨짐 방지
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("event_file")
    ap.add_argument("--generate", action="store_true", help="반영 후 바로 서류 생성")
    ap.add_argument("--overwrite", action="store_true", help="order_confirmed가 기존 deal을 교체하도록 허용(이전 파일은 보관)")
    ap.add_argument("--allow-after-approval", action="store_true",
                    help="이미 승인된 서류가 있는 deal도 변경(사람이 확인한 경우에만)")
    a = ap.parse_args()

    ev = json.loads(Path(a.event_file).read_text(encoding="utf-8"))
    if not isinstance(ev, dict):
        sys.exit("이벤트 JSON은 객체({...})여야 합니다.")
    kind, deal_id = ev.pop("event", None), ev.get("deal_id")
    if not deal_id:
        sys.exit("deal_id가 없습니다.")
    # deal_id가 파일 경로가 되므로 '../' 같은 값으로 프로젝트 밖에 쓰지 못하게 막습니다.
    if not isinstance(deal_id, str) or not DEAL_ID_RE.match(deal_id):
        sys.exit(f"deal_id {deal_id!r} 형식이 잘못되었습니다(영문·숫자·하이픈만 허용).")
    if kind not in ("order_confirmed", "price_update", "lc_received"):
        sys.exit(f"알 수 없는 이벤트: {kind}")
    path = ROOT / "deals" / f"{deal_id}.json"
    history = {"event": kind, "source": ev.get("source"), "received_at": _now()}

    if path.exists() and _approved(deal_id) and not a.allow_after_approval:
        sys.exit(f"{deal_id}는 이미 사람이 승인한 서류가 있습니다. {kind}를 반영하면 서류가 개정판이 됩니다. "
                 "사람에게 확인한 뒤 --allow-after-approval로 다시 실행하세요.")

    if kind == "order_confirmed":
        old = None
        if path.exists():
            if not a.overwrite:
                sys.exit(f"{path.name}이 이미 있습니다. 교체하려면 --overwrite(이전 파일은 deals/_archive/에 보관)")
            old = json.loads(path.read_text(encoding="utf-8"))
            archive = ROOT / "deals" / "_archive"
            archive.mkdir(exist_ok=True)
            dst = archive / f"{deal_id}-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
            dst.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"이전 deal 보관: deals/_archive/{dst.name}")
            history["replaced_previous"] = f"_archive/{dst.name}"
        deal = ev
        if old:
            deal["history"] = list(old.get("history", []))
            dropped = [k for k in ("lc", "lc_history") if k in old and k not in deal]
            if dropped:
                print(f"[주의] 새 주문에 {', '.join(dropped)}가 없어 빠졌습니다. 필요하면 lc_received를 다시 보내세요.")
    else:
        if not path.exists():
            sys.exit(f"{path.name}이 없습니다. order_confirmed가 먼저 와야 합니다.")
        deal = json.loads(path.read_text(encoding="utf-8"))
        if kind == "price_update":
            if ev.get("currency") and ev["currency"] != deal.get("currency", "USD"):
                sys.exit("통화가 deal과 다릅니다. 사람 확인 필요.")
            prices = {i["sku"]: i["unit_price"] for i in ev.get("items", []) if "sku" in i and "unit_price" in i}
            changes = []
            for it in deal.get("items", []):
                if it.get("sku") in prices:
                    new = prices[it["sku"]]
                    changes.append({"sku": it["sku"], "from": it.get("unit_price", "(마스터 단가)"), "to": new})
                    it["unit_price"] = new
            unknown = sorted(set(prices) - {it.get("sku") for it in deal.get("items", [])})
            history["price_changes"] = changes
            for c in changes:
                print(f"  {c['sku']}: {c['from']} → {c['to']}")
            print(f"단가 {len(changes)}건 반영")
            if unknown:
                print(f"[주의] deal에 없는 SKU는 무시했습니다: {', '.join(unknown)}")
        else:
            if not isinstance(ev.get("lc"), dict):
                sys.exit("lc_received 이벤트에 lc 객체가 없습니다.")
            prev = deal.get("lc")
            if isinstance(prev, dict):
                changed = sorted(k for k in set(prev) | set(ev["lc"]) if prev.get(k) != ev["lc"].get(k))
                deal.setdefault("lc_history", []).append({"replaced_at": _now(), "lc": prev, "changed_fields": changed})
                history["lc_changed_fields"] = changed
                print("L/C 변경(Amendment) 반영. 바뀐 항목:")
                for k in changed:
                    print(f"  {k}: {prev.get(k)!r} → {ev['lc'].get(k)!r}")
            else:
                print("L/C 정보 반영")
            deal["lc"] = ev["lc"]

    deal.setdefault("history", []).append(history)
    path.write_text(json.dumps(deal, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장: deals/{path.name}")
    if a.generate:
        sys.exit(subprocess.call([sys.executable, str(ROOT / "scripts" / "tradedocs.py"), "generate", str(path), "--pdf"]))


if __name__ == "__main__":
    main()
