# -*- coding: utf-8 -*-
import os


class Config:
    # KIS paper-only broker configuration. Credentials and the active contract
    # are supplied through environment variables, never committed to source.
    BROKER_PROVIDER = "KIS"
    KIS_PAPER_ADAPTER_ENABLED = True
    KIS_PAPER_PRODUCT_CODE = "03"
    # Shared live/tick-replay entry rule: 10-second reversal and one-minute
    # trend SMAs; their ARM/FIRE sequences remain independent.
    USE_SMA_CROSS_ENTRY = True
    # Tick-driven 10-second reversal candle. The forming close participates on
    # every tick; completed KIS 1m candles provide approximate warmup history.
    SMA_REVERSAL_BAR_SECONDS = 10
    # FIRE remains visible briefly so a
    # tick-level cross cannot disappear between dashboard refreshes.
    SMA_CROSS_FIRE_HOLD_SEC = 2.0
    # Trend lane: 1-minute SMA5/SMA10 versus SMA20. Touch SMA20 to ARM, then
    # FIRE inside the inclusive +/-0.3~0.5 recovery band.
    SMA_TREND_SLOPE_BARS = 3
    SMA_TREND_ARM_TIMEOUT_SEC = 60.0
    SMA_TREND_1M_FIRE_MIN_PT = 0.30
    SMA_TREND_1M_FIRE_MAX_PT = 0.50
    # Retained only for old report/config readers; no longer drives trend entry.
    SMA_TREND_20_DEVIATION_PT = 0.20
    SYMBOL_CODE = os.environ.get("KIS_FUTURES_SYMBOL", "A05610").strip()
    # Historical KIS tick-cache/backfill defaults.
    TICK_BACKFILL_TR_CODE = "KIS_TICK_DATA"
    TICK_BACKFILL_RQNAME = "REQ_KIS_TICK_BACKFILL"
    TICK_BACKFILL_SCREEN = "8508"
    TICK_BACKFILL_UNIT = "1"
    TICK_BACKFILL_DAYS = 5
    TICK_BACKFILL_OUTPUT_DIR = "data/ticks"
    TICK_BACKFILL_PAGE_DELAY_MS = 380
    TICK_BACKFILL_MAX_PAGES = 1200
    TICK_BACKFILL_USE_COMM_DATA_EX = True
    TICK_BACKFILL_KEEP_IDENTICAL_TICKS = True

    # Historical tick-replay backtest. The tested market path must come only
    # from cached KIS ticks; the 1-minute file is indicator warmup only.
    TICK_REPLAY_ENGINE = "HISTORICAL_TICK_REPLAY"
    # Tick replay runtime reads these values directly from the same live Config.
    # 0 / 0.0 / False are valid values and must never be replaced by defaults.
    TICK_REPLAY_TRADING_DAYS = 5
    # Backtests are offline-first: reuse the local minute/tick cache and never
    # contact KIS unless AUTO/FORCE is explicitly requested on the command line.
    TICK_REPLAY_CACHE_MODE = "REUSE"  # REUSE (default) / AUTO / FORCE
    TICK_REPLAY_CACHE_DIR = "data/ticks"
    # KIS current-day tick cache is considered complete after the derivatives
    # session data end. This is intentionally separate from EOD forced-flat.
    TICK_REPLAY_SESSION_COMPLETE_TIME = "15:45"
    TICK_REPLAY_EVENT_LOG_LEVEL = "COMPACT"
    TICK_REPLAY_REQUIRE_CONFIG_SYMBOL_MATCH = True
    TICK_REPLAY_CONFIG_SYNC_PATCH_TAG = "CONFIG_SYNC_ZERO_VALID_20260712"
    TICK_REPLAY_REQUIRE_EXACT_SEQUENCE = True
    TICK_REPLAY_ALLOW_COMPRESSION = False
    TICK_REPLAY_FILL_POLICY = "CURRENT_TICK_TOUCH"
    # Backtest data-size standard: replay every cached KIS tick through
    # the session end. A zero cap means no per-day truncation. The 1-minute
    # warmup budget remains independent and follows the live dynamic 140~600
    # row calculation below.
    TICK_REPLAY_MAX_TICKS_PER_DAY = 0
    TICK_REPLAY_TICK_LIMIT_POLICY = "FULL_SESSION"
    TICK_REPLAY_USE_LIVE_DYNAMIC_WARMUP = True
    TICK_REPLAY_DATA_LIMIT_PATCH_TAG = "TICK_FULL_SESSION_WARMUP600_LIVE_DYNAMIC_20260713"
    # Tick replay/live indicator performance. These paths preserve the same
    # candle, EMA, MACD and MFE results; set False only for rollback diagnosis.
    USE_INCREMENTAL_LATEST_MINUTE_UPDATE = True
    TICK_REPLAY_BULK_WARMUP = True
    MFE_USE_POSITION_META_EXTREMA = True
    TICK_REPLAY_PERFORMANCE_PATCH_TAG = "TICK_FAST_EXACT_V1_20260712"
    DASHBOARD_EXPIRY_YEAR_MONTH = "2026-10"
    KRX_HOLIDAYS_2026 = {
        "2026-01-01",  # New Year's Day
        "2026-02-16",  # Seollal
        "2026-02-17",  # Seollal
        "2026-02-18",  # Seollal
        "2026-03-02",  # Independence Movement Day substitute
        "2026-05-01",  # Labor Day / 근로자의 날
        "2026-05-05",  # Children's Day
        "2026-05-25",  # Buddha's Birthday substitute
        "2026-06-03",  # Local Election Day
        "2026-08-17",  # Liberation Day substitute
        "2026-09-24",  # Chuseok
        "2026-09-25",  # Chuseok
        "2026-09-28",  # Chuseok substitute
        "2026-10-05",  # National Foundation Day substitute
        "2026-10-09",  # Hangul Day
        "2026-12-25",  # Christmas
        "2026-12-31",  # Year-end market holiday
    }
    
    # ===== core parameters =====
    LOOKBACK = 20           
    EXIT_Z = 0.50
    MA_FAST = 10            
    MA_SLOW = 30
    WARM_UP_MIN = 30
    
    # [하이브리드 필터]
    # 이격도 기준을 낮춰 장세 전환 민감도 소폭 상향
    REGIME_THRESHOLD = 0.5
    # 레짐 판정 전용 갭 보정(진입/청산 Z 로직에는 미적용)
    USE_GAP_REGIME_ADJUST = False
    GAP_REGIME_ADJUST_MIN_PT = 15.0
    GAP_REGIME_ADJUST_FACTOR = 0.6
    GAP_REGIME_ADJUST_MAX_PT = 20.0
    # 레짐 판정 기준을 당일 08:45(anchor) 대비 변화량으로 전환 (갭보정보다 우선)
    USE_REGIME_ANCHOR_0845 = False
    REGIME_ANCHOR_TIME = "08:45"
    # Dashboard/session open follows the 08:45 session anchor.
    SESSION_OPEN_TIME = "08:45"
    # Live / backtest regime display and regime-driven logic master toggles.
    USE_REGIME_FILTER = False
    BT_USE_REGIME_FILTER = False

    # [양방향 설정]
    ENABLE_SHORT = True
    # [진입 설정]
    # Active stateful delta-entry source.
    ENTRY_SIGNAL_SOURCE = "z1"
    # Common live entry mode:
    # ON here: live uses a mixed recent-N 1-minute anchor.
    # - completed bars contribute confirmed closes only
    # - the current forming bar contributes only its live low/high extrema
    # This lets the anchor update only when the current bar makes a new
    # low/high, instead of following every intrabar last-price tick.
    USE_PRICE_EXTREMA_ENTRY = False
    PRICE_EXTREMA_ANCHOR_SOURCE = "close_1m_confirmed"
    # REVERSAL uses the latest five completed 1m actual highs/lows, including
    # wicks. After LOW touch, a qualified bullish candle arms LONG at its actual
    # high. After HIGH touch, a qualified bearish candle arms SHORT at its actual
    # low. TREND keeps its separate confirmed-body workflow.
    REVERSAL_CONFIRMED_EXTREMA_SOURCE = "high_low_1m"
    # Do not require a separate intrabar new-low/new-high prime event, because
    # the current bar's low/high is already reflected in the live anchor.
    REVERSAL_REQUIRE_EXTREMA_PRIME = False
    # If both reversal LONG and reversal SHORT FIRE are true on the same confirmed bar,
    # treat the overlap as a no-trade bottleneck. This prevents the LONG-priority
    # fallback from entering ambiguous BAND1 ranges in live/backtest.
    REVERSAL_BLOCK_BOTH_FIRE = True
    PRICE_EXTREMA_LOOKBACK_BARS = 5
    TREND_PRICE_EXTREMA_SOURCE = "OC_BODY_HIGH_LOW"
    TREND_PRICE_EXTREMA_LOOKBACK_BARS = 3
    REVERSAL_ARM_AFTER_CONFIRMED_EXTREMA_TOUCH = False
    TREND_ARM_AFTER_CONFIRMED_EXTREMA_TOUCH = True
    # ===== Z5-band ENTRY regime router =====
    # REVERSAL: |Z5| <= 1.40
    # TREND:    1.40 < |Z5| <= 3.80 (REVERSAL interval is excluded)
    # BLOCKED:  |Z5| > 3.80
    USE_HYBRID_ENTRY_REGIME = False
    # True: 추세형 Z5 구간에는 REVERSAL 진입 규칙, 반전형 Z5 구간에는 TREND 진입 규칙 적용
    SWAP_ENTRY_REGIME_RULES = False
    # Apply the TREND candle/ARM/FIRE profile inside the physical REVERSAL Z5
    # band as well. The physical band remains available in audit fields.
    USE_TREND_RULES_IN_REVERSAL_Z5_ZONE = False
    ENTRY_REGIME_REVERSAL_Z5_ABS_MIN = 0.00
    ENTRY_REGIME_REVERSAL_Z5_ABS_MAX = 1.40
    ENTRY_REGIME_TREND_Z5_ABS_MIN = 1.40
    ENTRY_REGIME_TREND_Z5_ABS_MAX = 3.80
    ENTRY_COMMON_Z5_ABS_MAX = 3.80
    USE_Z5_ENTRY_OUTER_CAP = False
    Z5_REGIME_BAND_PATCH_TAG = "REV_LE_1P40_TREND_GT_1P40_TO_3P80_20260802"
    # Trend-zone opposite-direction entry block patch v9 master toggle.
    USE_TREND_Z5_ZONE_OPPOSITE_ENTRY_BLOCK_V9 = False

    # REVERSAL rule profile (SWAP ON이면 TREND Z5 구간에서 실행):
    # Touch preceding five completed 1m actual H/L (wicks included).
    # LONG : LOW touch + valid bullish candle -> that candle actual high.
    # SHORT: HIGH touch + valid bearish candle -> that candle actual low.
    REVERSAL_PRICE_EXTREMA_OFFSET_PT = 0.00
    REVERSAL_PRICE_FIRE_BAND_MIN_PT = 0.30
    REVERSAL_PRICE_FIRE_BAND_MAX_PT = 0.70
    # Keep the shared switch ON so the REVERSAL FORWARD condition below is
    # effective. TREND remains OFF through its own condition-mode setting.
    ENTRY_PREV_VALID_CANDLE_COLOR_FILTER_ENABLED = False
    # FORWARD = bullish->LONG / bearish->SHORT
    # INVERSE = bearish->LONG / bullish->SHORT
    # BLOCK_OPPOSITE_CONSECUTIVE_3 = latest 3 completed 1m candles:
    #   3 bullish blocks SHORT only; 3 bearish blocks LONG only
    # BLOCK_OPPOSITE_MAJORITY_3 = >=2 bullish blocks SHORT;
    #   >=2 bearish blocks LONG. Doji is not counted.
    # BLOCK_OPPOSITE_LAST_2 = latest two completed candles:
    #   bullish+bullish blocks SHORT; bearish+bearish blocks LONG.
    # OFF     = no candle-direction condition
    REVERSAL_PREV_CANDLE_CONDITION_MODE = "FORWARD"
    REVERSAL_PREV_CANDLE_LOOKBACK_BARS = 1
    # Shared valid-candle body threshold. REVERSAL requires the post-touch
    # forward-direction candle; TREND uses it for its touched confirmation bar.
    ENTRY_PREV_CANDLE_MIN_RANGE_PT = 0.6
    # Latest completed qualified 1m candle blocks only the opposite entry:
    # bearish -> block LONG, bullish -> block SHORT. SMALL/DOJI remains allowed.
    USE_PREV_CANDLE_OPPOSITE_ENTRY_BLOCK = False
    # Timed wait/watch is disabled. A qualified previous-close ARM starts FIRE
    # monitoring immediately and has no timed expiry.
    REVERSAL_USE_15S_EXTREMA = True
    REVERSAL_ARM_ROLLING_15S_ENABLED = False
    REVERSAL_ARM_WAIT_WATCH_ENABLED = False
    ENTRY_STAGE_WAIT_SEC = 0.0
    REVERSAL_CANDIDATE_TIMEOUT_SEC = 0.0

    # TREND rule profile: touch preceding three completed 1m O/C-body H/L.
    # HIGH touch + valid-body confirmation -> LONG ARM at that candle's body high.
    # LOW touch + valid-body confirmation -> SHORT ARM at that candle's body low.
    # The ARM stays fixed; 15-second rolling is disabled in this profile.
    # Optional previous-completed-1m candle condition for TREND entry:
    # FORWARD = bullish->LONG / bearish->SHORT
    # INVERSE = bearish->LONG / bullish->SHORT
    # REQUIRE_SAME_DIRECTION_2 = latest 2 completed candles must both match entry:
    #   LONG requires 2 bullish; SHORT requires 2 bearish. Mixed/doji blocks.
    # REQUIRE_SAME_DIRECTION_3 = latest 3 completed candles must all match entry:
    #   LONG requires 3 bullish; SHORT requires 3 bearish. Mixed/doji blocks.
    # REQUIRE_MAJORITY_DIRECTION_3 = exact 2:1 split across latest 3 completed bars:
    #   LONG requires 2 bullish + 1 bearish; SHORT requires 2 bearish + 1 bullish.
    #   All-same or any doji blocks TREND.
    # OFF     = no candle-direction condition
    # This only gates TREND ARM; completed-15s rolling remains unchanged.
    # TREND uses a 5s ARM wait followed by a 3s FIRE-watch window.
    TREND_PRICE_EXTREMA_OFFSET_PT = 0.00
    # Closed TREND FIRE band from the stored ARM price:
    # LONG = ARM +0.00 .. +0.10 / SHORT = ARM -0.10 .. -0.00.
    TREND_PRICE_FIRE_BAND_MIN_PT = 0.00
    TREND_PRICE_FIRE_BAND_MAX_PT = 0.10
    TREND_PREV_CANDLE_CONDITION_MODE = "OFF"
    TREND_PREV_CANDLE_LOOKBACK_BARS = 2
    # The physical REVERSAL Z5 band uses the TREND profile without its
    # previous-candle direction prerequisite.
    REVERSAL_ZONE_TREND_PREV_CANDLE_CONDITION_MODE = "OFF"
    # Legacy four-consecutive-candle exhaustion gate.
    TREND_BLOCK_CONSECUTIVE_SAME_4_ENABLED = False
    # Block only the matching TREND entry direction when at least four of the
    # latest five completed 1m candles are qualified forward candles.
    TREND_BLOCK_FORWARD_4_OF_5_ENABLED = False
    # Two same-direction completed candles promote TREND across the full
    # shared Z5 band. Without that candle prerequisite, physical Z5 routing remains.
    TREND_CANDLE3_Z5_BAND_OVERRIDE_ENABLED = False
    # Legacy compatibility mirror. CONDITION_MODE takes precedence.
    TREND_PREV_CANDLE_PREREQUISITE_ENABLED = False
    # Keep the confirmed three-body H/L ARM fixed through FIRE evaluation.
    TREND_ARM_ROLLING_15S_ENABLED = False
    # Timed wait/watch is disabled: after ARM, FIRE monitoring starts without
    # a timed validation window. Rolling ARM remains independently enabled.
    TREND_ARM_WAIT_WATCH_ENABLED = False
    TREND_ENTRY_STAGE_WAIT_SEC = 0.0
    TREND_CANDIDATE_TIMEOUT_SEC = 0.0
    # No additional FIRE confirmation delay.
    TREND_FIRE_CONFIRM_MIN_SEC = 0.0
    TREND_FIRE_CONFIRM_TIMEOUT_SEC = 3.0

    # Legacy active values are kept equal to BAND 1 defaults for rollback/import compatibility.
    PRICE_EXTREMA_OFFSET_PT = REVERSAL_PRICE_EXTREMA_OFFSET_PT
    PRICE_FIRE_BAND_MIN_PT = REVERSAL_PRICE_FIRE_BAND_MIN_PT
    PRICE_FIRE_BAND_MAX_PT = REVERSAL_PRICE_FIRE_BAND_MAX_PT
    # Legacy flag name kept for compatibility; in price-extrema mode this only enables
    # the ARM/FIRE latch. It must not make ENTRY depend on dz5/Z5.
    USE_DZ5_ARM_ENTRY = False
    # Two-step price ARM -> ORDER entry:
    # LONG  arms when dz5_long crosses +0.65, fires when dz5_long crosses +0.85.
    # SHORT arms when dz5_short crosses -0.65, fires when dz5_short crosses -0.85.
    SWAP_ENTRY_ARM_BAND_SIDE = False
    # Reconnect/live continuity bootstrap bars for dz5 ARM.
    # During bootstrap, ARM creation is blocked and prev_z5 continuity is rebuilt from fresh live bars only.
    DZ5_LIVE_BOOTSTRAP_BARS = 2
    USE_DZ5_SESSION_LOW_ANCHOR = False
    # 20-bar z5 extrema reversal entry.  Keep the 20-bar basis; these
    # thresholds mirror strategy.py and dashboard/report display.
    DZ5_ARM_LONG = -0.15      # LONG ARM: current z5 vs previous-N low <= -0.15
    DZ5_ENTRY_LONG = 0.30     # LONG FIRE: armed low-anchor rebound >= +0.30
    DZ5_ARM_LONG_MAX = DZ5_ENTRY_LONG   # legacy alias: long fire threshold
    DZ5_ARM_SHORT = 0.15      # SHORT ARM: current z5 vs previous-N high >= +0.15
    DZ5_ENTRY_SHORT = -0.30   # SHORT FIRE: armed high-anchor retrace <= -0.30
    DZ5_ARM_SHORT_MIN = DZ5_ENTRY_SHORT # legacy alias: short fire threshold
    DZ5_ARM_EXPIRY_BARS = 5
    # Entry dz5 anchor window (previous completed bars).
    DZ5_ENTRY_EXTREMA_LOOKBACK_BARS = 20
    # Recent N-bar price GAP gate for entry:
    # require (recentN high - recentN low) >= ENTRY_PRICE_RANGE_MIN_PT
    USE_ENTRY_PRICE_RANGE_FILTER = False
    ENTRY_PRICE_RANGE_LOOKBACK_BARS = 5
    ENTRY_PRICE_RANGE_MIN_PT = 3.5
    # After warmup/history seed, require this many freshly formed live 1m bars
    # before allowing any new ENTRY / REVERSE order.
    POST_WARMUP_ENTRY_DELAY_BARS = 0
    # Startup entry gate: block new entry monitoring/execution for the first N seconds
    # after program launch.
    # 3-second/startup wait is disabled in this profile. Broker position and
    # unfilled-order prechecks remain independent hard gates.
    STARTUP_ENTRY_DELAY_SEC = 0.0
    # dz5 prev-value commit timing:
    # - BAR_CLOSE: commit prev_z5 only on confirmed minute close (legacy live behavior)
    # - EVERY_UPDATE: commit prev_z5 on every intraminute update (backtest OHLC4-like behavior)
    DZ5_PREV_COMMIT_MODE = "BAR_CLOSE"
    # Legacy one-step Z5 delta threshold is disabled. The stateful Z5
    # continuation sequence below is the active entry path.
    USE_PREV_Z5_DELTA_ENTRY = False
    PREV_Z5_DELTA_ENTRY = 0.5
    # Direct Z1-delta entry path. Compare current realtime Z1 with the Z1
    # committed at the previous confirmed 1-minute evaluation:
    # delta >= +0.8 -> BUY, delta <= -0.8 -> SELL_SHORT.
    USE_PREV_Z1_DELTA_ENTRY = False
    PREV_Z1_DELTA_ENTRY = 0.8
    # One-minute SMA owns actual ENTRY. Z1 delta thresholds are retained only
    # for compatibility/report readers and do not run an ARM/FIRE monitor.
    USE_Z1_DELTA_REVERSAL_ENTRY = False
    MONITOR_Z1_DELTA_CONTINUATION = False
    Z1_DELTA_ENTRY_MODE = "CONTINUATION"
    USE_DELTA_TWO_TOUCH_ARM = True
    Z1_DELTA_REVERSAL_PREHIT = 0.4
    Z1_DELTA_REVERSAL_ARM = 0.2
    # Z1 DELTA indices: ARM +/-0.20, LONG FIRE +0.10~+1.00,
    # SHORT FIRE -1.00~-0.10.
    Z1_DELTA_REVERSAL_FIRE_MIN = 0.1
    Z1_DELTA_REVERSAL_FIRE_MAX = 1.0
    Z1_DELTA_REVERSAL_FIRE = Z1_DELTA_REVERSAL_FIRE_MAX  # compatibility alias
    # Optional four-way Z1 simulation. The live profile keeps this OFF until
    # the reversal/trend split is explicitly promoted after validation.
    USE_Z1_DELTA_DUAL_REGIME_ENTRY = False
    Z1_DELTA_TREND_ARM = 0.2
    Z1_DELTA_TREND_FIRE = 0.4
    # Current-tick Z5 delta continuation: LONG +0.20 ARM -> +0.10~+1.00 FIRE;
    # SHORT -0.20 ARM -> -1.00~-0.10 FIRE. Boundaries are inclusive and
    # ARM/FIRE require separate evaluations.
    USE_Z5_DELTA_REVERSAL_ENTRY = False
    # Keep the Z5 ARM/FIRE state machine alive for dashboard selection even
    # while Z1 remains the only live order source.
    MONITOR_Z5_DELTA_CONTINUATION = True
    Z5_DELTA_ENTRY_MODE = "CONTINUATION"
    Z5_DELTA_REVERSAL_PREHIT = Z1_DELTA_REVERSAL_PREHIT
    Z5_DELTA_REVERSAL_ARM = 0.2
    Z5_DELTA_REVERSAL_FIRE_MIN = 0.1
    Z5_DELTA_REVERSAL_FIRE_MAX = 1.0
    Z5_DELTA_REVERSAL_FIRE = Z5_DELTA_REVERSAL_FIRE_MAX  # compatibility alias
    # Legacy reversal-mode dashboard thresholds. Z5 continuation mode instead
    # mirrors its authoritative +0.20/-0.20 ARM latch directly.
    DELTA_BLINK_PREHIT = 0.4
    DELTA_BLINK_ARM = 0.20
    # Keep a one-tick FIRE visible long enough for at least two 700ms blinks.
    DELTA_FIRE_BLINK_HOLD_SEC = 1.6
    Z5_DELTA_BLINK_PREHIT = Z5_DELTA_REVERSAL_PREHIT
    Z5_DELTA_BLINK_ARM = 0.20
    # Additional z5 range gates for entry (applied with delta rule as AND):
    # LONG  when LONG_ENTRY_Z5_MIN < z5 < LONG_ENTRY_Z5_MAX
    # SHORT when SHORT_ENTRY_Z5_MIN < z5 < SHORT_ENTRY_Z5_MAX
    # Swapped profile: LONG takes former SHORT positive band, SHORT takes former LONG negative band.
    LONG_ENTRY_Z5_MIN = -1.35
    LONG_ENTRY_Z5_MAX = 1.35
    SHORT_ENTRY_Z5_MIN = -1.35
    SHORT_ENTRY_Z5_MAX = 1.35
    USE_Z5_ENTRY_DEADZONE = False
    Z5_ENTRY_DEADZONE_ABS = 0.0
    Z5_LONG_BAND_LOW = LONG_ENTRY_Z5_MIN
    Z5_LONG_BAND_HIGH = LONG_ENTRY_Z5_MAX
    Z5_SHORT_BAND_LOW = SHORT_ENTRY_Z5_MIN
    Z5_SHORT_BAND_HIGH = SHORT_ENTRY_Z5_MAX
    # Use Z5 BAND as an additional entry gate even in price-extrema mode.
    # Legacy static-band compatibility values; hybrid routing above owns the active regime.
    USE_STATIC_Z5_ENTRY_BAND = False
    USE_Z5_ENTRY_BAND_FILTER = False
    Z5_BAND_PROFILE_PATCH_TAG = "Z5_FORCE_ACTIVE_20260601"
    # When OFF, entry does not require anchor polarity across 0-axis
    # (LONG anchor<0 / SHORT anchor>0).
    USE_DZ5_ANCHOR_POLARITY_FILTER = False
    # EMA20 price-position entry filter is disabled.
    # LONG/SHORT entry gates use ARM + BAND; EMA is OFF.
    USE_EMA20_ENTRY_FILTER = False
    # Legacy compatibility value; current EMA20 entry filter does not use a gap.
    EMA_CROSS_GAP_PT = 0.15
    # Legacy diagnostic boost for z5; forced OFF and not used by ENTRY/EXIT.
    USE_Z5_REALTIME_BLEND = False
    Z5_REALTIME_BLEND_WEIGHT = 0.50
    # EXIT_Z is price/retrace based only. Do not restore Z1/Z5-based EXIT_Z.
    EXIT_Z_SIGNAL_SOURCE = "price"
    # EXIT_Z price retrace threshold. Z5/entry_z5/current_z5 are ignored.
    USE_EXIT_Z = False
    # When True, EXIT_Z is temporarily blocked while MFE retrace is armed.
    EXIT_Z_SKIP_WHEN_MFE_ARMED = False
    EXIT_Z_LONG = 2.5
    EXIT_Z_SHORT = -2.5
    # EXIT_Z loss gate:
    # When enabled, EXIT_Z can trigger only if current PnL is at or below EXIT_Z_MAX_PNL_PT.
    # 0.0 means loss-only gate (profit zone blocks EXIT_Z).
    USE_EXIT_Z_PNL_GATE = False
    EXIT_Z_MAX_PNL_PT = 0.0
    # Peak/trough retrace delta for EXIT_Z linked to live MFE tracking.
    # LONG  triggers when current_price <= peak_price   - EXIT_Z_RETRACE_DELTA
    # SHORT triggers when current_price >= trough_price + EXIT_Z_RETRACE_DELTA
    # while MFE_TRAIL has not triggered yet.
    EXIT_Z_RETRACE_DELTA = 3.0
    # Deprecated Z5 anchored EXIT_Z flags kept only for import compatibility.
    # The live/backtest EXIT_Z path below ignores them.
    USE_EXIT_Z_TAKE_PROFIT_ENTRY_DELTA = False
    # Deprecated Z5 TP/SL toggles: force OFF so Codex/patches do not revive entry_z5 logic.
    EXIT_Z_TP_ENABLED = False
    EXIT_Z_SL_ENABLED = False
    USE_EXIT_Z_FIXED_OR_ENTRY_DELTA = False
    EXIT_Z_FIXED_LONG = 0.30   # legacy/unused in entry-anchor TP/SL mode
    EXIT_Z_FIXED_SHORT = -0.30 # legacy/unused in entry-anchor TP/SL mode
    EXIT_Z_ENTRY_DELTA = 0.50  # legacy/unused; do not use entry_z5
    # Deprecated legacy Z5 extrema window; ignored by price/retrace EXIT_Z.
    EXIT_Z_LOOKBACK_BARS = 5
    # EXIT_Z extrema expansion buffer:
    # LONG  stop threshold = prevN low  - EXIT_Z_EXTREMA_DIFF
    # SHORT stop threshold = prevN high + EXIT_Z_EXTREMA_DIFF
    EXIT_Z_EXTREMA_DIFF = 0.5
    EXIT_Z_TP_DELTA = 1.30  # legacy/unused; do not use entry_z5
    EXIT_Z_TP_MIN_PNL_PT = 0.0
    
    # [공통]
    SL = 3.5
    TS_START = 3.5
    TS_GAP = 3.5

    # ===== current strategy =====
    USE_ADX_FLOOR = False   # 거래횟수 유지 우선 모드
    ADX_FLOOR_LONG = 18.0
    ADX_FLOOR_SHORT = 12.0
    ADX_PERIOD = 14
    ADX_DELTA_BARS = 2
    ADX_DELTA_MIN = 0.0

    MACD_FAST = 12
    MACD_SLOW = 26
    MACD_SIGNAL = 9
    USE_MACD_OSCI_FILTER = False
    MACD_OSCI_LONG_MIN = 0.01
    MACD_OSCI_SHORT_MAX = -0.01

    USE_LATE_SESSION_TIGHTEN = True
    LATE_SESSION_START = "14:40"
    LATE_LONG_ENTRY_Z = -1.0
    LATE_SHORT_ENTRY_Z = 1.0
    LATE_ADX_DELTA_MIN = 0.2
    TRADE_START = "08:50"
    TRADE_END = "15:30"
    # Entry submit cooldown (duplicate-submit guard).
    ENTRY_SUBMIT_COOLDOWN_SEC = 3.0
    # ENTRY/EXIT 체결 직후 공통 액션 쿨다운(초): 자동 ENTRY/EXIT 재전송 모두 차단
    POST_FILL_ACTION_COOLDOWN_SEC = 3.0
    # MFE 컷과 반대 ENTRY FIRE가 같은 틱에 겹칠 때, EXIT_FILLED까지 반대 진입 스냅샷을 보존하는 시간(초)
    POST_EXIT_OVERLAP_ENTRY_WINDOW_SEC = 12.0
    # EXIT_FILLED 콜백/OrderCore pending 정리 후 반대 ENTRY를 넣기 전 안정화 지연(ms).
    POST_EXIT_OVERLAP_ENTRY_DELAY_MS = 350
    # 청산 직후 반대 ENTRY가 미확인 상태가 됐을 때 서버 무포지션/무미체결 확인 후 재시도까지 기다릴 최소 시간(초).
    POST_EXIT_ENTRY_UNKNOWN_CLEAR_SEC = 2.2
    # 위 서버 클리어 후 저장된 FIRE 스냅샷으로 재전송 가능한 최대 횟수.
    POST_EXIT_ENTRY_MAX_SEND_ATTEMPTS = 2
    # MFE_PROTECT는 손실완화 청산이므로 청산 직후 저장된 반대 ENTRY를 즉시 제출하지 않는다.
    # True로 바꾸면 기존 post-exit overlap lane에 태울 수 있지만 라이브 안정성 우선으로 기본 OFF.
    MFE_PROTECT_POST_EXIT_ENTRY_ENABLED = False
    # MFE_PROTECT 청산 후에는 잔존 ARM/FIRE 스냅샷을 지워 같은 조건 재진입 루프를 방지한다.
    MFE_PROTECT_RESET_ENTRY_ARMS = True
    # Exit 후 재진입 쿨다운 (즉시 반대진입 포함)
    REVERSE_ENTRY_COOLDOWN_SEC = 10.0
    # REVERSE_SIGNAL 청산 체결 확인 직후 반대 신규주문을 즉시 제출한다.
    REVERSE_SIGNAL_REENTRY_COOLDOWN_SEC = 0.0
    # FIXED_LOSS 청산 직후 전용 재진입 쿨다운(초)
    FIXED_LOSS_REENTRY_COOLDOWN_SEC = 5.0
    # MFE 컷(MFE_TRAIL / MFE_PROTECT) 청산 직후 전용 재진입 쿨다운(초)
    MFE_CUT_REENTRY_COOLDOWN_SEC = 10.0
    # 동일 바(확정봉) 내 리버스 진입 허용
    ALLOW_SAME_BAR_REVERSE = False
    # 반대 시그널 기반 청산(REVERSE_SIGNAL) 사용 여부 (live/backtest 공통).
    # REVERSE_SIGNAL is the highest-priority exit. After its fill, immediately
    # submit the saved opposite FIRE as a linked new entry.
    USE_REVERSE_SIGNAL_EXIT = False
    ALLOW_REVERSE_SIGNAL_REENTRY = False
    # REVERSE_SIGNAL 보강: 반대편 full entry-ready가 아니어도
    # LONG 보유 중 z5<=-REVERSE_Z5_ABS_LEVEL, SHORT 보유 중 z5>=+REVERSE_Z5_ABS_LEVEL이면 리버스 청산 허용.
    USE_REVERSE_Z5_ABS_FALLBACK = False
    REVERSE_Z5_ABS_LEVEL = 2.5
    EXIT_MIN_ABS_MOVE_PT = 0.0

    # End-of-day hard gate: block new entries (including reverse entries) after cutoff.
    EOD_BLOCK_NEW_ENTRY = True
    EOD_CUTOFF = "15:33"
    # End-of-day forced flat: if position exists at/after cutoff, submit exit automatically.
    EOD_FORCE_EXIT_POSITION = True
    SHORT_Z5_BLOCK_EXTREME = 2.0
    SHORT_Z5_BLOCK_BULL_FULL = 1.8
    SHORT_Z1_BLOCK_BULL_FULL = 1.2
    SHORT_Z5_BLOCK_BULL_FULL_SECONDARY = 0.8
    SHORT_Z1_BLOCK_MIXED = -2.5
    SHORT_Z5_BLOCK_MIXED = 0.4

    # ===== KIS live order/execution settings =====
    LIVE_SYMBOL_CODE = SYMBOL_CODE
    HIST_SYMBOL_CODE = SYMBOL_CODE
    LIVE_QTY = 1
    # Order execution mode: LIMIT_L1 / BEST_LIMIT. SMA10S_REVERSAL is an
    # intentional exception and submits a LIMIT at the cross-event current price.
    # LIMIT_L1 : buy=ASK1, sell=BID1, KIS order code 01
    # BEST_LIMIT: KIS best-limit, order code 04, price 0
    LIVE_ORDER_EXECUTION_MODE = "LIMIT_L1"
    # Historical tick replay mirrors the KisOrderCore entry unfilled lane.
    # KIS_TICK_DATA caches without L1 quote columns use the signal tick as the limit
    # price, then require a later saved tick to trade through that price.
    TICK_REPLAY_ENTRY_UNFILLED_CANCEL_SEC = 1.2
    TICK_REPLAY_POST_CANCEL_COOLDOWN_SEC = 2.0
    # Tick replay exits use market orders so a stop signal cannot remain
    # stranded as an unfilled limit order for the rest of the session.
    TICK_REPLAY_EXIT_ORDER_MODE = "MARKET"
    # Keep REVERSE_SIGNAL liquidation market-only in replay even if the
    # general replay EXIT mode is changed for another exit family later.
    TICK_REPLAY_REVERSE_EXIT_ORDER_MODE = "MARKET"
    # Official tick replay also uses the 10-second reversal SMA ENTRY path. Keep the
    # legacy source label for report compatibility, but disable Z1 execution.
    TICK_REPLAY_ENTRY_SIGNAL_SOURCE = "z1"
    TICK_REPLAY_USE_Z1_DELTA_REVERSAL_ENTRY = False
    TICK_REPLAY_MONITOR_Z1_DELTA_CONTINUATION = False
    TICK_REPLAY_USE_Z5_DELTA_REVERSAL_ENTRY = False
    TICK_REPLAY_ORDER_SYNC_PATCH_TAG = "PENDING_NEXT_TICK_L1_FALLBACK_20260728"

    # ===== takeover (position sync on startup) =====
    TAKEOVER_RQNAME = "REQ_TAKEOVER"
    UNFILLED_SYNC_RQNAME = "REQ_UNFILLED_SYNC"
    # KIS paper REST is rate-limited; startup sync requests are deliberately
    # paced and therefore need more headroom than the former desktop broker.
    ACCOUNT_SNAPSHOT_TIMEOUT_MS = 10000
    ORDERABLE_SNAPSHOT_TIMEOUT_MS = 10000
    UNFILLED_SYNC_TIMEOUT_MS = 10000
    TAKEOVER_TIMEOUT_MS = 10000
    UNFILLED_SYNC_MIN_INTERVAL_SEC = 1.5
    # Order notices are delivered by WebSocket. REST is only a safety
    # heartbeat while idle; running four account queries every five seconds
    # consumes the same APP-KEY budget needed by REST-polled paper quotes.
    UNFILLED_TIMER_IDLE_INTERVAL_SEC = 60.0
    TAKEOVER_HEARTBEAT_SEC = 0.0
    LOGIN_WAIT_WARN_SEC = 8.0
    LOGIN_WAIT_TIMEOUT_SEC = 25.0
    # Ignore transient balance-flat execution_notice right after entry fill/pending-entry to avoid false FLAT override.
    KIS_EXECUTION_BALANCE_FLAT_GUARD_SEC = 2.0
    UNFILLED_RECHECK_DELAY_MS = 350
    # EXIT unfilled watchdog: query after 1s, cancel at 3s, then re-submit best-limit after cancel confirmation.
    EXIT_UNFILLED_QUERY_DELAY_MS = 1000
    EXIT_UNFILLED_CANCEL_DELAY_MS = 3000
    EXIT_UNFILLED_RETRY_DELAY_MS = 150
    EXIT_UNFILLED_LOOP_ENABLED = True

    # EXIT exclusive safety lane:
    # From the first EXIT submit until broker position=0 and EXIT unfilled=0 are
    # both confirmed, block every ENTRY path and pause non-EXIT server queries.
    EXIT_EXCLUSIVE_MODE_ENABLED = True
    EXIT_EXCLUSIVE_BLOCK_ENTRY_WATCH = True
    EXIT_EXCLUSIVE_PAUSE_NON_EXIT_SERVER_SYNC = True
    WARMUP_USE_TR = True
    WARMUP_INTERVAL_MIN = 1
    WARMUP_MAX_PAGES = 5
    WARMUP_REQNAME = "REQ_WARMUP_1M"
    # Dynamic first-boot seed: previous-session 95 rows + today 08:30~now + 25 safety rows.
    # The request is clamped to 140~600 rows; 2,000/2,500-row OHLC-backtest seeding is retired.
    # Live and official tick replay both seed the latest 600 raw 1m rows.
    WARMUP_TARGET_ROWS = 600
    # Legacy sizing fields retained for compatibility; the current live controller
    # and official tick replay use WARMUP_TARGET_ROWS directly.
    WARMUP_DYNAMIC_TARGET = True
    WARMUP_MIN_TARGET_ROWS = 140
    WARMUP_MAX_TARGET_ROWS = 600
    WARMUP_PREV_SESSION_1M_ROWS = 95
    WARMUP_SAFETY_ROWS = 25
    WARMUP_DAY_START_TIME = "08:30"
    WARMUP_DAY_END_TIME = "15:45"
    WARMUP_TIMEOUT_SEC = 10.0
    # Preserve successful KIS history pages across transient failures. Retries
    # use exponential backoff and stop after a bounded number of attempts so a
    # prolonged paper-server outage cannot create an endless request loop.
    WARMUP_RETRY_MAX_ATTEMPTS = 8
    WARMUP_RETRY_BASE_DELAY_SEC = 5.0
    WARMUP_RETRY_MAX_DELAY_SEC = 60.0

    # Previous-session additive gap adjustment for Z5/regime only.
    # Previous-session OHLC is shifted by (today 08:45 open - previous close);
    # current-day/live/order/PnL prices remain raw.
    WARMUP_GAP_ADJUST_ENABLED = True
    WARMUP_GAP_ANCHOR_TIME = "08:45"

    # Same-process reconnect: replay boot server snapshots, retain Strategy/position state,
    # and append only missing confirmed 1m candles. Never reset the post-warmup bar gate.
    RECONNECT_GAP_FILL_ONLY = True
    RECONNECT_GAP_FILL_SAFETY_ROWS = 12
    RECONNECT_GAP_FILL_MAX_ROWS = 600

    # warmup gap fill (빈 1분을 직전 종가로 채워 5분/30분 Z 왜곡 완화)
    WARMUP_FILL_MISSING_1M = False
    WARMUP_FILL_MAX_GAP_MIN = 30
    # Backtest evaluation mode:
    # Anchor is still the confirmed 1m close high/low, same as live.
    # ENTRY can optionally approximate live current-price ARM/FIRE with an OHLC path
    # and fill at the configured FIRE threshold instead of the bar close.
    BT_ENTRY_ON_CLOSE_ONLY = True
    BT_ENTRY_INTRABAR_ARM_FIRE = True
    BT_ENTRY_INTRABAR_BLOCK_BOTH_FIRE = True
    # False = live-like: an ARM created by a prior pseudo tick/bar can fire later
    # until expiry/regime switch/opposite ARM clears it.
    BT_ENTRY_INTRABAR_REQUIRE_CURRENT_BAR_ARM = False
    BT_ENTRY_INTRABAR_ARM_MAX_BARS = 5
    BT_EXIT_INTRABAR_OHLC4 = True
    # Make backtest MFE peak/trough update like live ticks: OHLC replay points
    # are treated as sequential current-price ticks, not full-bar high/low lookahead.
    BT_MFE_TICKLIKE_EXTREMA = True
    # Legacy all-signal OHLC4 replay. Keep False; the new model only replays
    # price thresholds for ARM/FIRE entry, not the whole Strategy.signal path.
    BT_TICK_REPLAY_OHLC4 = False
    # Legacy backtest alias kept in sync with the live entry mode so reports and
    # optional parity checks do not imply a divergent entry engine by default.
    BT_USE_PRICE_EXTREMA_ENTRY = USE_PRICE_EXTREMA_ENTRY
    BT_PRICE_ENTRY_LOOKBACK_BARS = 5
    BT_PRICE_ENTRY_OFFSET_PT = 0.0
    # Backtest scenario selection:
    # default runs only BASE for speed. Use comma-separated names to compare:
    # "BASE,REGIME_LOG_ONLY,SOFT_REGIME_FIRE,STRICT_REGIME_BLOCK"
    BT_SCENARIOS = "BASE"

    # session-open z stabilization (장초반 5분/30분 Z 과대폭 완화)
    Z_OPEN_STABILIZE = False
    Z_OPEN_STABILIZE_START = "08:45"
    Z_OPEN_STABILIZE_END = "09:00"
    Z_OPEN_STABILIZE_MIN_SCALE = 0.25
    Z_OPEN_STABILIZE_ABS_CAP = 3.0

    # ===== backtest v6 (high-frequency relaxed EMA20 price-cross + z1/z5) =====
    BT_USE_V6_EMA_PRICE = False

    # LONG continuation / recovery (relaxed to increase trade count)
    BT_V6_LONG_Z1_PREV_MAX = 0.5
    BT_V6_LONG_Z1_CUR_MIN = 0.0
    BT_V6_LONG_Z5_MIN = -1.2
    BT_V6_LONG_Z5_MAX = 1.5

    # SHORT continuation / breakdown (relaxed to increase trade count)
    BT_V6_SHORT_Z1_PREV_MIN = -0.5
    BT_V6_SHORT_Z1_CUR_MAX = 0.0
    BT_V6_SHORT_Z5_MIN = -1.5
    BT_V6_SHORT_Z5_MAX = 0.8

    # fast exit by EMA20 re-cross + z1 turn (v7 relaxed)
    BT_V6_USE_FAST_EXIT = False
    BT_V7_FAST_EXIT_CONFIRM_BARS = 2
    BT_V7_FAST_EXIT_LONG_Z1_MAX = -0.5
    BT_V7_FAST_EXIT_SHORT_Z1_MIN = 0.5
    BT_V7_FAST_EXIT_REQUIRE_BARS_HELD = 2

    # Exit standard
    BT_USE_TRAIL_EXIT = True
    BT_TRAIL_ARM_PT = 2.0
    BT_TRAIL_GAP_PT = 2.0
    USE_STOP_LOSS_EXIT = False
    BT_STOP_LOSS_PT = 3.5
    USE_FIXED_LOSS_EXIT = True
    FIXED_LOSS_PT = 1.2
    FIXED_LOSS_BAND_LOW_PT = 1.2
    FIXED_LOSS_BAND_HIGH_PT = 1.6
    FIXED_LOSS_DISPLAY_AS_BAND = True
    # Exit immediately when the current PnL enters the inclusive
    # -1.2~-1.6pt band. No single/random threshold is selected in the band.
    FIXED_LOSS_BAND_MODE = "BAND_ENTRY"
    FIXED_LOSS_BAND_POSITION = 0.0
    FIXED_LOSS_BAND_SEED = 20260712
    # Hard-stop liquidation uses a market order for immediate execution.
    FIXED_LOSS_EXIT_ORDER_MODE = "MARKET"
    USE_MFE_RETRACE_EXIT = True
    # MFE_PROTECT is enabled for both entry regimes.
    # Each preset rule is:
    #   (MFE threshold, exit PnL band low, exit PnL band high)
    # It arms at MFE>=1.6 and exits immediately when PnL enters
    # the inclusive +0.6~+1.2pt protection band.
    USE_MFE_PROTECT_EXIT = True
    DISABLE_MFE_PROTECT_FOR_REVERSAL = False
    MFE_PROTECT_RULE_BASIS = "MFE"
    MFE_PROTECT_RULES = (
        (1.6, 0.6, 1.2),
    )
    # Protection-band liquidation uses KIS market order code 02 at price 0.
    MFE_PROTECT_EXIT_ORDER_MODE = "MARKET"
    MFE_PROTECT_BAND_MODE = "BAND_ENTRY"
    MFE_PROTECT_BAND_POSITION = 0.0
    MFE_PROTECT_BAND_SEED = 20260712
    MFE_PROTECT_SCOPE_PATCH_TAG = "ALL_REGIMES_MFE16_PNL_POS06_POS12_HANDOFF_MFE_CUT_20260728"
    EXIT_MFE_MIN_PT = 2.2
    # No fixed PROTECT upper bound. PROTECT hands off only when MFE CUT is armed.
    # Zero disables the legacy numeric MFE ceiling.
    MFE_PROTECT_MAX_MFE_PT = 0.0
    EXIT_BAND_SYNC_PATCH_TAG = "FIXED_LOSS_1P2_TO_1P6_20260916"
    EXIT_MFE_RETRACE_PCT = 0.30
    # S1 MFE CUT is active from MFE >= 2.2pt and takes ownership from PROTECT.
    EXIT_MFE_STAGE1_ENABLED = True
    EXIT_MFE_STAGE1_PT = 2.2
    EXIT_MFE_STAGE2_PT = 6.0
    EXIT_MFE_STAGE3_PT = 9.0
    EXIT_MFE_STAGE4_PT = 12.0  # MFE stages: 2.2 / 6.0 / 9.0 / 12.0pt
    EXIT_MFE_STAGE_MODE = "UPPER_BOUND"
    # Backward-compatible aliases; live strategy now uses EXIT_MFE_STAGE*_PT.
    EXIT_MFE_STAGE2_AT_PT = EXIT_MFE_STAGE2_PT
    EXIT_MFE_STAGE3_AT_PT = EXIT_MFE_STAGE3_PT
    EXIT_MFE_STAGE4_AT_PT = EXIT_MFE_STAGE4_PT
    EXIT_MFE_RETRACE_PCT_STAGE1_MIN = 0.20
    EXIT_MFE_RETRACE_PCT_STAGE1_MAX = 0.25
    EXIT_MFE_RETRACE_PCT_STAGE2_MIN = 0.20
    EXIT_MFE_RETRACE_PCT_STAGE2_MAX = 0.25
    EXIT_MFE_RETRACE_PCT_STAGE3_MIN = 0.15
    EXIT_MFE_RETRACE_PCT_STAGE3_MAX = 0.20
    EXIT_MFE_RETRACE_PCT_STAGE4_MIN = 0.10
    EXIT_MFE_RETRACE_PCT_STAGE4_MAX = 0.15
    # Trigger immediately on the first (shallow) boundary of each MFE CUT band.
    # A tick that crosses the whole band remains a valid exit signal.
    # Modes: BAND_ENTRY / DETERMINISTIC_RANDOM / FIXED_RATIO / LOW / MID / HIGH
    EXIT_MFE_CUT_BAND_MODE = "BAND_ENTRY"
    EXIT_MFE_CUT_BAND_POSITION = 0.50  # FIXED_RATIO only: 0=LOW, 1=HIGH
    EXIT_MFE_CUT_BAND_SEED = 20260712
    MFE_CUT_BAND_PATCH_TAG = "MFE_ALL_STAGE_BAND_ENTRY_PERSISTENT_20260807"
    # Backward-compatible single-value percentages remain aliases only.
    EXIT_MFE_RETRACE_PCT_STAGE1 = EXIT_MFE_RETRACE_PCT_STAGE1_MAX
    EXIT_MFE_RETRACE_PCT_STAGE2 = EXIT_MFE_RETRACE_PCT_STAGE2_MAX
    EXIT_MFE_RETRACE_PCT_STAGE3 = EXIT_MFE_RETRACE_PCT_STAGE3_MAX
    EXIT_MFE_RETRACE_PCT_STAGE4 = EXIT_MFE_RETRACE_PCT_STAGE4_MAX
    EXIT_MFE_MIN_RETRACE_PT = 0.80
    # S1 exits when the live retracement is inside the 20%~25% band.
    EXIT_MFE_MIN_RETRACE_PT_STAGE1 = 0.0
    EXIT_MFE_MIN_RETRACE_PT_STAGE2 = 0.80
    EXIT_MFE_MIN_RETRACE_PT_STAGE3 = 1.00
    EXIT_MFE_MIN_RETRACE_PT_STAGE4 = 1.20
    EXIT_MFE_MIN_HOLD_SEC = 0.0
    # S1 MFE_TRAIL has no activation delay; band entry can exit immediately.
    EXIT_MFE_S1_DELAY_SEC = 0.0

    # 2번 파일 EXIT 조건 이식: entry-regime split MFE profile is disabled.
    USE_ENTRY_REGIME_MFE_PROFILE = False

    # Legacy profile attributes kept as aliases so older modules/imports do not break.
    REVERSAL_EXIT_MFE_MIN_PT = EXIT_MFE_MIN_PT
    REVERSAL_EXIT_MFE_RETRACE_PCT = EXIT_MFE_RETRACE_PCT
    REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE1_MIN = EXIT_MFE_RETRACE_PCT_STAGE1_MIN
    REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE1_MAX = EXIT_MFE_RETRACE_PCT_STAGE1_MAX
    REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE2_MIN = EXIT_MFE_RETRACE_PCT_STAGE2_MIN
    REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE2_MAX = EXIT_MFE_RETRACE_PCT_STAGE2_MAX
    REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE3_MIN = EXIT_MFE_RETRACE_PCT_STAGE3_MIN
    REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE3_MAX = EXIT_MFE_RETRACE_PCT_STAGE3_MAX
    REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE4_MIN = EXIT_MFE_RETRACE_PCT_STAGE4_MIN
    REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE4_MAX = EXIT_MFE_RETRACE_PCT_STAGE4_MAX
    REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE1 = EXIT_MFE_RETRACE_PCT_STAGE1
    REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE2 = EXIT_MFE_RETRACE_PCT_STAGE2
    REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE3 = EXIT_MFE_RETRACE_PCT_STAGE3
    REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE4 = EXIT_MFE_RETRACE_PCT_STAGE4
    REVERSAL_EXIT_MFE_STAGE2_AT_PT = EXIT_MFE_STAGE2_PT
    REVERSAL_EXIT_MFE_STAGE3_AT_PT = EXIT_MFE_STAGE3_PT
    REVERSAL_EXIT_MFE_STAGE4_AT_PT = EXIT_MFE_STAGE4_PT
    REVERSAL_EXIT_MFE_MIN_RETRACE_PT = EXIT_MFE_MIN_RETRACE_PT
    REVERSAL_EXIT_MFE_MIN_RETRACE_PT_STAGE1 = EXIT_MFE_MIN_RETRACE_PT_STAGE1
    REVERSAL_EXIT_MFE_MIN_RETRACE_PT_STAGE2 = EXIT_MFE_MIN_RETRACE_PT_STAGE2
    REVERSAL_EXIT_MFE_MIN_RETRACE_PT_STAGE3 = EXIT_MFE_MIN_RETRACE_PT_STAGE3
    REVERSAL_EXIT_MFE_MIN_RETRACE_PT_STAGE4 = EXIT_MFE_MIN_RETRACE_PT_STAGE4
    REVERSAL_EXIT_MFE_CUT_BAND_MODE = EXIT_MFE_CUT_BAND_MODE
    REVERSAL_EXIT_MFE_CUT_BAND_POSITION = EXIT_MFE_CUT_BAND_POSITION
    REVERSAL_EXIT_MFE_CUT_BAND_SEED = EXIT_MFE_CUT_BAND_SEED
    TREND_EXIT_MFE_MIN_PT = EXIT_MFE_MIN_PT
    TREND_EXIT_MFE_RETRACE_PCT = EXIT_MFE_RETRACE_PCT
    TREND_EXIT_MFE_RETRACE_PCT_STAGE1_MIN = EXIT_MFE_RETRACE_PCT_STAGE1_MIN
    TREND_EXIT_MFE_RETRACE_PCT_STAGE1_MAX = EXIT_MFE_RETRACE_PCT_STAGE1_MAX
    TREND_EXIT_MFE_RETRACE_PCT_STAGE2_MIN = EXIT_MFE_RETRACE_PCT_STAGE2_MIN
    TREND_EXIT_MFE_RETRACE_PCT_STAGE2_MAX = EXIT_MFE_RETRACE_PCT_STAGE2_MAX
    TREND_EXIT_MFE_RETRACE_PCT_STAGE3_MIN = EXIT_MFE_RETRACE_PCT_STAGE3_MIN
    TREND_EXIT_MFE_RETRACE_PCT_STAGE3_MAX = EXIT_MFE_RETRACE_PCT_STAGE3_MAX
    TREND_EXIT_MFE_RETRACE_PCT_STAGE4_MIN = EXIT_MFE_RETRACE_PCT_STAGE4_MIN
    TREND_EXIT_MFE_RETRACE_PCT_STAGE4_MAX = EXIT_MFE_RETRACE_PCT_STAGE4_MAX
    TREND_EXIT_MFE_RETRACE_PCT_STAGE1 = EXIT_MFE_RETRACE_PCT_STAGE1
    TREND_EXIT_MFE_RETRACE_PCT_STAGE2 = EXIT_MFE_RETRACE_PCT_STAGE2
    TREND_EXIT_MFE_RETRACE_PCT_STAGE3 = EXIT_MFE_RETRACE_PCT_STAGE3
    TREND_EXIT_MFE_RETRACE_PCT_STAGE4 = EXIT_MFE_RETRACE_PCT_STAGE4
    TREND_EXIT_MFE_STAGE2_AT_PT = EXIT_MFE_STAGE2_PT
    TREND_EXIT_MFE_STAGE3_AT_PT = EXIT_MFE_STAGE3_PT
    TREND_EXIT_MFE_STAGE4_AT_PT = EXIT_MFE_STAGE4_PT
    TREND_EXIT_MFE_MIN_RETRACE_PT = EXIT_MFE_MIN_RETRACE_PT
    TREND_EXIT_MFE_MIN_RETRACE_PT_STAGE1 = EXIT_MFE_MIN_RETRACE_PT_STAGE1
    TREND_EXIT_MFE_MIN_RETRACE_PT_STAGE2 = EXIT_MFE_MIN_RETRACE_PT_STAGE2
    TREND_EXIT_MFE_MIN_RETRACE_PT_STAGE3 = EXIT_MFE_MIN_RETRACE_PT_STAGE3
    TREND_EXIT_MFE_MIN_RETRACE_PT_STAGE4 = EXIT_MFE_MIN_RETRACE_PT_STAGE4
    TREND_EXIT_MFE_CUT_BAND_MODE = EXIT_MFE_CUT_BAND_MODE
    TREND_EXIT_MFE_CUT_BAND_POSITION = EXIT_MFE_CUT_BAND_POSITION
    TREND_EXIT_MFE_CUT_BAND_SEED = EXIT_MFE_CUT_BAND_SEED

    # MFE retrace trigger mode:
    # - "TOUCH": trigger on live/current price touch of the cut line.
    # - EXIT_MFE_TOUCH_USE_BAR_EXTREMA=False prevents same-bar high/low look-ahead.
    EXIT_MFE_TRIGGER_MODE = "TOUCH"
    EXIT_MFE_TOUCH_USE_BAR_EXTREMA = False
    USE_BAR3_CUT_EXIT = True
    EXIT_BAR_LOOKBACK = 3
    EXIT_BAR_RETRACE_PCT = 0.20
    USE_MAX_HOLD_EXIT = True
    MAX_HOLD_MIN = 60
    # 백테스트에서 청산 직후 동일 바 리버스 진입 허용
    BT_ALLOW_SAME_BAR_REVERSE_ENTRY = False
    # 오버나잇 금지: 날짜 경계에서 강제 청산
    BT_CLOSE_ON_SESSION_BOUNDARY = True
    # Live parity default: do not run any backtester-local EXIT_Z override.
    # EXIT_Z must come from Strategy.signal(), same as live.
    BT_FORCE_EXIT_Z_OVERRIDE = False



