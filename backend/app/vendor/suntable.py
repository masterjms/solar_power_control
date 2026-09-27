# -*- coding: utf-8 -*-
"""SolarLTE 1년 스케줄 표 계산 — 기준 구현 (정수 연산, 판 SUNTABLE-1)

같은 입력이면 PC 도구, 단말 펌웨어(FW/Modules/suntable.c), 서버가 **비트 단위로 같은**
372일 표를 만들어야 한다. 그래서 실수(float)를 쓰지 않는다. 실수 삼각함수는 언어와
수학 라이브러리마다 마지막 자리가 달라 드물게 1분씩 어긋난다.

  입력 : lat_e6, lon_e6 (도 x 1,000,000 정수), on_corr, off_corr (분, 정수)
  출력 : 372칸 [(점등 시, 분, 소등 시, 분), ...] 과 그 CRC-32

계산식은 NOAA 일출/일몰 근사식(sunlib.py 와 같은 식)이며, 모든 값을 2^24 배 정수
(Q24)로 다룬다. 삼각함수는 CORDIC(더하기와 시프트만), 제곱근은 정수 제곱근이다.

이식 규칙 (서버가 다른 언어로 옮길 때)
  1. 64비트 정수로 계산한다. JavaScript 는 BigInt 를 쓴다
  2. 오른쪽 시프트와 나눗셈은 모두 **내림(floor)** 이다. C 의 / 와 % 는 0 쪽으로
     자르므로 그대로 쓰지 않는다 (asr, fdiv, fmod 참고)
  3. 아래 상수는 계산하지 말고 정수 그대로 쓴다
  4. suntable_vectors.json 의 모든 항목과 CRC 가 같아야 한다
"""
import math
import zlib

VERSION = 'SUNTABLE-1'

Q = 24
ONE = 1 << Q
DEG360 = 360 * ONE
DEG180 = 180 * ONE
DEG90 = 90 * ONE

TZ_MINUTES = 540                 # KST
MINUTES_PER_DAY = 1440
DAYS_PER_MONTH = 31
DAY_SLOTS = 12 * DAYS_PER_MONTH  # 372

# 표는 월/일 372칸이라 해마다 같이 쓴다. 윤년(2028) 달력으로 계산한다.
MONTH_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
MONTH_START = (0, 31, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335)

# CORDIC : ATAN[i] = atan(2^-i) 도 x 2^24, KINV = 28단 이득의 역수 x 2^24
CORDIC_N = 28
ATAN = (754974720, 445687602, 235489088, 119537938, 60000934, 30029717,
        15018523, 7509720, 3754917, 1877466, 938734, 469367, 234684, 117342,
        58671, 29335, 14668, 7334, 3667, 1833, 917, 458, 229, 115, 57, 29,
        14, 7)
KINV = 10188014

# 식의 상수 x 2^24
K_09856 = 16535624       # 0.9856
K_3289 = 55180263        # 3.289
K_1916 = 32145146        # 1.916
K_0020 = 335544          # 0.020
K_282634 = 4741811667    # 282.634
K_091764 = 15395444      # 0.91764
K_039782 = 6674312       # 0.39782
K_006571 = 1102431       # 0.06571
K_6622 = 111098724       # 6.622
ZENITH = 1523924861      # 90.833 도 (공식 일출/일몰, 굴절 + 태양 반지름)


# ---------------------------------------------------------------- 정수 도구
def asr(x, n):
    """내림 오른쪽 시프트. Python >> 는 원래 내림이다."""
    return x >> n


def fmul(a, b):
    return (a * b) >> Q


def fdiv(a, b):
    """내림 나눗셈 (a / b) x 2^24"""
    return (a * ONE) // b


def fmod(a, m):
    """내림 나머지. 결과는 0 <= r < m"""
    return a % m


def isqrt(v):
    return math.isqrt(v) if v > 0 else 0


def sincos(a):
    """(sin a, cos a) x 2^24. a 는 도 x 2^24"""
    a = fmod(a + DEG180, DEG360) - DEG180          # [-180, 180)
    neg = False
    if a > DEG90:
        a -= DEG180
        neg = True
    elif a < -DEG90:
        a += DEG180
        neg = True
    x, y, z = KINV, 0, a
    for i in range(CORDIC_N):
        if z >= 0:
            x, y, z = x - asr(y, i), y + asr(x, i), z - ATAN[i]
        else:
            x, y, z = x + asr(y, i), y - asr(x, i), z + ATAN[i]
    if neg:
        x, y = -x, -y
    return y, x


