# PC 적용 지시문: KIS 웜업 재시도 패치

## 적용 범위

금일 적용한 텔레그램 관련 수정은 그대로 유지한다.

텔레그램 작업 외에 추가로 적용할 변경은 GitHub `john` 저장소의 웜업 재시도 패치 커밋으로 한정한다.

- 패치 커밋: `c22e9662c1cfcf993b58025e10b188d7b5706b58`
- 커밋 제목: `Fix resumable KIS warmup retries`

## 운영 파일

다음 3개 파일의 웜업 관련 변경을 적용한다.

1. `kis_main.py`
2. `config.py`
3. `core/history_seed_loader.py`

`tests/test_history_seed_loader.py`는 회귀 테스트용이며 실제 운영 실행에는 필수가 아니다.

## 필수 지시사항

1. 현재 PC에 반영된 텔레그램 진입유형·진입사유 복원 로직을 삭제하거나 되돌리지 말 것.
2. 이전 버전 파일 전체를 무조건 덮어쓰지 말고, 커밋 `c22e966` 변경사항만 병합할 것.
3. App Key, App Secret, 계좌번호, 텔레그램 토큰, `.env.kis.local`, 실행 로그를 GitHub에 올리거나 변경하지 말 것.
4. 웜업 중 KIS REST 호가와 일반 타이머 계좌 동기화가 재시작되지 않는지 확인할 것.
5. 타임아웃 후 이미 받은 분봉을 유지하고, 다음 재시도가 저장된 지점부터 재개되는지 확인할 것.
6. 재시도는 기본 8회로 제한하고, 소진 시 자동진입 차단을 유지한 채 `Warmup 재시도 중단 - 수동확인` 상태로 멈출 것.

## 검증 명령

```text
python -m unittest tests.test_history_seed_loader tests.test_kis_rate_limit -v
python -m py_compile core/history_seed_loader.py kis_main.py config.py
```

기대 결과:

- 웜업 로더 및 KIS 호출 제한 테스트 9건 통과
- Python 문법 검사 통과
- 텔레그램 진입유형·진입사유 표시 유지

## 재시작 절차

1. 현재 포지션과 미체결 주문이 없는지 먼저 확인한다.
2. 운영 프로그램을 정상 종료한다.
3. 패치를 적용하고 위 검증 명령을 실행한다.
4. 프로그램을 재실행한다.
5. 로그에서 `WARMUP CHECKPOINT`, `KIS_BARS_RESUME`, `WARMUP DONE` 순서를 확인한다.
6. 웜업 완료 전에는 자동진입을 활성화하지 않는다.

## 완료 보고 항목

- 적용한 커밋 SHA
- 변경한 파일 목록
- 테스트 결과
- 재실행 후 웜업 완료 여부
- 텔레그램 진입유형·진입사유 표시 유지 여부