# module-level aliases for backward compatibility
SYMBOL_CODE = Config.SYMBOL_CODE
TICK_REPLAY_ENGINE = Config.TICK_REPLAY_ENGINE
TICK_REPLAY_TRADING_DAYS = Config.TICK_REPLAY_TRADING_DAYS
TICK_REPLAY_CACHE_MODE = Config.TICK_REPLAY_CACHE_MODE
TICK_REPLAY_CACHE_DIR = Config.TICK_REPLAY_CACHE_DIR
TICK_REPLAY_EVENT_LOG_LEVEL = Config.TICK_REPLAY_EVENT_LOG_LEVEL
USE_INCREMENTAL_LATEST_MINUTE_UPDATE = Config.USE_INCREMENTAL_LATEST_MINUTE_UPDATE
TICK_REPLAY_BULK_WARMUP = Config.TICK_REPLAY_BULK_WARMUP
MFE_USE_POSITION_META_EXTREMA = Config.MFE_USE_POSITION_META_EXTREMA
TICK_REPLAY_PERFORMANCE_PATCH_TAG = Config.TICK_REPLAY_PERFORMANCE_PATCH_TAG
TICK_REPLAY_REQUIRE_CONFIG_SYMBOL_MATCH = Config.TICK_REPLAY_REQUIRE_CONFIG_SYMBOL_MATCH
TICK_REPLAY_CONFIG_SYNC_PATCH_TAG = Config.TICK_REPLAY_CONFIG_SYNC_PATCH_TAG
LIVE_SYMBOL_CODE = Config.LIVE_SYMBOL_CODE
HIST_SYMBOL_CODE = Config.HIST_SYMBOL_CODE
KRX_HOLIDAYS_2026 = Config.KRX_HOLIDAYS_2026
LIVE_QTY = Config.LIVE_QTY
LIVE_ORDER_EXECUTION_MODE = Config.LIVE_ORDER_EXECUTION_MODE

