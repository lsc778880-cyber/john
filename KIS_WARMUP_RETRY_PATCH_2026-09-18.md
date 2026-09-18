# KIS 모의투자 웜업 재시도 패치

- 작업일: 2026-09-18
- 대상: ZENITH DELTA KIS 모의투자 버전
- 증상: 600개 분봉 웜업 중 KIS REST 타임아웃이 발생하면 `0/600`부터 무제한 재시도

## 원인

1. `HistorySeedLoader` 지역 변수에만 성공 페이지를 보관해, 예외가 발생하면 이미 받은 102/203개 분봉이 전부 소실됨.
2. 웜업 실패 후 재시도 대기 중 메인 타이머가 REST 호가 폴링을 다시 시작함.
3. KIS 모의투자의 App Key당 REST 초당 1건 제한에서 웜업과 호가·계좌 조회가 경쟁함.
4. 재시도 횟수 상한이 없어 KIS 장애가 길어지면 60초 간격으로 무제한 반복함.

## 수정 내용

### 0. 2차 원인: 10초 REST 제한시간

1차 패치 적용 후 로그에서 체크포인트 `304/600`과 재개 로직은 정상 작동했지만, 재개 후 첫 REST 요청이 정확히 10초에서 다시 종료됨.

- KIS 모의 REST 기본 제한시간을 10초에서 30초로 확장.
- 잔고·미체결·계좌·주문가능 상위 타이머를 35초로 조정.
- REST 로컬 대기열에서 사용한 시간을 네트워크 제한시간에서 차감해, 전체 요청이 설정 시간의 두 배까지 늘어나지 않게 수정.
- `.env.kis.example`에 `KIS_REQUEST_TIMEOUT_SEC=30.0` 예시 추가.

### 1. 페이지 체크포인트 및 이어받기

- `HistorySeedLoader.load_seed_bars()`에 `initial_bars`, `checkpoint_cb` 인자를 추가함.
- 각 KIS 분봉 페이지를 성공적으로 파싱한 즉시 컨트롤러의 `_warmup_seed_checkpoint`에 저장함.
- 타임아웃 후 다음 재시도는 저장된 가장 오래된 분봉 시점부터 이전 페이지를 계속 조회함.
- 예: `203/600 -> timeout -> 203/600 resume -> 600/600`.
- 웜업이 완전히 성공한 뒤에만 체크포인트를 비움.

### 2. 웜업 REST 단독 구간 보장

- 웜업 중 호가 REST 구독을 중지함.
- 웜업 재시도 대기 중에도 호가 구독을 자동 복구하지 않음.
- 웜업/재시도 중에는 일반 타이머 기반 계좌 동기화도 건너뜀어 REST 대기열 경쟁을 줄임.
- 웜업 성공 또는 재시도 소진 후에만 호가 수신을 복구함.

### 3. 유한 재시도

`config.py`에 다음 설정을 추가함.

```python
WARMUP_RETRY_MAX_ATTEMPTS = 8
WARMUP_RETRY_BASE_DELAY_SEC = 5.0
WARMUP_RETRY_MAX_DELAY_SEC = 60.0
```

- 기본 백오프: 5, 10, 20, 40, 60초 후 60초 간격.
- 8회 재시도 소진 시 `RETRY_EXHAUSTED`를 남기고 `Warmup 재시도 중단 - 수동확인` 상태로 전환함.
- 웜업이 완료되지 않으면 기존처럼 자동 진입은 계속 차단됨.

## 변경 파일

- `core/history_seed_loader.py`
- `kis_main.py`
- `config.py`
- `tests/test_history_seed_loader.py`
- `infra/kis/settings.py`
- `infra/kis/rate_limit.py`
- `.env.kis.example`
- `tests/test_kis_rate_limit.py`
- `tests/test_kis_paper_adapter.py`

## 검증

실행 명령:

```text
python -m unittest tests.test_history_seed_loader tests.test_kis_rate_limit tests.test_kis_paper_adapter -v
python -m py_compile core/history_seed_loader.py infra/kis/settings.py infra/kis/rate_limit.py kis_main.py config.py
```

결과:

- 웜업, KIS REST rate-limit, KIS 모의 어댑터 테스트 25건 통과.
- Python 문법 검사 통과.

## 적용 주의사항

- 현재 실행 중인 Python 프로세스는 기존 코드를 메모리에 로드한 상태이므로, 패치 적용은 정상 종료 후 다음 실행부터 반영됨.
- 실행 중인 포지션이 있다면 임의로 종료하지 말고, 서버 잔고와 미체결 상태를 먼저 확인해야 함.
- App Key, App Secret, 계좌번호, 텔레그램 토큰 및 실행 로그는 저장소에 포함하지 않음.
