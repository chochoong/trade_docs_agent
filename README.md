# 무역서류 에이전트 (trade-docs-agent)

1인 무역용 AI 에이전트 시스템 중 **6번 무역서류 에이전트**를 담은 프로젝트 폴더입니다.
거래 정보 하나(deal JSON)로 PI, CI, PL, S/I(수출) 또는 PO(수입)를 엑셀과 PDF로 만듭니다.
서류끼리 숫자와 조건이 맞는지 검증하고, 통관·재무 에이전트에 넘길 manifest를 남깁니다.

## 빠른 시작
```bash
pip install -r requirements.txt          # openpyxl
# (PDF가 필요하면 LibreOffice 설치)
# Windows에서는 아래 명령의 python3를 python으로 바꿔 실행하세요.

python3 scripts/tradedocs.py validate deals/D2026-001.json
python3 scripts/tradedocs.py generate deals/D2026-001.json --pdf
# → output/D2026-001/ 에 PI·CI·PL·SI(xlsx/pdf), validation.md, manifest.json (status: needs_human_review)
python3 scripts/tradedocs.py approve deals/D2026-001.json    # 본인이 검토 후 직접 실행 → status: approved
```

승인한 뒤 deal을 고쳐 다시 `generate`하면 내용이 바뀐 서류만 개정판(`CI-D2026-001-R1` 등)으로 만들어지고,
다시 승인해야 합니다. 바뀌지 않은 서류는 승인본을 그대로 씁니다.

## 처음 할 일
1. `data/company.json`: 자사 정보와 은행 계좌를 입력합니다.
   계좌를 확인했으면 **본인이 터미널에서 직접** `python scripts/tradedocs.py bank-verify`를 실행해 확인 기록을 남깁니다.
   이후 계좌 정보가 바뀌면 다시 확인할 때까지 서류 생성이 막힙니다(계좌 변경 사기 방지).
2. `data/partners.csv`: 거래처를 입력합니다(엑셀로 열어서 편집해도 됩니다. 저장할 때 'CSV UTF-8(쉼표로 분리)' 형식으로 저장하세요. 일반 CSV(cp949)로 저장해도 읽지만 경고가 나옵니다).
3. `data/products.csv`: 품목, HS코드(후보값), 단가, 박스 입수, 중량, 박스 규격을 입력합니다.
4. `deals/_template.json`을 복사해서 거래 1건을 작성한 뒤 `generate`를 실행합니다.

샘플 deal 3개:
- `D2026-001`: FOB 수출. 오류 없이 생성되고, 계약보다 적게 선적한 품목이 포함되어 있습니다.
- `D2026-002`: CIF + L/C 수출. 일부러 문제를 넣어 두어서 검증이 생성을 막습니다.
- `D2026-003`: 수입 발주서(PO)입니다.

## AI 에이전트로 쓰기
- **Claude Code / Claude 데스크톱**: 이 폴더를 열면 `CLAUDE.md` → `AGENTS.md` 지침을 따릅니다.
  예: "B001 바이어에게 텀블러 960개, 도마세트 240세트 FOB 부산으로 PI 만들어줘"
- **다른 에이전트·자동화 도구**: `interfaces/README.md`의 JSON 이벤트 형식으로 연결합니다.
  ```bash
  python3 scripts/ingest.py interfaces/inbound/order_confirmed.example.json --generate
  ```

## 구조
```
trade-docs-agent/
├── AGENTS.md / CLAUDE.md     에이전트 지침
├── data/                     기준 데이터 (자사·거래처·품목·기본값)
├── knowledge/                인코텀즈, 결제조건·L/C, 계약 조항, 검증 규칙
├── schemas/                  deal(입력) / manifest(출력) JSON 스키마
├── interfaces/               다른 에이전트와 연결하는 방법 + inbound/outbound 예시
├── deals/                    거래별 입력 JSON
├── scripts/tradedocs.py      생성·검증 CLI
├── scripts/ingest.py         inbound 이벤트 → deal 반영
└── output/<deal_id>/         결과물
```

## 주의
생성된 서류의 상태는 항상 `needs_human_review`입니다. 금액, 계좌, HS코드, 원산지, 계약 조항은
사람이 확인한 뒤 발송하세요. HS코드 확정은 관세사에게, 운송은 포워더에게 맡깁니다.