WARMUP_USE_TR = Config.WARMUP_USE_TR
WARMUP_INTERVAL_MIN = Config.WARMUP_INTERVAL_MIN
WARMUP_MAX_PAGES = Config.WARMUP_MAX_PAGES
WARMUP_REQNAME = Config.WARMUP_REQNAME

WARMUP_TARGET_ROWS = Config.WARMUP_TARGET_ROWS
WARMUP_DYNAMIC_TARGET = Config.WARMUP_DYNAMIC_TARGET
WARMUP_MIN_TARGET_ROWS = Config.WARMUP_MIN_TARGET_ROWS
WARMUP_MAX_TARGET_ROWS = Config.WARMUP_MAX_TARGET_ROWS
WARMUP_PREV_SESSION_1M_ROWS = Config.WARMUP_PREV_SESSION_1M_ROWS
WARMUP_SAFETY_ROWS = Config.WARMUP_SAFETY_ROWS
WARMUP_DAY_START_TIME = Config.WARMUP_DAY_START_TIME
WARMUP_DAY_END_TIME = Config.WARMUP_DAY_END_TIME
WARMUP_GAP_ADJUST_ENABLED = Config.WARMUP_GAP_ADJUST_ENABLED
WARMUP_GAP_ANCHOR_TIME = Config.WARMUP_GAP_ANCHOR_TIME
RECONNECT_GAP_FILL_ONLY = Config.RECONNECT_GAP_FILL_ONLY
RECONNECT_GAP_FILL_SAFETY_ROWS = Config.RECONNECT_GAP_FILL_SAFETY_ROWS
RECONNECT_GAP_FILL_MAX_ROWS = Config.RECONNECT_GAP_FILL_MAX_ROWS
WARMUP_TIMEOUT_SEC = Config.WARMUP_TIMEOUT_SEC
WARMUP_RETRY_MAX_ATTEMPTS = Config.WARMUP_RETRY_MAX_ATTEMPTS
WARMUP_RETRY_BASE_DELAY_SEC = Config.WARMUP_RETRY_BASE_DELAY_SEC
WARMUP_RETRY_MAX_DELAY_SEC = Config.WARMUP_RETRY_MAX_DELAY_SEC

