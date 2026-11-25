# 다른 에이전트와 연결하기

## trade_hub 공용 DB (6번 documents) — 기본 연결 방식

1인 무역 파이프라인(`../trade_hub`)과는 공용 SQLite DB로 연결합니다. 슈퍼바이저가 `supervisor.json`의
`documents` 설정대로 `python scripts/tradedocs.py from-hub`를 실행합니다(환경변수 `TRADE_DB`로 DB 경로를 넘김).

- 대상: 협상·샘플 단계에서 선정된(`selected`) 공급처 줄에 견적이 있고, 그 품목 PO가 없거나 최신 PO 뒤에 견적·통관 판정이 새로 생긴 품목
  (`scripts/hub_link.py` `PENDING_SQL` = `supervisor.json` `when_sql`, 바꾸면 같이 바꿀 것)
- 읽기: `products`, `product_suppliers`, `suppliers`, `quotes`, `compliance_checks`
- 쓰기: `agent_runs`(documents), `documents`(`db.add_document`), `tasks`(kind=document, "PO 초안 검토: <품목>")
- 수량 = 최신 견적 `min_qty`(첫 구매 한도를 넘으면 한도로 줄임), 지정장소 = `quotes.incoterm_place`(비면 WARNING),
  원산지 = 사양의 `origin_country`(없으면 공급처 국가 + 추정 WARNING), 통화 USD
- 검증 ERROR여도 `check_result='error'` 행을 남깁니다(사람이 공용 DB를 고치면 다음 실행에서 다시 만듦)
- 승인: 사람이 `python scripts/tradedocs.py approve deals/HUB-P<품목>-S<공급처줄>.json` →
  공용 DB `documents.status='approved'`, 연결된 할 일 done. `trade_hub done`만으로는 승인되지 않습니다
- 허브에서 만든 `deals/HUB-*.json`은 직접 고치지 말고 공용 DB를 고친 뒤 `from-hub --product <id>`로 다시 만듭니다

```bash
python scripts/tradedocs.py from-hub --dry-run       # 처리할 품목만 보기
python scripts/tradedocs.py from-hub                 # 실행 조건에 맞는 품목 전부
python scripts/tradedocs.py from-hub --product 12    # 한 품목만
```

아래 JSON 이벤트 방식은 trade_hub 밖의 도구(n8n 등)나 수출 거래용입니다.

이 에이전트는 **파일 기반 JSON 이벤트**로 다른 에이전트와 연결됩니다. 폴더를 공유하거나, n8n·Make 같은 워크플로 도구로
JSON을 전달하거나, 상위 오케스트레이터 에이전트가 CLI를 호출하는 방식 모두 같은 형식을 씁니다.

```
[시장조사] → [공급처 발굴] → [원가계산] ─┐
                                        ├─ order_confirmed ─▶ [무역서류 에이전트] ─ docs_generated ─┬─▶ [통관·물류]
[커뮤니케이션·협상] ───────────────────┘                                                         └─▶ [재무·관리]
```

## 받는 것 (inbound)

| 이벤트 | 보내는 에이전트 | 형식 | 예시 |
|---|---|---|---|
| `order_confirmed` | 커뮤니케이션·협상 | `schemas/deal.schema.json` + `event` 필드 | `inbound/order_confirmed.example.json` |
| `price_update` | 원가계산 | 품목별 단가 (deal의 `items[].unit_price`로 반영) | `inbound/price_update.example.json` |
| `lc_received` | 커뮤니케이션 / 사람 | deal의 `lc` 블록 | `inbound/lc_received.example.json` |

받은 뒤 처리 (`python3 scripts/ingest.py <이벤트파일>`, Windows는 `python`):
1. `order_confirmed`: `event` 필드를 빼고 `deals/<deal_id>.json`으로 새로 저장합니다. 이미 있으면 중단합니다.
   `--overwrite`를 주면 이전 파일을 `deals/_archive/`에 보관하고 교체합니다(history는 이어 붙임).
2. `price_update`: SKU별 `unit_price`만 바꿉니다. 이전 단가는 `history[].price_changes`에 남습니다.
3. `lc_received`: `lc` 블록을 교체합니다. 이전 내용과 바뀐 항목은 `lc_history`에 남습니다(Amendment 이력).
4. 이미 사람이 승인한 서류가 있는 deal은 `--allow-after-approval` 없이는 바꾸지 않습니다(사람 확인 후에만).
5. `python3 scripts/tradedocs.py generate deals/<deal_id>.json --pdf`

## 보내는 것 (outbound)

| 이벤트 | 받는 에이전트 | 위치 | 내용 |
|---|---|---|---|
| `docs_generated` | 통관·물류, 재무 | `output/<deal_id>/manifest.json` | `schemas/manifest.schema.json` |

- `handoff.customs_logistics_agent`: CI/PL/SI 파일, HS코드, 원산지, 포장 수, 중량, CBM, 송장 금액
- `handoff.finance_agent`: 받을 돈(수출) 또는 줄 돈(수입), 통화, 결제조건, 송장 번호, ETD, L/C 번호
- `status`
  - `needs_human_review`: 서류 생성 완료, 사람 검토 전. **다른 에이전트는 아직 사용하지 않습니다**
  - `approved`: 사람이 `tradedocs.py approve`로 검토·승인함. `approval.approved_by/approved_at`이 있습니다
  - `blocked`: ERROR가 있어 생성 중단. `validation.md` 확인 후 deal을 수정하고 다시 실행
- `documents[].sha256`: 파일 해시. 받는 쪽은 사용 전에 파일 해시가 같은지 확인합니다
- `documents[].revision`: 승인 후 내용이 바뀌어 다시 만든 서류는 번호에 `-R1`, `-R2`가 붙습니다(예: `CI-D2026-001-R1`)

## 규칙
- 다른 에이전트는 `output/` 파일을 **읽기만** 합니다. 수정이 필요하면 deal을 바꾸고 다시 생성합니다.
- 서류와 금액·계좌·HS코드는 `status`가 `approved`일 때만 외부로 보내거나 다음 단계에 씁니다.
- 승인 기록은 `output/<deal_id>/approvals.jsonl`에 쌓입니다(누가, 언제, 어떤 파일 해시를 승인했는지).
- 모든 이벤트에는 `deal_id`가 있어야 합니다. 이것이 에이전트 사이의 공통 키입니다.
