# PC 적용 지시문: 텔레그램 복원 + KIS 웜업 재시도 패치

## 적용 범위

대상 PC에는 금일 텔레그램 관련 수정과 웜업 재시도 패치가 모두 적용되지 않은 상태이다.

따라서 다음 두 가지 변경을 모두 적용한다.

1. 텔레그램 진입유형·진입사유 복원
2. KIS 웜업 체크포인트·이어받기·유한 재시도

GitHub `john` 저장소의 다음 소스 커밋은 두 변경이 모두 포함된 PC 운영 파일을 담고 있다.

- 패치 커밋: `c22e9662c1cfcf993b58025e10b188d7b5706b58`
- 커밋 제목: `Fix resumable KIS warmup retries`

## 운영 파일

다음 3개 운영 파일을 대상 PC에 병합한다. `kis_main.py`에는 텔레그램 복원과 웜업 제어 변경이 모두 포함되어 있다.

1. `kis_main.py`
2. `config.py`
3. `core/history_seed_loader.py`

`tests/test_history_seed_loader.py`는 회귀 테스트용이며 실제 운영 실행에는 필수가 아니다.

## 필수 지시사항

1. 텔레그램 진입 보고에 `진입유형=추세형 (TREND)` 또는 `진입유형=반전형 (REVERSAL)`을 표시할 것.
2. KIS 잔고로 진입 체결을 확인해도 `KIS_BALANCE_CONFIRMED`를 진입사유로 보내지 말고, 주문 감사 정보와 제출 스냅샷에서 원래 전략 트리거를 복원할 것.
3. 웜업 패치는 커밋 `c22e966`의 `kis_main.py`, `config.py`, `core/history_seed_loader.py` 변경을 모두 적용할 것.
4. App Key, App Secret, 계좌번호, 텔레그램 토큰, `.env.kis.local`, 실행 로그를 GitHub에 올리거나 변경하지 말 것.
5. 웜업 중 KIS REST 호가와 일반 타이머 계좌 동기화가 재시작되지 않는지 확인할 것.
6. 타임아웃 후 이미 받은 분봉을 유지하고, 다음 재시도가 저장된 지점부터 재개되는지 확인할 것.
7. 재시도는 기본 8회로 제한하고, 소진 시 자동진입 차단을 유지한 채 `Warmup 재시도 중단 - 수동확인` 상태로 멈출 것.

## 검증 명령

```text
python -m unittest tests.test_history_seed_loader tests.test_kis_rate_limit -v
python -m py_compile core/history_seed_loader.py kis_main.py config.py
```

기대 결과:

- 웜업 로더 및 KIS 호출 제한 테스트 9건 통과
- Python 문법 검사 통과
- 텔레그램 진입 알림에 진입유형(TREND/REVERSAL) 표시
- 텔레그램 진입사유에 브로커 확인 문구가 아닌 원래 전략 트리거 표시

## 재시작 절차

1. 현재 포지션과 미체결 주문이 없는지 먼저 확인한다.
2. 운영 프로그램을 정상 종료한다.
3. 텔레그램 복원과 웜업 패치를 모두 적용하고 위 검증 명령을 실행한다.
4. 프로그램을 재실행한다.
5. 로그에서 `WARMUP CHECKPOINT`, `KIS_BARS_RESUME`, `WARMUP DONE` 순서를 확인한다.
6. 웜업 완료 전에는 자동진입을 활성화하지 않는다.

## 완료 보고 항목

- 적용한 커밋 SHA
- 변경한 파일 목록
- 테스트 결과
- 재실행 후 웜업 완료 여부
- 텔레그램 진입유형·원래 진입사유 표시 적용 여부