TAKEOVER_RQNAME  = Config.TAKEOVER_RQNAME
UNFILLED_SYNC_RQNAME = Config.UNFILLED_SYNC_RQNAME
UNFILLED_SYNC_TIMEOUT_MS = Config.UNFILLED_SYNC_TIMEOUT_MS
UNFILLED_SYNC_MIN_INTERVAL_SEC = Config.UNFILLED_SYNC_MIN_INTERVAL_SEC
UNFILLED_TIMER_IDLE_INTERVAL_SEC = Config.UNFILLED_TIMER_IDLE_INTERVAL_SEC
TAKEOVER_HEARTBEAT_SEC = Config.TAKEOVER_HEARTBEAT_SEC
LOGIN_WAIT_WARN_SEC = Config.LOGIN_WAIT_WARN_SEC
LOGIN_WAIT_TIMEOUT_SEC = Config.LOGIN_WAIT_TIMEOUT_SEC
KIS_EXECUTION_BALANCE_FLAT_GUARD_SEC = Config.KIS_EXECUTION_BALANCE_FLAT_GUARD_SEC
UNFILLED_RECHECK_DELAY_MS = Config.UNFILLED_RECHECK_DELAY_MS

WARMUP_FILL_MISSING_1M = Config.WARMUP_FILL_MISSING_1M
WARMUP_FILL_MAX_GAP_MIN = Config.WARMUP_FILL_MAX_GAP_MIN


