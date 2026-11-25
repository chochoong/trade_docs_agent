# 검증 규칙 목록

`scripts/tradedocs.py validate`가 자동으로 검사하는 항목입니다. 코드(`code`)는 validation.json에 그대로 나옵니다.

| 등급 | 코드 | 검사 내용 |
|---|---|---|
| ERROR | REQ_FIELD | deal 필수 항목 누락 |
| ERROR | DEAL_ID_INVALID / ROLE_INVALID / INCOTERM_TYPE / LC_TYPE / ITEMS_TYPE / ITEM_FORMAT / NUM_FORMAT | deal 형식 오류(영문·숫자·하이픈 외 deal_id, seller/buyer 외 역할, 객체가 아닌 incoterm·lc, 숫자가 아닌 값) |
| ERROR | QTY_INVALID / NOTHING_SHIPPED / AMOUNT_NEGATIVE | 계약 수량 0 이하, 선적 수량 음수, 전 품목 선적 0, 운임·보험료 음수 |
| ERROR | ORIGIN_REQUIRED | 수입 거래인데 items[].origin 없음(품목 마스터 원산지를 그대로 쓰지 않음) |
| ERROR | BANK_CHANGED / BANK_LOCK_BROKEN | 사람이 bank-verify로 확인한 뒤 계좌 정보가 바뀜, 확인 기록 파일 손상 |
| ERROR | PARTNER_MISSING / PRODUCT_MISSING | 기준 데이터에 거래처·품목이 없음 |
| ERROR | PARTNER_ROLE | 거래처 종류(type)와 역할이 맞지 않음(수출인데 supplier, 수입인데 buyer) |
| ERROR | LINE_CURRENCY | unit_price가 없어 다른 통화의 마스터 단가를 환산 없이 쓰게 됨 |
| ERROR | PRICE_REQUIRED | 수입 거래인데 unit_price 없음(마스터 단가는 판매가) |
| ERROR | DATE_FORMAT / DATE_ORDER | 날짜 형식 오류, ETA < ETD |
| ERROR | INCOTERM_INVALID / INCOTERM_PLACE | Incoterms 2020의 11개 규칙이 아니거나 지정장소가 없음 |
| ERROR | BANK_MISSING | 수출인데 은행 정보가 비어 있음 |
| ERROR | HS_FORMAT | HS코드가 6/8/10자리 숫자가 아님 |
| ERROR | PRICE_ZERO | 단가 0 이하 |
| ERROR | CARTON_SPEC / WEIGHT_LOGIC | 박스 입수 없음, 박스당 순중량 ≥ 총중량 |
| ERROR | QTY_CHANGED_NO_REASON | PI 수량 ≠ 선적 수량인데 사유 없음 |
| ERROR | LC_AMOUNT / LC_SHIPMENT_DATE / LC_EXPIRY / LC_PARTIAL | 신용장 한도·선적기한·유효기일·분할선적 위반 |
| ERROR | LC_CURRENCY / LC_EXPIRED / LC_EXPIRY_BEFORE_ETD | L/C 통화 ≠ 거래 통화, 서류 작성일에 이미 만료, 유효기일이 ETD보다 빠름 |
| WARNING | BANK_NOT_LOCKED / BANK_STALE | 계좌 확인 기록 없음, 마지막 확인 후 90일 경과 |
| WARNING | ETD_PAST | ETD가 서류 작성일보다 이전 |
| WARNING | INCOTERM_MODE | 해상 전용 규칙(FOB/CIF 등)을 항공 등에 사용 |
| WARNING | INCOTERM_PLACE_MISMATCH | 지정장소가 선적항(FAS/FOB) 또는 도착항·도착지(CFR/CIF/CPT/CIP/D조건)와 다름 |
| WARNING | FREIGHT_* / INSURANCE_* | 인코텀즈와 운임·보험료 포함 여부가 맞지 않음 |
| WARNING | DDP_RISK | DDP 사용 |
| WARNING | PARTIAL_CARTON | 마지막 박스가 덜 참(PL에는 포장재+내용물 추정 중량, 실측 필요) |
| WARNING | PRICE_DEVIATION | (수출) 단가가 마스터 대비 ±20% 초과 |
| WARNING | OVER_SHIPMENT | 계약보다 많이 선적 |
| WARNING | LC_MISSING / LC_TERMS_MISMATCH / LC_PRESENTATION / LC_BL_CONSIGNEE | L/C 결제인데 정보 없음(L/C·LC·Letter/Documentary Credit만 인식), lc는 있는데 결제조건에 L/C 없음, 서류 제시기간(presentation_days, 기본 21일) 촉박, B/L 수하인 문구 없음 |
| WARNING | PARTNER_CURRENCY / POD_MISSING / BENEFICIARY_NAME | 통화 불일치, 도착항 누락, 예금주명 불일치 |
| INFO | HS_CONFIRM / BANK_VERIFY / LC_DESCRIPTION / LC_CURRENCY / PI_CI_DIFF / QTY_CHANGED / INCOTERM_CONTAINER / ORIGIN_OVERRIDE / LINE_CURRENCY / PARTNER_TERMS_DIFF / POL_DEFAULT | 사람이 확인할 항목 안내 (LC_CURRENCY·LINE_CURRENCY는 ERROR가 아닌 경우의 안내) |

## 자동 계산 (서류 간 일치를 구조적으로 보장)
- 금액 = 수량 × 단가 (통화별 자릿수로 반올림: 기본 소수 둘째 자리, JPY·KRW 정수), 합계, 영문 금액(SAY ... ONLY)
- 박스 수 = ⌈수량 ÷ 박스당 입수⌉, 박스 번호는 품목 순서대로 연번
- 순중량 = 수량 × 개당 순중량, CBM = 박스 규격 × 박스 수
- 총중량 = 꽉 찬 박스 수 × 박스당 총중량 + (덜 찬 마지막 박스: 포장재 무게 + 남은 수량 × 개당 순중량)
  - 포장재 무게 = 박스당 총중량 − 입수 × 개당 순중량
- CI와 PL은 같은 선적 수량(shipped_qty)으로 계산하므로 수량이 어긋날 수 없습니다.

## --force로도 생성되지 않는 ERROR
ROLE_INVALID, INCOTERM_TYPE, LC_TYPE, ITEMS_TYPE, NUM_FORMAT, BANK_CHANGED, BANK_LOCK_BROKEN, PARTNER_ROLE

## 승인과 개정
- `approve`는 생성 후 deal·기준 데이터·서류 파일이 바뀌지 않았는지 해시로 확인한 뒤에만 승인합니다.
- 승인 후 다시 `generate`하면 서류마다 승인본과 셀 내용을 비교해, 같으면 승인본을 그대로 쓰고 다르면 개정판(-R1, -R2 ...)을 만듭니다.
- 입력이 승인 때와 완전히 같으면 아무것도 다시 만들지 않습니다.

## 에이전트가 사람 대신 판단하지 않는 것
- 공급처에서 받은 서류의 진위, 계좌 변경, HS코드 확정, 원산지 판정, 계약 조항 수용 여부