def atan2(y, x):
    """도 x 2^24, 범위 (-180, 180]"""
    if x == 0 and y == 0:
        return 0
    base = 0
    if x < 0:
        x, y = -x, -y
        base = DEG180
    z = 0
    for i in range(CORDIC_N):
        if y > 0:
            x, y, z = x + asr(y, i), y - asr(x, i), z + ATAN[i]
        else:
            x, y, z = x - asr(y, i), y + asr(x, i), z - ATAN[i]
    r = base + z
    if r > DEG180:
        r -= DEG360
    return r


def unit_sqrt(s):
    """sqrt(1 - s^2) x 2^24. s 가 1 을 조금 넘으면 0"""
    return isqrt(ONE * ONE - s * s)


# ---------------------------------------------------------------- 계산
def sun_times(month, day, lat_e6, lon_e6):
    """(일출, 일몰) 분(0~1439). 해가 뜨지 않거나 지지 않으면 None"""
    n = MONTH_START[month - 1] + day
    lat = (lat_e6 * ONE) // 1000000                # 도 x 2^24 (내림)
    lon = (lon_e6 * ONE) // 1000000
    sin_lat, cos_lat = sincos(lat)
    _, cos_z = sincos(ZENITH)
    lng_hour = fdiv(lon, 15 * ONE)
    out = []
    for rising in (True, False):
        base = 6 if rising else 18
        t = n * ONE + fdiv(base * ONE - lng_hour, 24 * ONE)
        m = fmul(K_09856, t) - K_3289
        s1, _ = sincos(m)
        s2, _ = sincos(2 * m)
        L = fmod(m + fmul(K_1916, s1) + fmul(K_0020, s2) + K_282634, DEG360)
        sin_l, cos_l = sincos(L)
        # 적경 : atan(0.91764 tan L) 을 L 과 같은 사분면에 둔 것 = atan2
        ra = fdiv(fmod(atan2(fmul(K_091764, sin_l), cos_l), DEG360), 15 * ONE)
        sin_dec = fmul(K_039782, sin_l)
        cos_dec = unit_sqrt(sin_dec)
        den = fmul(cos_dec, cos_lat)
        if den <= 0:                                    # 극점 : 나눌 수 없다 = 계산 불가 날
            return None
        cos_h = fdiv(cos_z - fmul(sin_dec, sin_lat), den)
        if cos_h > ONE or cos_h < -ONE:
            return None
        h = atan2(unit_sqrt(cos_h), cos_h)             # acos, 0 ~ 180 도
        if rising:
            h = DEG360 - h
        h = fdiv(h, 15 * ONE)
        mean_time = h + ra - fmul(K_006571, t) - K_6622
        utc = fmod(mean_time - lng_hour, 24 * ONE)
        local = utc * 60 + TZ_MINUTES * ONE
        out.append(((local + ONE // 2) >> Q) % MINUTES_PER_DAY)
    return out[0], out[1]


def build_table(lat_e6, lon_e6, on_corr, off_corr):
    """372칸 표. 없는 날짜(2월 30일 등)와 계산 불가 날은 그 월 앞날 값으로 채운다."""
    table = []
    last = (0, 0, 0, 0)
    for month in range(1, 13):
        for day in range(1, DAYS_PER_MONTH + 1):
            if day <= MONTH_DAYS[month - 1]:
                r = sun_times(month, day, lat_e6, lon_e6)
                if r is not None:
                    rise, sset = r
                    on = (sset + on_corr) % MINUTES_PER_DAY
                    off = (rise + off_corr) % MINUTES_PER_DAY
                    last = (on // 60, on % 60, off // 60, off % 60)
            table.append(last)
    return table


def table_bytes(table):
    return bytes(v for item in table for v in item)


def table_crc32(table):
    """단말 calculate_crc32 와 같은 표준 CRC-32 (zlib)"""
    return zlib.crc32(table_bytes(table)) & 0xFFFFFFFF