Z_OPEN_STABILIZE = Config.Z_OPEN_STABILIZE
Z_OPEN_STABILIZE_START = Config.Z_OPEN_STABILIZE_START
Z_OPEN_STABILIZE_END = Config.Z_OPEN_STABILIZE_END
Z_OPEN_STABILIZE_MIN_SCALE = Config.Z_OPEN_STABILIZE_MIN_SCALE
Z_OPEN_STABILIZE_ABS_CAP = Config.Z_OPEN_STABILIZE_ABS_CAP


BT_USE_V6_EMA_PRICE = Config.BT_USE_V6_EMA_PRICE
BT_V6_LONG_Z1_PREV_MAX = Config.BT_V6_LONG_Z1_PREV_MAX
BT_V6_LONG_Z1_CUR_MIN = Config.BT_V6_LONG_Z1_CUR_MIN
BT_V6_LONG_Z5_MIN = Config.BT_V6_LONG_Z5_MIN
BT_V6_LONG_Z5_MAX = Config.BT_V6_LONG_Z5_MAX
BT_V6_SHORT_Z1_PREV_MIN = Config.BT_V6_SHORT_Z1_PREV_MIN
BT_V6_SHORT_Z1_CUR_MAX = Config.BT_V6_SHORT_Z1_CUR_MAX
BT_V6_SHORT_Z5_MIN = Config.BT_V6_SHORT_Z5_MIN
BT_V6_SHORT_Z5_MAX = Config.BT_V6_SHORT_Z5_MAX
BT_V6_USE_FAST_EXIT = Config.BT_V6_USE_FAST_EXIT
BT_V7_FAST_EXIT_CONFIRM_BARS = Config.BT_V7_FAST_EXIT_CONFIRM_BARS
BT_V7_FAST_EXIT_LONG_Z1_MAX = Config.BT_V7_FAST_EXIT_LONG_Z1_MAX
BT_V7_FAST_EXIT_SHORT_Z1_MIN = Config.BT_V7_FAST_EXIT_SHORT_Z1_MIN
BT_V7_FAST_EXIT_REQUIRE_BARS_HELD = Config.BT_V7_FAST_EXIT_REQUIRE_BARS_HELD
BT_USE_TRAIL_EXIT = Config.BT_USE_TRAIL_EXIT
BT_TRAIL_ARM_PT = Config.BT_TRAIL_ARM_PT
BT_TRAIL_GAP_PT = Config.BT_TRAIL_GAP_PT
BT_CLOSE_ON_SESSION_BOUNDARY = Config.BT_CLOSE_ON_SESSION_BOUNDARY
BT_FORCE_EXIT_Z_OVERRIDE = Config.BT_FORCE_EXIT_Z_OVERRIDE
USE_STOP_LOSS_EXIT = Config.USE_STOP_LOSS_EXIT
USE_FIXED_LOSS_EXIT = Config.USE_FIXED_LOSS_EXIT
FIXED_LOSS_PT = Config.FIXED_LOSS_PT
FIXED_LOSS_BAND_LOW_PT = Config.FIXED_LOSS_BAND_LOW_PT
FIXED_LOSS_BAND_HIGH_PT = Config.FIXED_LOSS_BAND_HIGH_PT
FIXED_LOSS_DISPLAY_AS_BAND = Config.FIXED_LOSS_DISPLAY_AS_BAND
FIXED_LOSS_BAND_MODE = Config.FIXED_LOSS_BAND_MODE
FIXED_LOSS_BAND_POSITION = Config.FIXED_LOSS_BAND_POSITION
FIXED_LOSS_BAND_SEED = Config.FIXED_LOSS_BAND_SEED
FIXED_LOSS_EXIT_ORDER_MODE = Config.FIXED_LOSS_EXIT_ORDER_MODE
TICK_REPLAY_EXIT_ORDER_MODE = Config.TICK_REPLAY_EXIT_ORDER_MODE
TICK_REPLAY_REVERSE_EXIT_ORDER_MODE = Config.TICK_REPLAY_REVERSE_EXIT_ORDER_MODE
TICK_REPLAY_ENTRY_SIGNAL_SOURCE = Config.TICK_REPLAY_ENTRY_SIGNAL_SOURCE
TICK_REPLAY_USE_Z1_DELTA_REVERSAL_ENTRY = Config.TICK_REPLAY_USE_Z1_DELTA_REVERSAL_ENTRY
TICK_REPLAY_MONITOR_Z1_DELTA_CONTINUATION = Config.TICK_REPLAY_MONITOR_Z1_DELTA_CONTINUATION
TICK_REPLAY_USE_Z5_DELTA_REVERSAL_ENTRY = Config.TICK_REPLAY_USE_Z5_DELTA_REVERSAL_ENTRY
USE_MFE_RETRACE_EXIT = Config.USE_MFE_RETRACE_EXIT
USE_MFE_PROTECT_EXIT = Config.USE_MFE_PROTECT_EXIT
DISABLE_MFE_PROTECT_FOR_REVERSAL = Config.DISABLE_MFE_PROTECT_FOR_REVERSAL
MFE_PROTECT_SCOPE_PATCH_TAG = Config.MFE_PROTECT_SCOPE_PATCH_TAG
MFE_PROTECT_MAX_MFE_PT = Config.MFE_PROTECT_MAX_MFE_PT
MFE_PROTECT_RULE_BASIS = Config.MFE_PROTECT_RULE_BASIS
MFE_PROTECT_RULES = Config.MFE_PROTECT_RULES
MFE_PROTECT_EXIT_ORDER_MODE = Config.MFE_PROTECT_EXIT_ORDER_MODE
MFE_PROTECT_BAND_MODE = Config.MFE_PROTECT_BAND_MODE
MFE_PROTECT_BAND_POSITION = Config.MFE_PROTECT_BAND_POSITION
MFE_PROTECT_BAND_SEED = Config.MFE_PROTECT_BAND_SEED
EXIT_BAND_SYNC_PATCH_TAG = Config.EXIT_BAND_SYNC_PATCH_TAG
MFE_PROTECT_POST_EXIT_ENTRY_ENABLED = Config.MFE_PROTECT_POST_EXIT_ENTRY_ENABLED
MFE_PROTECT_RESET_ENTRY_ARMS = Config.MFE_PROTECT_RESET_ENTRY_ARMS
EXIT_MFE_MIN_PT = Config.EXIT_MFE_MIN_PT
EXIT_MFE_RETRACE_PCT = Config.EXIT_MFE_RETRACE_PCT
EXIT_MFE_STAGE1_ENABLED = Config.EXIT_MFE_STAGE1_ENABLED
EXIT_MFE_STAGE1_PT = Config.EXIT_MFE_STAGE1_PT
EXIT_MFE_STAGE2_PT = Config.EXIT_MFE_STAGE2_PT
EXIT_MFE_STAGE3_PT = Config.EXIT_MFE_STAGE3_PT
EXIT_MFE_STAGE4_PT = Config.EXIT_MFE_STAGE4_PT
EXIT_MFE_STAGE_MODE = Config.EXIT_MFE_STAGE_MODE
EXIT_MFE_RETRACE_PCT_STAGE1_MIN = Config.EXIT_MFE_RETRACE_PCT_STAGE1_MIN
EXIT_MFE_RETRACE_PCT_STAGE1_MAX = Config.EXIT_MFE_RETRACE_PCT_STAGE1_MAX
EXIT_MFE_RETRACE_PCT_STAGE2_MIN = Config.EXIT_MFE_RETRACE_PCT_STAGE2_MIN
EXIT_MFE_RETRACE_PCT_STAGE2_MAX = Config.EXIT_MFE_RETRACE_PCT_STAGE2_MAX
EXIT_MFE_RETRACE_PCT_STAGE3_MIN = Config.EXIT_MFE_RETRACE_PCT_STAGE3_MIN
EXIT_MFE_RETRACE_PCT_STAGE3_MAX = Config.EXIT_MFE_RETRACE_PCT_STAGE3_MAX
EXIT_MFE_RETRACE_PCT_STAGE4_MIN = Config.EXIT_MFE_RETRACE_PCT_STAGE4_MIN
EXIT_MFE_RETRACE_PCT_STAGE4_MAX = Config.EXIT_MFE_RETRACE_PCT_STAGE4_MAX
EXIT_MFE_CUT_BAND_MODE = Config.EXIT_MFE_CUT_BAND_MODE
EXIT_MFE_CUT_BAND_POSITION = Config.EXIT_MFE_CUT_BAND_POSITION
EXIT_MFE_CUT_BAND_SEED = Config.EXIT_MFE_CUT_BAND_SEED
MFE_CUT_BAND_PATCH_TAG = Config.MFE_CUT_BAND_PATCH_TAG
EXIT_MFE_RETRACE_PCT_STAGE1 = Config.EXIT_MFE_RETRACE_PCT_STAGE1
EXIT_MFE_RETRACE_PCT_STAGE2 = Config.EXIT_MFE_RETRACE_PCT_STAGE2
EXIT_MFE_RETRACE_PCT_STAGE3 = Config.EXIT_MFE_RETRACE_PCT_STAGE3
EXIT_MFE_RETRACE_PCT_STAGE4 = Config.EXIT_MFE_RETRACE_PCT_STAGE4
EXIT_MFE_STAGE2_AT_PT = Config.EXIT_MFE_STAGE2_AT_PT
EXIT_MFE_STAGE3_AT_PT = Config.EXIT_MFE_STAGE3_AT_PT
EXIT_MFE_STAGE4_AT_PT = Config.EXIT_MFE_STAGE4_AT_PT
EXIT_MFE_MIN_RETRACE_PT = Config.EXIT_MFE_MIN_RETRACE_PT
EXIT_MFE_MIN_RETRACE_PT_STAGE1 = Config.EXIT_MFE_MIN_RETRACE_PT_STAGE1
EXIT_MFE_MIN_RETRACE_PT_STAGE2 = Config.EXIT_MFE_MIN_RETRACE_PT_STAGE2
EXIT_MFE_MIN_RETRACE_PT_STAGE3 = Config.EXIT_MFE_MIN_RETRACE_PT_STAGE3
EXIT_MFE_MIN_RETRACE_PT_STAGE4 = Config.EXIT_MFE_MIN_RETRACE_PT_STAGE4
EXIT_MFE_MIN_HOLD_SEC = Config.EXIT_MFE_MIN_HOLD_SEC
EXIT_MFE_S1_DELAY_SEC = Config.EXIT_MFE_S1_DELAY_SEC
USE_ENTRY_REGIME_MFE_PROFILE = Config.USE_ENTRY_REGIME_MFE_PROFILE
REVERSAL_EXIT_MFE_MIN_PT = Config.REVERSAL_EXIT_MFE_MIN_PT
REVERSAL_EXIT_MFE_RETRACE_PCT = Config.REVERSAL_EXIT_MFE_RETRACE_PCT
REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE1_MIN = Config.REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE1_MIN
REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE1_MAX = Config.REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE1_MAX
REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE2_MIN = Config.REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE2_MIN
REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE2_MAX = Config.REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE2_MAX
REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE3_MIN = Config.REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE3_MIN
REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE3_MAX = Config.REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE3_MAX
REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE4_MIN = Config.REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE4_MIN
REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE4_MAX = Config.REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE4_MAX
REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE1 = Config.REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE1
REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE2 = Config.REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE2
REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE3 = Config.REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE3
REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE4 = Config.REVERSAL_EXIT_MFE_RETRACE_PCT_STAGE4
REVERSAL_EXIT_MFE_STAGE2_AT_PT = Config.REVERSAL_EXIT_MFE_STAGE2_AT_PT
REVERSAL_EXIT_MFE_STAGE3_AT_PT = Config.REVERSAL_EXIT_MFE_STAGE3_AT_PT
REVERSAL_EXIT_MFE_STAGE4_AT_PT = Config.REVERSAL_EXIT_MFE_STAGE4_AT_PT
REVERSAL_EXIT_MFE_MIN_RETRACE_PT = Config.REVERSAL_EXIT_MFE_MIN_RETRACE_PT
REVERSAL_EXIT_MFE_MIN_RETRACE_PT_STAGE1 = Config.REVERSAL_EXIT_MFE_MIN_RETRACE_PT_STAGE1
REVERSAL_EXIT_MFE_MIN_RETRACE_PT_STAGE2 = Config.REVERSAL_EXIT_MFE_MIN_RETRACE_PT_STAGE2
REVERSAL_EXIT_MFE_MIN_RETRACE_PT_STAGE3 = Config.REVERSAL_EXIT_MFE_MIN_RETRACE_PT_STAGE3
REVERSAL_EXIT_MFE_MIN_RETRACE_PT_STAGE4 = Config.REVERSAL_EXIT_MFE_MIN_RETRACE_PT_STAGE4
REVERSAL_EXIT_MFE_CUT_BAND_MODE = Config.REVERSAL_EXIT_MFE_CUT_BAND_MODE
REVERSAL_EXIT_MFE_CUT_BAND_POSITION = Config.REVERSAL_EXIT_MFE_CUT_BAND_POSITION
REVERSAL_EXIT_MFE_CUT_BAND_SEED = Config.REVERSAL_EXIT_MFE_CUT_BAND_SEED
TREND_EXIT_MFE_MIN_PT = Config.TREND_EXIT_MFE_MIN_PT
TREND_EXIT_MFE_RETRACE_PCT = Config.TREND_EXIT_MFE_RETRACE_PCT
TREND_EXIT_MFE_RETRACE_PCT_STAGE1_MIN = Config.TREND_EXIT_MFE_RETRACE_PCT_STAGE1_MIN
TREND_EXIT_MFE_RETRACE_PCT_STAGE1_MAX = Config.TREND_EXIT_MFE_RETRACE_PCT_STAGE1_MAX
TREND_EXIT_MFE_RETRACE_PCT_STAGE2_MIN = Config.TREND_EXIT_MFE_RETRACE_PCT_STAGE2_MIN
TREND_EXIT_MFE_RETRACE_PCT_STAGE2_MAX = Config.TREND_EXIT_MFE_RETRACE_PCT_STAGE2_MAX
TREND_EXIT_MFE_RETRACE_PCT_STAGE3_MIN = Config.TREND_EXIT_MFE_RETRACE_PCT_STAGE3_MIN
TREND_EXIT_MFE_RETRACE_PCT_STAGE3_MAX = Config.TREND_EXIT_MFE_RETRACE_PCT_STAGE3_MAX
TREND_EXIT_MFE_RETRACE_PCT_STAGE4_MIN = Config.TREND_EXIT_MFE_RETRACE_PCT_STAGE4_MIN
TREND_EXIT_MFE_RETRACE_PCT_STAGE4_MAX = Config.TREND_EXIT_MFE_RETRACE_PCT_STAGE4_MAX
TREND_EXIT_MFE_RETRACE_PCT_STAGE1 = Config.TREND_EXIT_MFE_RETRACE_PCT_STAGE1
TREND_EXIT_MFE_RETRACE_PCT_STAGE2 = Config.TREND_EXIT_MFE_RETRACE_PCT_STAGE2
TREND_EXIT_MFE_RETRACE_PCT_STAGE3 = Config.TREND_EXIT_MFE_RETRACE_PCT_STAGE3
TREND_EXIT_MFE_RETRACE_PCT_STAGE4 = Config.TREND_EXIT_MFE_RETRACE_PCT_STAGE4
TREND_EXIT_MFE_STAGE2_AT_PT = Config.TREND_EXIT_MFE_STAGE2_AT_PT
TREND_EXIT_MFE_STAGE3_AT_PT = Config.TREND_EXIT_MFE_STAGE3_AT_PT
TREND_EXIT_MFE_STAGE4_AT_PT = Config.TREND_EXIT_MFE_STAGE4_AT_PT
TREND_EXIT_MFE_MIN_RETRACE_PT = Config.TREND_EXIT_MFE_MIN_RETRACE_PT
TREND_EXIT_MFE_MIN_RETRACE_PT_STAGE1 = Config.TREND_EXIT_MFE_MIN_RETRACE_PT_STAGE1
TREND_EXIT_MFE_MIN_RETRACE_PT_STAGE2 = Config.TREND_EXIT_MFE_MIN_RETRACE_PT_STAGE2
TREND_EXIT_MFE_MIN_RETRACE_PT_STAGE3 = Config.TREND_EXIT_MFE_MIN_RETRACE_PT_STAGE3
TREND_EXIT_MFE_MIN_RETRACE_PT_STAGE4 = Config.TREND_EXIT_MFE_MIN_RETRACE_PT_STAGE4
TREND_EXIT_MFE_CUT_BAND_MODE = Config.TREND_EXIT_MFE_CUT_BAND_MODE
TREND_EXIT_MFE_CUT_BAND_POSITION = Config.TREND_EXIT_MFE_CUT_BAND_POSITION
TREND_EXIT_MFE_CUT_BAND_SEED = Config.TREND_EXIT_MFE_CUT_BAND_SEED
EXIT_MFE_TRIGGER_MODE = Config.EXIT_MFE_TRIGGER_MODE
EXIT_MFE_TOUCH_USE_BAR_EXTREMA = Config.EXIT_MFE_TOUCH_USE_BAR_EXTREMA
USE_BAR3_CUT_EXIT = Config.USE_BAR3_CUT_EXIT
EXIT_BAR_LOOKBACK = Config.EXIT_BAR_LOOKBACK
EXIT_BAR_RETRACE_PCT = Config.EXIT_BAR_RETRACE_PCT
USE_MAX_HOLD_EXIT = Config.USE_MAX_HOLD_EXIT
MAX_HOLD_MIN = Config.MAX_HOLD_MIN
BT_STOP_LOSS_PT = Config.BT_STOP_LOSS_PT

