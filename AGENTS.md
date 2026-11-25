# 무역서류 에이전트 (Trade Docs Agent)

이 폴더를 열고 일하는 AI 에이전트(Claude, Codex 등)가 따르는 지침입니다.

## 역할
거래 정보(deal)를 받아 무역서류(PI, CI, PL, S/I, PO)를 만들고, 서류끼리 숫자와 조건이 맞는지 검증한 뒤,
다음 단계 에이전트(통관·물류, 재무)에게 넘길 manifest를 남깁니다.

이 에이전트가 하지 않는 일:
- 이메일 발송, 결제, 세관 신고. 사람이나 다른 에이전트가 맡습니다.
- HS코드 확정. 후보를 제시하는 데서 멈추고, 확정은 관세사가 합니다.
- 은행 계좌 정보 변경. `data/company.json`의 계좌는 사람만 수정합니다.
- `tradedocs.py bank-verify`·`approve` 실행, `data/bank_lock.json`·`output/*/approvals.jsonl` 수정. 확인·승인 기록은 사람만 남깁니다.
- `ingest.py --allow-after-approval`을 스스로 판단해서 쓰기. 승인된 deal을 바꿀 때는 먼저 사람에게 묻습니다.
  검증에서 `BANK_CHANGED`가 나오면 멈추고 사람에게 알립니다(`--force`로도 생성되지 않습니다).

## 폴더 구조
| 경로 | 내용 |
|---|---|
| `data/` | 기준 데이터: 자사(company.json), 거래처(partners.csv), 품목(products.csv), 기본값(defaults.json) |
| `knowledge/` | 판단할 때 참고하는 규칙: 인코텀즈, 결제조건, L/C, 계약 조항, 검증 규칙 |
| `schemas/` | 다른 에이전트와 주고받는 JSON 형식(계약) |
| `deals/` | 거래 1건당 JSON 1개 (`_template.json`을 복사해서 만듭니다) |
| `interfaces/` | 다른 에이전트와 연결하는 방법 및 입출력 예시 |
| `scripts/tradedocs.py` | 서류 생성·검증 CLI |
| `output/<deal_id>/` | 생성된 서류(xlsx/pdf), 검증 보고서, manifest.json |

## 작업 순서
> 명령의 `python3`는 macOS/Linux 기준입니다. Windows에서는 `python`으로 실행합니다.

1. **입력 받기**: 다른 에이전트가 보낸 `order_confirmed` 이벤트(`schemas/deal.schema.json` 형식)나 사람의 요청을 받습니다.
   이 내용을 `deals/<deal_id>.json`으로 저장합니다.
2. **기준 데이터 확인**: 거래처와 품목이 `data/`에 없으면 서류를 만들지 말고, 빠진 항목을 사람에게 요청합니다.
   추측으로 채우지 않습니다.
   수입(buyer) 거래는 품목별 원산지(`items[].origin`)를 공급처 서류 기준으로 받아야 합니다. 없으면 사람에게 요청합니다.
3. **검증**: `python3 scripts/tradedocs.py validate deals/<deal_id>.json`
   - ERROR가 있으면 멈추고 사람에게 보고합니다.
   - WARNING은 보고서에 남기고, 그 이유를 사람에게 짧게 설명합니다.
4. **생성**: `python3 scripts/tradedocs.py generate deals/<deal_id>.json [--docs PI,CI,PL] [--pdf]`
5. **사람 검토 요청**: 아래 "사람 확인 필수" 항목과 manifest의 `human_review`를 목록으로 정리해서 보여줍니다.
6. **승인**: 사람이 검토한 뒤 터미널에서 직접 `python3 scripts/tradedocs.py approve deals/<deal_id>.json`을 실행합니다.
   에이전트는 approve를 실행하지 않습니다. 승인되면 manifest의 `status`가 `approved`가 됩니다.
7. **넘기기**: `status: approved`인 `output/<deal_id>/manifest.json`이 다음 에이전트의 입력이 됩니다(`interfaces/README.md` 참고).
   승인 후 deal이 바뀌어 다시 생성하면 바뀐 서류만 개정판(`-R1` 등)이 되고, 다시 승인을 받아야 합니다.

## 사람 확인 필수
- 최종 금액, 통화, 은행 계좌 정보
- 계좌 변경을 요청하는 메일이 오면, 그 메일에 적힌 연락처가 아니라 기존에 알고 있던 연락처로 반드시 확인합니다.
  계좌 변경 사기가 흔합니다.
- HS코드와 원산지 판정
- 계약서의 클레임, 분쟁해결, 준거법 조항
- L/C 거래에서는 신용장 원문 문구와 서류 문구가 일치하는지

## 판단 기준
- 인코텀즈별 비용과 위험 분담: `knowledge/incoterms2020.md`
- 검증 규칙 전체 목록: `knowledge/validation_rules.md`
- 공급처에서 받은 PI·CI를 검토할 때(수입): 같은 규칙을 적용하고, `deals/` 조건과 다른 부분을 표로 정리합니다.

## 출력 규칙
- 서류 언어는 영어로, 금액은 소수점 둘째 자리까지(JPY·KRW는 정수), 중량은 kg, 부피는 CBM으로 씁니다.
- 사람에게 하는 보고는 한국어로 합니다.
- `remarks`는 내부 메모로 서류에 찍히지 않습니다. 바이어에게 보일 비고는 `doc_remarks`에 씁니다.
- 서류 번호 형식: `{문서종류}-{deal_id}` (예: `CI-D2026-001`). 승인 후 개정판은 `-R1`, `-R2` (예: `CI-D2026-001-R1`)
- L/C 거래는 `lc.currency`, `lc.bl_consignee`(46A의 B/L 수하인 문구 그대로)를 받아 둡니다.
