# 다른 에이전트와 연결하기

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