USE_ADX_FLOOR = Config.USE_ADX_FLOOR
ADX_FLOOR_LONG = Config.ADX_FLOOR_LONG
ADX_FLOOR_SHORT = Config.ADX_FLOOR_SHORT
ADX_PERIOD = Config.ADX_PERIOD
ADX_DELTA_BARS = Config.ADX_DELTA_BARS
ADX_DELTA_MIN = Config.ADX_DELTA_MIN
USE_MACD_OSCI_FILTER = Config.USE_MACD_OSCI_FILTER
MACD_OSCI_LONG_MIN = Config.MACD_OSCI_LONG_MIN
MACD_OSCI_SHORT_MAX = Config.MACD_OSCI_SHORT_MAX
MACD_FAST = Config.MACD_FAST
MACD_SLOW = Config.MACD_SLOW
MACD_SIGNAL = Config.MACD_SIGNAL
USE_REGIME_ANCHOR_0845 = Config.USE_REGIME_ANCHOR_0845
REGIME_ANCHOR_TIME = Config.REGIME_ANCHOR_TIME
USE_REGIME_FILTER = Config.USE_REGIME_FILTER
BT_USE_REGIME_FILTER = Config.BT_USE_REGIME_FILTER
USE_GAP_REGIME_ADJUST = Config.USE_GAP_REGIME_ADJUST
GAP_REGIME_ADJUST_MIN_PT = Config.GAP_REGIME_ADJUST_MIN_PT
GAP_REGIME_ADJUST_FACTOR = Config.GAP_REGIME_ADJUST_FACTOR
GAP_REGIME_ADJUST_MAX_PT = Config.GAP_REGIME_ADJUST_MAX_PT
USE_LATE_SESSION_TIGHTEN = Config.USE_LATE_SESSION_TIGHTEN
LATE_SESSION_START = Config.LATE_SESSION_START
LATE_LONG_ENTRY_Z = Config.LATE_LONG_ENTRY_Z
LATE_SHORT_ENTRY_Z = Config.LATE_SHORT_ENTRY_Z
LATE_ADX_DELTA_MIN = Config.LATE_ADX_DELTA_MIN
TRADE_START = Config.TRADE_START
TRADE_END = Config.TRADE_END
ALLOW_SAME_BAR_REVERSE = Config.ALLOW_SAME_BAR_REVERSE
USE_REVERSE_SIGNAL_EXIT = Config.USE_REVERSE_SIGNAL_EXIT
ALLOW_REVERSE_SIGNAL_REENTRY = Config.ALLOW_REVERSE_SIGNAL_REENTRY
USE_REVERSE_Z5_ABS_FALLBACK = Config.USE_REVERSE_Z5_ABS_FALLBACK
REVERSE_Z5_ABS_LEVEL = Config.REVERSE_Z5_ABS_LEVEL
REVERSE_ENTRY_COOLDOWN_SEC = Config.REVERSE_ENTRY_COOLDOWN_SEC
REVERSE_SIGNAL_REENTRY_COOLDOWN_SEC = Config.REVERSE_SIGNAL_REENTRY_COOLDOWN_SEC
FIXED_LOSS_REENTRY_COOLDOWN_SEC = Config.FIXED_LOSS_REENTRY_COOLDOWN_SEC
MFE_CUT_REENTRY_COOLDOWN_SEC = Config.MFE_CUT_REENTRY_COOLDOWN_SEC
POST_FILL_ACTION_COOLDOWN_SEC = Config.POST_FILL_ACTION_COOLDOWN_SEC
POST_EXIT_OVERLAP_ENTRY_WINDOW_SEC = Config.POST_EXIT_OVERLAP_ENTRY_WINDOW_SEC
POST_EXIT_OVERLAP_ENTRY_DELAY_MS = Config.POST_EXIT_OVERLAP_ENTRY_DELAY_MS
POST_EXIT_ENTRY_UNKNOWN_CLEAR_SEC = Config.POST_EXIT_ENTRY_UNKNOWN_CLEAR_SEC
POST_EXIT_ENTRY_MAX_SEND_ATTEMPTS = Config.POST_EXIT_ENTRY_MAX_SEND_ATTEMPTS
MFE_PROTECT_POST_EXIT_ENTRY_ENABLED = Config.MFE_PROTECT_POST_EXIT_ENTRY_ENABLED
MFE_PROTECT_RESET_ENTRY_ARMS = Config.MFE_PROTECT_RESET_ENTRY_ARMS
EOD_BLOCK_NEW_ENTRY = Config.EOD_BLOCK_NEW_ENTRY
EOD_CUTOFF = Config.EOD_CUTOFF
EOD_FORCE_EXIT_POSITION = Config.EOD_FORCE_EXIT_POSITION
SHORT_Z5_BLOCK_EXTREME = Config.SHORT_Z5_BLOCK_EXTREME
SHORT_Z5_BLOCK_BULL_FULL = Config.SHORT_Z5_BLOCK_BULL_FULL
SHORT_Z1_BLOCK_BULL_FULL = Config.SHORT_Z1_BLOCK_BULL_FULL
SHORT_Z5_BLOCK_BULL_FULL_SECONDARY = Config.SHORT_Z5_BLOCK_BULL_FULL_SECONDARY
SHORT_Z1_BLOCK_MIXED = Config.SHORT_Z1_BLOCK_MIXED
SHORT_Z5_BLOCK_MIXED = Config.SHORT_Z5_BLOCK_MIXED

ENTRY_SIGNAL_SOURCE = Config.ENTRY_SIGNAL_SOURCE
USE_PRICE_EXTREMA_ENTRY = Config.USE_PRICE_EXTREMA_ENTRY
PRICE_EXTREMA_ANCHOR_SOURCE = Config.PRICE_EXTREMA_ANCHOR_SOURCE
REVERSAL_CONFIRMED_EXTREMA_SOURCE = Config.REVERSAL_CONFIRMED_EXTREMA_SOURCE
TREND_PRICE_EXTREMA_SOURCE = Config.TREND_PRICE_EXTREMA_SOURCE
REVERSAL_REQUIRE_EXTREMA_PRIME = Config.REVERSAL_REQUIRE_EXTREMA_PRIME
REVERSAL_BLOCK_BOTH_FIRE = Config.REVERSAL_BLOCK_BOTH_FIRE
PRICE_EXTREMA_LOOKBACK_BARS = Config.PRICE_EXTREMA_LOOKBACK_BARS
TREND_PRICE_EXTREMA_LOOKBACK_BARS = Config.TREND_PRICE_EXTREMA_LOOKBACK_BARS
REVERSAL_ARM_AFTER_CONFIRMED_EXTREMA_TOUCH = Config.REVERSAL_ARM_AFTER_CONFIRMED_EXTREMA_TOUCH
TREND_ARM_AFTER_CONFIRMED_EXTREMA_TOUCH = Config.TREND_ARM_AFTER_CONFIRMED_EXTREMA_TOUCH
PRICE_EXTREMA_OFFSET_PT = Config.PRICE_EXTREMA_OFFSET_PT
PRICE_FIRE_BAND_MIN_PT = Config.PRICE_FIRE_BAND_MIN_PT
PRICE_FIRE_BAND_MAX_PT = Config.PRICE_FIRE_BAND_MAX_PT

USE_HYBRID_ENTRY_REGIME = Config.USE_HYBRID_ENTRY_REGIME
SWAP_ENTRY_REGIME_RULES = Config.SWAP_ENTRY_REGIME_RULES
USE_TREND_RULES_IN_REVERSAL_Z5_ZONE = Config.USE_TREND_RULES_IN_REVERSAL_Z5_ZONE
ENTRY_REGIME_REVERSAL_Z5_ABS_MAX = Config.ENTRY_REGIME_REVERSAL_Z5_ABS_MAX
ENTRY_REGIME_REVERSAL_Z5_ABS_MIN = Config.ENTRY_REGIME_REVERSAL_Z5_ABS_MIN
ENTRY_REGIME_TREND_Z5_ABS_MIN = Config.ENTRY_REGIME_TREND_Z5_ABS_MIN
ENTRY_REGIME_TREND_Z5_ABS_MAX = Config.ENTRY_REGIME_TREND_Z5_ABS_MAX
ENTRY_COMMON_Z5_ABS_MAX = Config.ENTRY_COMMON_Z5_ABS_MAX
USE_Z5_ENTRY_OUTER_CAP = Config.USE_Z5_ENTRY_OUTER_CAP
Z5_REGIME_BAND_PATCH_TAG = Config.Z5_REGIME_BAND_PATCH_TAG
USE_TREND_Z5_ZONE_OPPOSITE_ENTRY_BLOCK_V9 = Config.USE_TREND_Z5_ZONE_OPPOSITE_ENTRY_BLOCK_V9
REVERSAL_PRICE_EXTREMA_OFFSET_PT = Config.REVERSAL_PRICE_EXTREMA_OFFSET_PT
REVERSAL_PRICE_FIRE_BAND_MIN_PT = Config.REVERSAL_PRICE_FIRE_BAND_MIN_PT
REVERSAL_PRICE_FIRE_BAND_MAX_PT = Config.REVERSAL_PRICE_FIRE_BAND_MAX_PT
REVERSAL_PREV_CANDLE_CONDITION_MODE = Config.REVERSAL_PREV_CANDLE_CONDITION_MODE
ENTRY_PREV_VALID_CANDLE_COLOR_FILTER_ENABLED = Config.ENTRY_PREV_VALID_CANDLE_COLOR_FILTER_ENABLED
REVERSAL_PREV_CANDLE_LOOKBACK_BARS = Config.REVERSAL_PREV_CANDLE_LOOKBACK_BARS
ENTRY_PREV_CANDLE_MIN_RANGE_PT = Config.ENTRY_PREV_CANDLE_MIN_RANGE_PT
USE_PREV_CANDLE_OPPOSITE_ENTRY_BLOCK = Config.USE_PREV_CANDLE_OPPOSITE_ENTRY_BLOCK
REVERSAL_USE_15S_EXTREMA = Config.REVERSAL_USE_15S_EXTREMA
REVERSAL_ARM_ROLLING_15S_ENABLED = Config.REVERSAL_ARM_ROLLING_15S_ENABLED
REVERSAL_ARM_WAIT_WATCH_ENABLED = Config.REVERSAL_ARM_WAIT_WATCH_ENABLED
ENTRY_STAGE_WAIT_SEC = Config.ENTRY_STAGE_WAIT_SEC
REVERSAL_CANDIDATE_TIMEOUT_SEC = Config.REVERSAL_CANDIDATE_TIMEOUT_SEC
TREND_PRICE_EXTREMA_OFFSET_PT = Config.TREND_PRICE_EXTREMA_OFFSET_PT
TREND_PREV_CANDLE_CONDITION_MODE = Config.TREND_PREV_CANDLE_CONDITION_MODE
TREND_PREV_CANDLE_LOOKBACK_BARS = Config.TREND_PREV_CANDLE_LOOKBACK_BARS
REVERSAL_ZONE_TREND_PREV_CANDLE_CONDITION_MODE = Config.REVERSAL_ZONE_TREND_PREV_CANDLE_CONDITION_MODE
TREND_BLOCK_CONSECUTIVE_SAME_4_ENABLED = Config.TREND_BLOCK_CONSECUTIVE_SAME_4_ENABLED
TREND_BLOCK_FORWARD_4_OF_5_ENABLED = Config.TREND_BLOCK_FORWARD_4_OF_5_ENABLED
TREND_CANDLE3_Z5_BAND_OVERRIDE_ENABLED = Config.TREND_CANDLE3_Z5_BAND_OVERRIDE_ENABLED
TREND_PREV_CANDLE_PREREQUISITE_ENABLED = Config.TREND_PREV_CANDLE_PREREQUISITE_ENABLED
TREND_ARM_ROLLING_15S_ENABLED = Config.TREND_ARM_ROLLING_15S_ENABLED
TREND_ARM_WAIT_WATCH_ENABLED = Config.TREND_ARM_WAIT_WATCH_ENABLED
TREND_ENTRY_STAGE_WAIT_SEC = Config.TREND_ENTRY_STAGE_WAIT_SEC
TREND_CANDIDATE_TIMEOUT_SEC = Config.TREND_CANDIDATE_TIMEOUT_SEC
TREND_FIRE_CONFIRM_MIN_SEC = Config.TREND_FIRE_CONFIRM_MIN_SEC
TREND_FIRE_CONFIRM_TIMEOUT_SEC = Config.TREND_FIRE_CONFIRM_TIMEOUT_SEC
USE_DZ5_ARM_ENTRY = Config.USE_DZ5_ARM_ENTRY
USE_PREV_Z5_DELTA_ENTRY = Config.USE_PREV_Z5_DELTA_ENTRY
PREV_Z5_DELTA_ENTRY = Config.PREV_Z5_DELTA_ENTRY
USE_PREV_Z1_DELTA_ENTRY = Config.USE_PREV_Z1_DELTA_ENTRY
PREV_Z1_DELTA_ENTRY = Config.PREV_Z1_DELTA_ENTRY
USE_Z1_DELTA_REVERSAL_ENTRY = Config.USE_Z1_DELTA_REVERSAL_ENTRY
MONITOR_Z1_DELTA_CONTINUATION = Config.MONITOR_Z1_DELTA_CONTINUATION
Z1_DELTA_ENTRY_MODE = Config.Z1_DELTA_ENTRY_MODE
USE_DELTA_TWO_TOUCH_ARM = Config.USE_DELTA_TWO_TOUCH_ARM
USE_Z5_DELTA_REVERSAL_ENTRY = Config.USE_Z5_DELTA_REVERSAL_ENTRY
MONITOR_Z5_DELTA_CONTINUATION = Config.MONITOR_Z5_DELTA_CONTINUATION
Z5_DELTA_ENTRY_MODE = Config.Z5_DELTA_ENTRY_MODE
Z1_DELTA_REVERSAL_ARM = Config.Z1_DELTA_REVERSAL_ARM
Z1_DELTA_REVERSAL_PREHIT = Config.Z1_DELTA_REVERSAL_PREHIT
Z1_DELTA_REVERSAL_FIRE = Config.Z1_DELTA_REVERSAL_FIRE
Z1_DELTA_REVERSAL_FIRE_MIN = Config.Z1_DELTA_REVERSAL_FIRE_MIN
Z1_DELTA_REVERSAL_FIRE_MAX = Config.Z1_DELTA_REVERSAL_FIRE_MAX
USE_Z1_DELTA_DUAL_REGIME_ENTRY = Config.USE_Z1_DELTA_DUAL_REGIME_ENTRY
Z1_DELTA_TREND_ARM = Config.Z1_DELTA_TREND_ARM
Z1_DELTA_TREND_FIRE = Config.Z1_DELTA_TREND_FIRE
Z5_DELTA_REVERSAL_ARM = Config.Z5_DELTA_REVERSAL_ARM
Z5_DELTA_REVERSAL_PREHIT = Config.Z5_DELTA_REVERSAL_PREHIT
Z5_DELTA_REVERSAL_FIRE = Config.Z5_DELTA_REVERSAL_FIRE
Z5_DELTA_REVERSAL_FIRE_MIN = Config.Z5_DELTA_REVERSAL_FIRE_MIN
Z5_DELTA_REVERSAL_FIRE_MAX = Config.Z5_DELTA_REVERSAL_FIRE_MAX
SWAP_ENTRY_ARM_BAND_SIDE = Config.SWAP_ENTRY_ARM_BAND_SIDE
DZ5_LIVE_BOOTSTRAP_BARS = Config.DZ5_LIVE_BOOTSTRAP_BARS
USE_DZ5_SESSION_LOW_ANCHOR = Config.USE_DZ5_SESSION_LOW_ANCHOR
DZ5_ARM_LONG = Config.DZ5_ARM_LONG
DZ5_ENTRY_LONG = Config.DZ5_ENTRY_LONG
DZ5_ARM_LONG_MAX = Config.DZ5_ARM_LONG_MAX
DZ5_ARM_SHORT_MIN = Config.DZ5_ARM_SHORT_MIN
DZ5_ARM_SHORT = Config.DZ5_ARM_SHORT
DZ5_ENTRY_SHORT = Config.DZ5_ENTRY_SHORT
DZ5_ARM_EXPIRY_BARS = Config.DZ5_ARM_EXPIRY_BARS
DZ5_ENTRY_EXTREMA_LOOKBACK_BARS = Config.DZ5_ENTRY_EXTREMA_LOOKBACK_BARS
USE_ENTRY_PRICE_RANGE_FILTER = Config.USE_ENTRY_PRICE_RANGE_FILTER
ENTRY_PRICE_RANGE_LOOKBACK_BARS = Config.ENTRY_PRICE_RANGE_LOOKBACK_BARS
ENTRY_PRICE_RANGE_MIN_PT = Config.ENTRY_PRICE_RANGE_MIN_PT
POST_WARMUP_ENTRY_DELAY_BARS = Config.POST_WARMUP_ENTRY_DELAY_BARS
DZ5_PREV_COMMIT_MODE = Config.DZ5_PREV_COMMIT_MODE
LONG_ENTRY_Z5_MIN = Config.LONG_ENTRY_Z5_MIN
LONG_ENTRY_Z5_MAX = Config.LONG_ENTRY_Z5_MAX
SHORT_ENTRY_Z5_MIN = Config.SHORT_ENTRY_Z5_MIN
SHORT_ENTRY_Z5_MAX = Config.SHORT_ENTRY_Z5_MAX
Z5_LONG_BAND_LOW = Config.Z5_LONG_BAND_LOW
Z5_LONG_BAND_HIGH = Config.Z5_LONG_BAND_HIGH
Z5_SHORT_BAND_LOW = Config.Z5_SHORT_BAND_LOW
Z5_SHORT_BAND_HIGH = Config.Z5_SHORT_BAND_HIGH
USE_STATIC_Z5_ENTRY_BAND = Config.USE_STATIC_Z5_ENTRY_BAND
USE_Z5_ENTRY_BAND_FILTER = Config.USE_Z5_ENTRY_BAND_FILTER
USE_Z5_ENTRY_DEADZONE = Config.USE_Z5_ENTRY_DEADZONE
USE_DZ5_ANCHOR_POLARITY_FILTER = Config.USE_DZ5_ANCHOR_POLARITY_FILTER
USE_EMA20_ENTRY_FILTER = Config.USE_EMA20_ENTRY_FILTER
EMA_CROSS_GAP_PT = Config.EMA_CROSS_GAP_PT
USE_Z5_REALTIME_BLEND = Config.USE_Z5_REALTIME_BLEND
Z5_REALTIME_BLEND_WEIGHT = Config.Z5_REALTIME_BLEND_WEIGHT
EXIT_Z_SIGNAL_SOURCE = Config.EXIT_Z_SIGNAL_SOURCE
USE_EXIT_Z = Config.USE_EXIT_Z
EXIT_Z_SKIP_WHEN_MFE_ARMED = Config.EXIT_Z_SKIP_WHEN_MFE_ARMED
EXIT_Z_LONG = Config.EXIT_Z_LONG
EXIT_Z_SHORT = Config.EXIT_Z_SHORT
USE_EXIT_Z_PNL_GATE = Config.USE_EXIT_Z_PNL_GATE
EXIT_Z_MAX_PNL_PT = Config.EXIT_Z_MAX_PNL_PT
EXIT_Z_RETRACE_DELTA = Config.EXIT_Z_RETRACE_DELTA
USE_EXIT_Z_TAKE_PROFIT_ENTRY_DELTA = Config.USE_EXIT_Z_TAKE_PROFIT_ENTRY_DELTA
USE_EXIT_Z_FIXED_OR_ENTRY_DELTA = Config.USE_EXIT_Z_FIXED_OR_ENTRY_DELTA
EXIT_Z_TP_ENABLED = Config.EXIT_Z_TP_ENABLED
EXIT_Z_SL_ENABLED = Config.EXIT_Z_SL_ENABLED
EXIT_Z_FIXED_LONG = Config.EXIT_Z_FIXED_LONG
EXIT_Z_FIXED_SHORT = Config.EXIT_Z_FIXED_SHORT
EXIT_Z_ENTRY_DELTA = Config.EXIT_Z_ENTRY_DELTA
EXIT_Z_TP_DELTA = Config.EXIT_Z_TP_DELTA
EXIT_Z_TP_MIN_PNL_PT = Config.EXIT_Z_TP_MIN_PNL_PT
EXIT_Z_LOOKBACK_BARS = Config.EXIT_Z_LOOKBACK_BARS
EXIT_Z_EXTREMA_DIFF = Config.EXIT_Z_EXTREMA_DIFF
BT_ENTRY_INTRABAR_REQUIRE_CURRENT_BAR_ARM = Config.BT_ENTRY_INTRABAR_REQUIRE_CURRENT_BAR_ARM
BT_USE_PRICE_EXTREMA_ENTRY = Config.BT_USE_PRICE_EXTREMA_ENTRY
BT_ALLOW_SAME_BAR_REVERSE_ENTRY = Config.BT_ALLOW_SAME_BAR_REVERSE_ENTRY


# Account/TR startup guards for LIVE/PAPER selection.
BOOT_ACCOUNT_SYNC_DELAY_MS = 1500
ACCOUNT_INFO_FAIL_BLOCK_SEC = 20.0


def describe_trade_time_gate(cfg=Config):
    target = cfg or Config
    trade_start = str(getattr(target, "TRADE_START", Config.TRADE_START) or Config.TRADE_START)
    trade_end = str(getattr(target, "TRADE_END", Config.TRADE_END) or Config.TRADE_END)
    eod_cutoff = str(getattr(target, "EOD_CUTOFF", trade_end) or trade_end)
    return {
        "trade_start": trade_start,
        "trade_end": trade_end,
        "eod_cutoff": eod_cutoff,
    }

# ===== KIS_TICK_DATA tick backfill / tick-replay backtest =====
# Keep tick backfill separate from 1-minute warmup data.
TICK_BACKFILL_TR_CODE = Config.TICK_BACKFILL_TR_CODE
TICK_BACKFILL_RQNAME = Config.TICK_BACKFILL_RQNAME
TICK_BACKFILL_SCREEN = Config.TICK_BACKFILL_SCREEN
TICK_BACKFILL_UNIT = Config.TICK_BACKFILL_UNIT          # 1 = 1-tick chart
TICK_BACKFILL_DAYS = Config.TICK_BACKFILL_DAYS
TICK_BACKFILL_OUTPUT_DIR = Config.TICK_BACKFILL_OUTPUT_DIR
TICK_BACKFILL_PAGE_DELAY_MS = Config.TICK_BACKFILL_PAGE_DELAY_MS
TICK_BACKFILL_MAX_PAGES = Config.TICK_BACKFILL_MAX_PAGES
# KIS_TICK_DATA repeat block parser: GetCommDataEx first, named GetCommData fallback.
TICK_BACKFILL_USE_COMM_DATA_EX = Config.TICK_BACKFILL_USE_COMM_DATA_EX
TICK_BACKFILL_KEEP_IDENTICAL_TICKS = Config.TICK_BACKFILL_KEEP_IDENTICAL_TICKS

# Backtester can optionally use KIS_TICK_DATA tick CSV as its intrabar path.
BT_USE_KIS_TICK_DATA_TICK_REPLAY = False
BT_TICK_REPLAY_DIR = "data/ticks"
BT_TICK_REPLAY_STRICT_DATES = True

# ===== historical tick-replay engine =====
TICK_REPLAY_ENGINE = Config.TICK_REPLAY_ENGINE
TICK_REPLAY_TRADING_DAYS = Config.TICK_REPLAY_TRADING_DAYS
TICK_REPLAY_CACHE_DIR = Config.TICK_REPLAY_CACHE_DIR
TICK_REPLAY_EVENT_LOG_LEVEL = Config.TICK_REPLAY_EVENT_LOG_LEVEL
TICK_REPLAY_REQUIRE_EXACT_SEQUENCE = Config.TICK_REPLAY_REQUIRE_EXACT_SEQUENCE
TICK_REPLAY_ALLOW_COMPRESSION = Config.TICK_REPLAY_ALLOW_COMPRESSION
TICK_REPLAY_FILL_POLICY = Config.TICK_REPLAY_FILL_POLICY
TICK_REPLAY_MAX_TICKS_PER_DAY = Config.TICK_REPLAY_MAX_TICKS_PER_DAY
TICK_REPLAY_TICK_LIMIT_POLICY = Config.TICK_REPLAY_TICK_LIMIT_POLICY
TICK_REPLAY_USE_LIVE_DYNAMIC_WARMUP = Config.TICK_REPLAY_USE_LIVE_DYNAMIC_WARMUP
TICK_REPLAY_DATA_LIMIT_PATCH_TAG = Config.TICK_REPLAY_DATA_LIMIT_PATCH_TAG
